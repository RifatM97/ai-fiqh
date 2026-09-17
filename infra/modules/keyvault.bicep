// Key Vault — provisioned empty, deliberately. Actual secret values
// (VOYAGE_API_KEY, ANTHROPIC_API_KEY, AZURE_OPENAI_API_KEY) are set with
// `az keyvault secret set` from the existing .env after this deploys, never
// written into Bicep or a parameters file. RBAC authorization (not the
// legacy access-policy model) so access is a role assignment, same pattern
// as everything else in this stack — see docs/deployment.md §4.

param location string
param name string
param tenantId string = subscription().tenantId

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: name
  location: location
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
  }
}

output id string = keyVault.id
output name string = keyVault.name
output vaultUri string = keyVault.properties.vaultUri
