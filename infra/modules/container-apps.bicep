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

// Confirmed locally on 2026-09-17 that the Consumption plan's 8 GiB
// per-container ceiling OOM-kills gemma4:12b (reproduced twice, including
// with the tracker's recommended 16,384-token context). So ai-fiqh-ollama
// runs on a Dedicated profile. This names an actual SKU — confirm it is
// offered in the target region before deploying:
//   az containerapp env workload-profile list-supported --location <region> -o table
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
        // minimumCount: 0 lets the dedicated node deallocate once
        // ai-fiqh-ollama has scaled to zero — the closest a Dedicated
        // profile gets to Consumption's idle economics, at the price of a
        // node-level cold start on top of the 56.8s model load (§4c).
        minimumCount: 0
        maximumCount: 1
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
    workloadProfileName: ollamaProfileName
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
          // 16 GiB is ~2x the model's on-disk size — the local test only
          // succeeded once 16 GiB was available. 3 vCPU rather than 4 leaves
          // the dedicated node some capacity for its own system overhead.
          resources: {
            cpu: json('3')
            memory: '16Gi'
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
