// AI-Fiqh — Phase 1 (docs/deployment.md §4): core compute only.
// No auth, no VNet/APIM, no CI/CD yet — see docs/deployment.md for what's
// deliberately deferred and why.
//
// Deployed in two stages, because the container apps cannot be created
// until their images are in ACR and their secrets are in Key Vault — and
// ACR and Key Vault are created by this same template:
//
//   Stage 1  deployApps=false  Log Analytics, ACR, Key Vault (+ your secrets role)
//            → push images to ACR, set the three Key Vault secrets
//   Stage 2  deployApps=true   Container Apps environment, identities, both apps
//
// Full command sequence: docs/deployment.md §7 (runbook).

targetScope = 'resourceGroup'

param location string = 'uksouth'
param namePrefix string = 'aifiqh'

@description('false for stage 1 (registry + vault only), true for stage 2 (the apps). See header.')
param deployApps bool = false

@description('Object ID of whoever sets the Key Vault secrets — granted Key Vault Secrets Officer. `az ad signed-in-user show --query id -o tsv`.')
param deployerPrincipalId string = ''

param webImageTag string = 'v1'
param ollamaImageTag string = 'v1'

@description('Existing Azure OpenAI resource this deployment calls — provisioning it is out of scope here (docs/deployment.md §3).')
param azureOpenAiEndpoint string

param azureOpenAiDeployment string

param dedicatedProfileWorkloadType string = 'E4'

@description('Google OAuth client ID for sign-in (docs/deployment.md §4a). Empty leaves the app open. Not a secret; the client secret goes only to Key Vault as google-client-secret.')
param googleClientId string = ''

@description('1 keeps the GPU fallback warm (billed continuously); 0 stops the cost but the fallback cannot answer within the 240s ingress timeout from cold.')
@minValue(0)
@maxValue(1)
param ollamaMinReplicas int = 1

var uniqueSuffix = substring(uniqueString(resourceGroup().id), 0, 6)

module logAnalytics 'modules/log-analytics.bicep' = {
  name: 'log-analytics'
  params: {
    location: location
    name: 'law-${namePrefix}'
  }
}

module acr 'modules/acr.bicep' = {
  name: 'acr'
  params: {
    location: location
    name: 'acr${namePrefix}${uniqueSuffix}'
  }
}

module keyVault 'modules/keyvault.bicep' = {
  name: 'keyvault'
  params: {
    location: location
    name: 'kv-${namePrefix}-${uniqueSuffix}'
    deployerPrincipalId: deployerPrincipalId
  }
}

module containerApps 'modules/container-apps.bicep' = if (deployApps) {
  name: 'container-apps'
  params: {
    location: location
    environmentName: 'cae-${namePrefix}'
    logAnalyticsCustomerId: logAnalytics.outputs.customerId
    logAnalyticsPrimarySharedKey: logAnalytics.outputs.primarySharedKey
    acrName: acr.outputs.name
    acrLoginServer: acr.outputs.loginServer
    keyVaultName: keyVault.outputs.name
    keyVaultUri: keyVault.outputs.vaultUri
    webImageTag: webImageTag
    ollamaImageTag: ollamaImageTag
    azureOpenAiEndpoint: azureOpenAiEndpoint
    azureOpenAiDeployment: azureOpenAiDeployment
    dedicatedProfileWorkloadType: dedicatedProfileWorkloadType
    ollamaMinReplicas: ollamaMinReplicas
    googleClientId: googleClientId
  }
}

output acrName string = acr.outputs.name
output acrLoginServer string = acr.outputs.loginServer
output keyVaultName string = keyVault.outputs.name
output webFqdn string = deployApps ? containerApps!.outputs.webFqdn : ''
