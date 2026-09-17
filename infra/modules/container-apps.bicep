// The Container Apps environment plus the two apps from docs/deployment.md
// §4. No custom VNet yet (that's §4b, alongside APIM) — Microsoft-managed
// networking is enough to give ai-fiqh-ollama a real internal-only ingress,
// since `ingress.external` is a per-app setting independent of whether the
// environment itself has a custom VNet.
//
// Known first-deploy quirk, not solved here: a container app's managed
// identity needs its role assignment (AcrPull / Key Vault Secrets User) to
// finish propagating before it can actually pull its image or resolve a
// Key-Vault-backed secret. ARM will accept this template and create
// everything, but the very first revision can fail to start on a fresh
// resource group for that reason — a `az containerapp revision restart` a
// minute or two later resolves it. Not a bug in this template.

param location string
param environmentName string
param logAnalyticsCustomerId string
@secure()
param logAnalyticsPrimarySharedKey string

param acrName string
param acrLoginServer string
param keyVaultName string
param keyVaultUri string

param webImageTag string = 'latest'
param ollamaImageTag string = 'latest'

param azureOpenAiEndpoint string
param azureOpenAiDeployment string

var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
var keyVaultSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: acrName
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

// Workload profiles: 'Consumption' stays the default for ai-fiqh-web.
// ai-fiqh-ollama moves to a Dedicated profile — confirmed locally on
// 2026-09-17 that the Consumption plan's 8 GiB per-container ceiling is not
// a theoretical risk but an actual OOM kill (`llama-server process has
// terminated: signal: killed`), reproduced twice, including with the
// tracker's own recommended 16,384-token context ceiling applied. 8 GiB
// simply isn't enough headroom to load an ~8 GB quantized model plus KV
// cache and runtime overhead.
//
// `dedicatedProfileWorkloadType` names an actual SKU (e.g. 'E4', 'E8') —
// confirm what's currently offered in this region before deploying:
//   az containerapp env workload-profile list-supported --location <region>
// The value below is a placeholder for "a memory-optimized profile with
// real headroom above 8 GiB," not a verified-available SKU name.
param dedicatedProfileWorkloadType string = 'E4'
var ollamaProfileName = 'ollama-profile'

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
        // minimumCount: 0 lets the dedicated node pool itself deallocate
        // when ai-fiqh-ollama has scaled its own replica to 0 — the closest
        // approximation of "scale to zero" a Dedicated profile allows.
        // Expect a real node-level cold start in addition to the
        // container-level one; this is the thing still needing to be
        // measured (docs/deployment.md §4c).
        minimumCount: 0
        maximumCount: 1
      }
    ]
  }
}

// --- ai-fiqh-ollama: the content-filter fallback, scale-to-zero ------------
//
// Moved to the dedicated `ollama-profile` workload profile (see the
// environment resource above) after confirming locally on 2026-09-17 that
// the Consumption plan's 8 GiB ceiling is not enough headroom for
// gemma4:12b — reproducibly OOM-killed at that size, including with the
// tracker's own recommended 16,384-token context ceiling applied. 16 GiB
// requested here is roughly double the model's on-disk footprint, chosen
// as a real safety margin after that finding, not a round-number guess.
resource ollamaApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'ai-fiqh-ollama'
  location: location
  identity: {
    type: 'SystemAssigned'
  }
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
          identity: 'system'
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'ollama'
          image: '${acrLoginServer}/ai-fiqh-ollama:${ollamaImageTag}'
          resources: {
            cpu: json('4')
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

resource ollamaAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, ollamaApp.id, acrPullRoleId)
  scope: acr
  properties: {
    principalId: ollamaApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
  }
}

// --- ai-fiqh-web: the Streamlit app, always warm ---------------------------

resource webApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'ai-fiqh-web'
  location: location
  identity: {
    type: 'SystemAssigned'
  }
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
          identity: 'system'
        }
      ]
      secrets: [
        {
          name: 'voyage-api-key'
          keyVaultUrl: '${keyVaultUri}secrets/voyage-api-key'
          identity: 'system'
        }
        {
          name: 'anthropic-api-key'
          keyVaultUrl: '${keyVaultUri}secrets/anthropic-api-key'
          identity: 'system'
        }
        {
          name: 'azure-openai-api-key'
          keyVaultUrl: '${keyVaultUri}secrets/azure-openai-api-key'
          identity: 'system'
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

resource webAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, webApp.id, acrPullRoleId)
  scope: acr
  properties: {
    principalId: webApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
  }
}

resource webKeyVaultSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, webApp.id, keyVaultSecretsUserRoleId)
  scope: keyVault
  properties: {
    principalId: webApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
  }
}

output environmentId string = environment.id
output environmentDefaultDomain string = environment.properties.defaultDomain
output webFqdn string = webApp.properties.configuration.ingress.fqdn
output webPrincipalId string = webApp.identity.principalId
output ollamaPrincipalId string = ollamaApp.identity.principalId
