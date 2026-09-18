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

// Two findings, in order, drove where ai-fiqh-ollama runs:
//
// 1. 2026-09-17, locally: the Consumption plan's 8 GiB per-container ceiling
//    OOM-kills gemma4:12b. So a Dedicated CPU profile (E4, 4 cores/32 GiB).
// 2. 2026-09-18, in Azure: E4 runs it, but CPU-only inference processes the
//    prompt at 6.7 tok/s — ~12 minutes for a 4,685-token RAG prompt, and the
//    Container Apps ingress cuts the request off at 4m0s (the 504 seen in
//    testing). Unusable regardless of the timeout.
//
// So it runs on a serverless GPU profile. Consumption-GPU keeps scale-to-zero
// (no minimumCount/maximumCount — those are Dedicated-only), and a T4's 16 GB
// of VRAM comfortably holds this ~8 GB quantized model.
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

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: environmentName
  location: location
  properties: {
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

// --- ai-fiqh-ollama: the content-filter fallback, scale-to-zero ------------

resource ollamaApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'ai-fiqh-ollama'
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
        minReplicas: 0
        maxReplicas: 1
      }
    }
  }
}

// --- ai-fiqh-web: the Streamlit app, always warm ---------------------------

resource webApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'ai-fiqh-web'
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
      // External for now — becomes internal-only once APIM is the sole
      // public entry point, per docs/deployment.md §4b. Deliberate interim
      // state for this phase, not an oversight.
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
      }
      registries: [
        {
          server: acrLoginServer
          identity: webIdentity.id
        }
      ]
      secrets: [
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
      ]
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

output environmentDefaultDomain string = environment.properties.defaultDomain
output webFqdn string = webApp.properties.configuration.ingress.fqdn
