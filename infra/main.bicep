// AI-Fiqh infrastructure (docs/deployment.md §4). CI/CD is still deferred.
//
// Deployed in stages, because things depend on each other in ways one
// deployment cannot resolve: the container apps need their images in ACR and
// their secrets in Key Vault, and ACR and Key Vault are created here too.
//
//   Stage 1  deployApps=false  Log Analytics, ACR, Key Vault (+ your secrets role)
//            → push images to ACR, set the Key Vault secrets
//   Stage 2  deployApps=true   Container Apps environment, identities, both apps,
//                              and sign-in once googleClientId is set (§7b)
//   Stage 3  useVnet=true      VNet + an internal environment behind it (§7c)
//            deployApim=true   APIM as the only public entry point (slow: 30-45+ min)
//
// Full command sequences: docs/deployment.md §7, §7b, §7c.

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

// --- Phase 3: VNet + APIM (§4b) --------------------------------------------

@description('true creates the VNet and puts the environment inside it. A Container Apps environment cannot gain a VNet after creation, so this needs a NEW environmentName.')
param useVnet bool = false

@description('true removes the environment public endpoint (needs something inside the VNet to front it). false keeps the app publicly reachable while still in the VNet — the default, and what makes private endpoints possible later. Fixed at creation.')
param environmentInternal bool = false

@description('Environment name. Change it together with useVnet: the VNet cannot be added to the existing one.')
param environmentName string = 'cae-aifiqh'

@description('App names. Change these alongside environmentName to build the new environment while the old one keeps serving — app names are unique per resource group, and an existing app cannot be moved between environments.')
param webAppName string = 'ai-fiqh-web'

param ollamaAppName string = 'ai-fiqh-ollama'

@description('CIDRs allowed to reach the web app (§4d). Empty leaves it open.')
param allowedIpRanges array = []

@description('true creates API Management in the VNet. Provisioning takes 30-45+ minutes. Requires useVnet.')
param deployApim bool = false

param apimPublisherName string = 'AI-Fiqh'
param apimPublisherEmail string = ''

@description('Requests per minute per client IP at the gateway before 429 (§4b).')
param apimRateLimitCalls int = 60

@description('Overrides the hostname sign-in redirects are built from. Defaults to APIM’s gateway hostname when APIM is deployed.')
param publicHostname string = ''

// --- CI/CD (§8) ----------------------------------------------------------------

@description('true creates the identity GitHub Actions deploys as, trusted via a federated credential. Requires deployApps (it is granted rights on the apps).')
param deployCiIdentity bool = false

@description('owner/repo GitHub Actions runs from.')
param githubRepository string = 'RifatM97/ai-fiqh'

@description('Numeric owner and repository IDs for GitHub immutable OIDC subjects (see ci-identity.bicep).')
param githubOwnerId string = ''
param githubRepositoryId string = ''

var uniqueSuffix = substring(uniqueString(resourceGroup().id), 0, 6)
var apimName = 'apim-${namePrefix}-${uniqueSuffix}'
var effectivePublicHostname = !empty(publicHostname)
  ? publicHostname
  : (deployApim ? '${apimName}.azure-api.net' : '')

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

module network 'modules/network.bicep' = if (useVnet) {
  name: 'network'
  params: {
    location: location
    vnetName: 'vnet-${namePrefix}'
  }
}

module containerApps 'modules/container-apps.bicep' = if (deployApps) {
  name: 'container-apps'
  params: {
    location: location
    environmentName: environmentName
    webAppName: webAppName
    ollamaAppName: ollamaAppName
    allowedIpRanges: allowedIpRanges
    infrastructureSubnetId: useVnet ? network!.outputs.acaSubnetId : ''
    environmentInternal: environmentInternal
    publicHostname: effectivePublicHostname
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

// Without this the apps are unreachable: an internal environment on a custom
// VNet has no DNS of its own, so APIM cannot resolve the backend (§4b).
module privateDns 'modules/private-dns.bicep' = if (useVnet && deployApps && environmentInternal) {
  name: 'private-dns'
  params: {
    environmentDefaultDomain: containerApps!.outputs.environmentDefaultDomain
    environmentStaticIp: containerApps!.outputs.environmentStaticIp
    vnetId: network!.outputs.vnetId
  }
}

module apim 'modules/apim.bicep' = if (deployApim && useVnet && deployApps) {
  name: 'apim'
  params: {
    location: location
    apimName: apimName
    apimSubnetId: network!.outputs.apimSubnetId
    publisherName: apimPublisherName
    publisherEmail: apimPublisherEmail
    webInternalFqdn: containerApps!.outputs.webInternalFqdn
    rateLimitCalls: apimRateLimitCalls
  }
  dependsOn: [
    privateDns
  ]
}

module ciIdentity 'modules/ci-identity.bicep' = if (deployCiIdentity && deployApps) {
  name: 'ci-identity'
  params: {
    location: location
    acrName: acr.outputs.name
    webAppName: webAppName
    ollamaAppName: ollamaAppName
    githubRepository: githubRepository
    githubOwnerId: githubOwnerId
    githubRepositoryId: githubRepositoryId
  }
  // The role assignments target the apps by name, so they must exist first.
  dependsOn: [
    containerApps
  ]
}

output acrName string = acr.outputs.name
output acrLoginServer string = acr.outputs.loginServer
output keyVaultName string = keyVault.outputs.name
// Internal once useVnet is set: resolvable only inside the VNet.
output webFqdn string = deployApps ? containerApps!.outputs.webFqdn : ''
output publicUrl string = deployApim ? apim!.outputs.gatewayUrl : (deployApps ? 'https://${containerApps!.outputs.webFqdn}' : '')
output signInRedirectUri string = deployApim ? '${apim!.outputs.gatewayUrl}/.auth/login/google/callback' : ''
// The value GitHub needs as the AZURE_CLIENT_ID repository variable.
output ciClientId string = (deployCiIdentity && deployApps) ? ciIdentity!.outputs.clientId : ''
