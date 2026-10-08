// The Container Apps environment plus the two apps from docs/deployment.md
// §4. No custom VNet yet (that's §4b, alongside APIM) — Microsoft-managed
// networking is enough to give ai-fiqh-ollama a real internal-only ingress,
// since `ingress.external` is a per-app setting independent of whether the
// environment itself has a custom VNet.
//
// Identities are user-assigned, not system-assigned, on purpose: a
// system-assigned identity only exists once its container app does, so it
// can't hold AcrPull / Key Vault Secrets User at the moment that app's first
// revision tries to pull its image and resolve its secrets. A user-assigned
// identity is created first, granted its roles, and only then attached —
// the `dependsOn` on each app enforces that order.

param location string
param environmentName string

@description('Subnet delegated to Microsoft.App/environments. Empty keeps the environment on Microsoft-managed networking. Cannot be changed after the environment is created — a different value means a new environment.')
param infrastructureSubnetId string = ''

@description('true removes the environment public endpoint (only reachable from the VNet). false keeps a public endpoint while still being in the VNet, which is what lets private endpoints to Key Vault and Azure OpenAI be added later. Fixed at creation, like the subnet.')
param environmentInternal bool = false

// App names are parameters so a new environment can be built and tested while
// the old one still serves traffic: a container app name is unique per
// resource group, not per environment, and an existing app cannot be moved to
// another environment. The managed identities keep their names either way, so
// their existing role assignments are reused rather than duplicated.
param webAppName string = 'ai-fiqh-web'
param ollamaAppName string = 'ai-fiqh-ollama'

@description('CIDRs allowed to reach ai-fiqh-web, e.g. [\'203.0.113.7/32\']. Empty leaves it open to the internet.')
param allowedIpRanges array = []

// A variable because Bicep cannot loop inside a conditional expression.
var webIpRules = [for (cidr, i) in allowedIpRanges: {
  name: 'allow-${i}'
  ipAddressRange: cidr
  action: 'Allow'
}]

@description('Hostname APIM serves the app on. Sets the auth layer to build sign-in redirects from X-Forwarded-Host, without which sign-in breaks behind the gateway.')
param publicHostname string = ''

param logAnalyticsCustomerId string
@secure()
param logAnalyticsPrimarySharedKey string

param acrName string
param acrLoginServer string
param keyVaultName string
param keyVaultUri string

param webImageTag string
param ollamaImageTag string

param azureOpenAiEndpoint string
param azureOpenAiDeployment string

// Sign-in (docs/deployment.md §4a). Google rather than Entra ID because the
// Vodafone tenant blocks users from creating app registrations
// (`allowedToCreateApps: false`, checked 2026-09-21). Empty leaves the app
// open, as in phase 1, so this deploys safely before the Google client exists.
param googleClientId string = ''
var authEnabled = !empty(googleClientId)

// Two findings, in order, drove where ai-fiqh-ollama runs:
//
// 1. 2026-09-17, locally: the Consumption plan's 8 GiB per-container ceiling
//    OOM-kills gemma4:12b. So a Dedicated CPU profile (E4, 4 cores/32 GiB).
// 2. 2026-09-18, in Azure: E4 runs it, but CPU-only inference processes the
//    prompt at 6.7 tok/s — ~12 minutes for a 4,685-token RAG prompt, and the
//    Container Apps ingress cuts the request off at 4m0s (the 504 seen in
//    testing). Unusable regardless of the timeout.
//
// So it runs on a serverless GPU profile (no minimumCount/maximumCount —
// those are Dedicated-only), and a T4's 16 GB of VRAM comfortably holds this
// ~8 GB quantized model. Scale-to-zero turned out not to survive a GPU cold
// start; see the ai-fiqh-ollama resource below.
//
// The E4 profile is still declared below only because removing a profile
// while an app references it is a fight not worth having in one deployment;
// it costs nothing at minimumCount 0 and can be dropped in a later pass.
param gpuProfileName string = 'gpu-ollama'
param gpuProfileWorkloadType string = 'Consumption-GPU-NC8as-T4'
param dedicatedProfileWorkloadType string = 'E4'

var ollamaProfileName = 'ollama-profile'
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
var keyVaultSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: acrName
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

// --- Identities and their roles, created before either app -----------------

resource webIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-ai-fiqh-web'
  location: location
}

resource ollamaIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-ai-fiqh-ollama'
  location: location
}

resource webAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, webIdentity.id, acrPullRoleId)
  scope: acr
  properties: {
    principalId: webIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
  }
}

resource webKeyVaultSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, webIdentity.id, keyVaultSecretsUserRoleId)
  scope: keyVault
  properties: {
    principalId: webIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
  }
}

resource ollamaAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, ollamaIdentity.id, acrPullRoleId)
  scope: acr
  properties: {
    principalId: ollamaIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
  }
}

// --- Environment -------------------------------------------------------------

var vnetIntegrated = !empty(infrastructureSubnetId)

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: environmentName
  location: location
  properties: {
    // VNet membership and public reachability are independent. internal: false
    // keeps a public endpoint while putting the apps in the VNet — the outbound
    // path runs through the subnet, and private endpoints become possible.
    // internal: true needs the private DNS zone from private-dns.bicep, or
    // nothing can resolve the apps at all.
    vnetConfiguration: vnetIntegrated ? {
      infrastructureSubnetId: infrastructureSubnetId
      internal: environmentInternal
    } : null
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalyticsCustomerId
        sharedKey: logAnalyticsPrimarySharedKey
      }
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
      {
        name: ollamaProfileName
        workloadProfileType: dedicatedProfileWorkloadType
        minimumCount: 0
        maximumCount: 1
      }
      {
        name: gpuProfileName
        workloadProfileType: gpuProfileWorkloadType
      }
    ]
  }
}

// --- ai-fiqh-ollama: the content-filter fallback, kept warm ----------------
//
// Scale-to-zero was tried and does not work here (2026-09-18). Starting from
// zero took ~4 minutes to get a GPU node (four scheduling attempts, a minute
// apart) and then 95.6s to pull the 11 GB image, all before the model loads.
// The Container Apps ingress ends every request at 240s, so a fallback that
// has to wake the GPU always fails. One warm replica removes all of that.
//
// It is also now the most expensive component: a GPU billed continuously
// while the replica exists. Set `ollamaMinReplicas = 0` in main.bicepparam
// to stop paying for it between sessions (the fallback then fails again
// until it is set back to 1).
param ollamaMinReplicas int = 1

resource ollamaApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: ollamaAppName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${ollamaIdentity.id}': {}
    }
  }
  dependsOn: [
    ollamaAcrPull
  ]
  properties: {
    managedEnvironmentId: environment.id
    workloadProfileName: gpuProfileName
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: false
        targetPort: 11434
        transport: 'http'
      }
      registries: [
        {
          server: acrLoginServer
          identity: ollamaIdentity.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'ollama'
          image: '${acrLoginServer}/ai-fiqh-ollama:${ollamaImageTag}'
          // Sized to the whole NC8as-T4 node (8 cores / 56 GiB), which is how
          // a serverless GPU profile is allocated — one app per GPU node.
          resources: {
            cpu: json('8')
            memory: '56Gi'
          }
        }
      ]
      scale: {
        minReplicas: ollamaMinReplicas
        maxReplicas: 1
      }
    }
  }
}

// --- ai-fiqh-web: the Streamlit app, always warm ---------------------------

resource webApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: webAppName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${webIdentity.id}': {}
    }
  }
  dependsOn: [
    webAcrPull
    webKeyVaultSecretsUser
  ]
  properties: {
    managedEnvironmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      // Public unless the whole environment is internal — an app cannot have a
      // public endpoint in an internal environment.
      ingress: {
        external: !environmentInternal
        targetPort: 8000
        transport: 'auto'
        // Allow-list (§4d). Empty means open to the internet, as before. Any
        // address not listed gets a 403 at the ingress — before sign-in, so
        // bots never reach the Google prompt. Container Apps requires every
        // rule to share one action, so these are all Allow.
        ipSecurityRestrictions: empty(allowedIpRanges) ? null : webIpRules
      }
      registries: [
        {
          server: acrLoginServer
          identity: webIdentity.id
        }
      ]
      secrets: concat([
        {
          name: 'voyage-api-key'
          keyVaultUrl: '${keyVaultUri}secrets/voyage-api-key'
          identity: webIdentity.id
        }
        {
          name: 'anthropic-api-key'
          keyVaultUrl: '${keyVaultUri}secrets/anthropic-api-key'
          identity: webIdentity.id
        }
        {
          name: 'azure-openai-api-key'
          keyVaultUrl: '${keyVaultUri}secrets/azure-openai-api-key'
          identity: webIdentity.id
        }
      ], authEnabled ? [
        // Read by the auth config below (`clientSecretSettingName`), not by
        // the app. Must exist in Key Vault before this deploys, or the
        // revision fails to start.
        {
          name: 'google-client-secret'
          keyVaultUrl: '${keyVaultUri}secrets/google-client-secret'
          identity: webIdentity.id
        }
      ] : [])
    }
    template: {
      containers: [
        {
          name: 'web'
          image: '${acrLoginServer}/ai-fiqh-web:${webImageTag}'
          resources: {
            cpu: json('1')
            memory: '2Gi'
          }
          // The platform checks health from inside the environment, so a
          // revision only reports ready once Streamlit actually answers. This
          // replaces the deploy workflow's external health check, which an IP
          // allow-list would block (a GitHub runner is not on the list).
          probes: [
            {
              type: 'Readiness'
              httpGet: {
                path: '/_stcore/health'
                port: 8000
              }
              periodSeconds: 10
              failureThreshold: 3
            }
          ]
          env: [
            { name: 'AI_FIQH_LLM_PROVIDER', value: 'azure' }
            { name: 'AI_FIQH_LLM_FALLBACK_PROVIDER', value: 'ollama' }
            { name: 'AZURE_OPENAI_ENDPOINT', value: azureOpenAiEndpoint }
            { name: 'AZURE_OPENAI_DEPLOYMENT', value: azureOpenAiDeployment }
            // Confirmed against src/ai_fiqh/llm.py — OllamaClient reads
            // OLLAMA_HOST directly and passes it to ollama.Client(host=...).
            // Container Apps ingress terminates TLS and proxies to the
            // target port internally, so no :11434 here.
            { name: 'OLLAMA_HOST', value: 'https://${ollamaApp.name}.internal.${environment.properties.defaultDomain}' }
            { name: 'VOYAGE_API_KEY', secretRef: 'voyage-api-key' }
            { name: 'ANTHROPIC_API_KEY', secretRef: 'anthropic-api-key' }
            { name: 'AZURE_OPENAI_API_KEY', secretRef: 'azure-openai-api-key' }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
}

// --- Sign-in in front of ai-fiqh-web (§4a) ---------------------------------
//
// Container Apps authentication: the platform redirects unauthenticated
// requests to Google and passes the signed-in identity to the app as
// X-MS-CLIENT-PRINCIPAL* headers, so the app contains no auth code. Anyone
// with a Google account is let in; the point is a real person, not a list.
//
// No token store: the app needs only the identity headers, not stored Google
// tokens, and a token store would need a blob container.
resource webAuth 'Microsoft.App/containerApps/authConfigs@2024-03-01' = if (authEnabled) {
  parent: webApp
  name: 'current'
  properties: {
    platform: {
      enabled: true
    }
    globalValidation: {
      unauthenticatedClientAction: 'RedirectToLoginPage'
      redirectToProvider: 'google'
      // Keeps the runbook's health check returning 200. It returns "ok" and
      // nothing else, so leaving it unauthenticated exposes nothing.
      excludedPaths: [
        '/_stcore/health'
      ]
    }
    identityProviders: {
      google: {
        enabled: true
        registration: {
          clientId: googleClientId
          clientSecretSettingName: 'google-client-secret'
        }
        login: {
          scopes: [
            'openid'
            'profile'
            'email'
          ]
        }
      }
    }
    httpSettings: {
      requireHttps: true
      // Behind APIM the app only ever sees its own internal hostname, so
      // sign-in redirects would point somewhere the browser cannot reach.
      // 'Standard' makes the auth layer trust X-Forwarded-Host / -Proto,
      // which the APIM policy sets (§4b).
      forwardProxy: empty(publicHostname) ? null : {
        convention: 'Standard'
      }
    }
  }
}

output environmentDefaultDomain string = environment.properties.defaultDomain
output environmentStaticIp string = environment.properties.staticIp
// Internal ingress still reports an FQDN here; it just resolves only inside
// the VNet once the environment is internal.
output webFqdn string = webApp.properties.configuration.ingress.fqdn
output webInternalFqdn string = '${webAppName}.internal.${environment.properties.defaultDomain}'
