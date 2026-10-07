// Key Vault — provisioned empty, deliberately. Actual secret values
// (VOYAGE_API_KEY, ANTHROPIC_API_KEY, AZURE_OPENAI_API_KEY) are set with
// `az keyvault secret set` from the existing .env after this deploys, never
// written into Bicep or a parameters file. RBAC authorization (not the
// legacy access-policy model) so access is a role assignment, same pattern
// as everything else in this stack — see docs/deployment.md §4.

param location string
param name string
param tenantId string = subscription().tenantId

// Under RBAC authorization, owning the resource group does not grant
// data-plane access: whoever runs `az keyvault secret set` needs a secrets
// role on the vault itself. Pass the deployer's object ID to grant it here.
param deployerPrincipalId string = ''

var keyVaultSecretsOfficerRoleId = 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'

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

resource deployerSecretsOfficer 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(deployerPrincipalId)) {
  name: guid(keyVault.id, deployerPrincipalId, keyVaultSecretsOfficerRoleId)
  scope: keyVault
  properties: {
    principalId: deployerPrincipalId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsOfficerRoleId)
  }
}

output id string = keyVault.id
output name string = keyVault.name
output vaultUri string = keyVault.properties.vaultUri
