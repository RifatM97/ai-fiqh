// AI-Fiqh — Phase 1 (docs/deployment.md §4): core compute only.
// No auth, no VNet/APIM, no CI/CD yet — see docs/deployment.md for what's
// deliberately deferred and why.
//
// Deploy (resource group created separately, not by this template):
//   az deployment group create \
//     --resource-group <rg> \
//     --template-file infra/main.bicep \
//     --parameters infra/main.bicepparam
//
// Then populate the Key Vault (never via Bicep — see modules/keyvault.bicep):
//   az keyvault secret set --vault-name <kv-name> --name voyage-api-key --value <...>
//   az keyvault secret set --vault-name <kv-name> --name anthropic-api-key --value <...>
//   az keyvault secret set --vault-name <kv-name> --name azure-openai-api-key --value <...>
//
// And only after both images exist in ACR (docker build + az acr login +
// docker push, or `az acr build`) will the container apps' revisions
// actually start — provisioning them first is expected to leave the
// revisions in a failed state until the images are pushed.

targetScope = 'resourceGroup'

param location string = 'uksouth'
param namePrefix string = 'aifiqh'

param webImageTag string = 'latest'
param ollamaImageTag string = 'latest'

@description('Existing Azure OpenAI resource this deployment calls — provisioning it is out of scope here (docs/deployment.md §3).')
param azureOpenAiEndpoint string

param azureOpenAiDeployment string

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
  }
}

module containerApps 'modules/container-apps.bicep' = {
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
  }
}

output acrLoginServer string = acr.outputs.loginServer
output keyVaultName string = keyVault.outputs.name
output webFqdn string = containerApps.outputs.webFqdn
