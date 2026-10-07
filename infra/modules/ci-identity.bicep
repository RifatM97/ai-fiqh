// Identity GitHub Actions deploys as (docs/deployment.md §8).
//
// A user-assigned managed identity with a federated credential, rather than an
// Entra app registration: the Vodafone tenant blocks users from creating app
// registrations (`allowedToCreateApps: false`), but a managed identity is an
// Azure resource governed by Azure RBAC, so it can be created here.
//
// No secret exists anywhere. GitHub mints a short-lived OIDC token for a job,
// Entra checks it against the credential below, and issues an Azure token in
// exchange. The subject pins that to one repository *and* one GitHub
// environment, so a workflow on another branch or in a fork gets nothing.
//
// Deliberately cannot deploy infrastructure: it holds no role-assignment
// rights, and the Bicep template re-applies role assignments on every run. The
// user's RBAC Administrator role is conditioned to block granting the roles
// that would allow it, which also makes this the safer split — CI ships
// images, infrastructure changes stay a reviewed manual deploy.

param location string
param acrName string
param webAppName string
param ollamaAppName string

@description('owner/repo the federated credential trusts.')
param githubRepository string

@description('GitHub environment deploy jobs run in. Its branch policy decides which branches can deploy.')
param githubEnvironment string = 'production'

// Looked up with `az role definition list --name <role>`, not from memory —
// a mistyped Reader ID failed the first deploy (RoleDefinitionDoesNotExist).
var contributorRoleId = 'b24988ac-6180-42a0-ab88-20f7382dd24c'
var readerRoleId = 'acdd72a7-3385-48ef-bd42-f606fba81ae7'

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: acrName
}

resource webApp 'Microsoft.App/containerApps@2024-03-01' existing = {
  name: webAppName
}

resource ollamaApp 'Microsoft.App/containerApps@2024-03-01' existing = {
  name: ollamaAppName
}

resource ciIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-ai-fiqh-github'
  location: location
}

resource githubTrust 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = {
  parent: ciIdentity
  name: 'github-${githubEnvironment}'
  properties: {
    issuer: 'https://token.actions.githubusercontent.com'
    subject: 'repo:${githubRepository}:environment:${githubEnvironment}'
    audiences: [
      'api://AzureADTokenExchange'
    ]
  }
}

// Contributor on the registry, not AcrPush: `az acr build` runs an ACR Task,
// and AcrPush does not cover scheduling one.
resource acrContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, ciIdentity.id, contributorRoleId)
  scope: acr
  properties: {
    principalId: ciIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', contributorRoleId)
  }
}

// Scoped to the two apps it updates, not the resource group: it can roll a new
// image onto them and nothing else.
resource webContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(webApp.id, ciIdentity.id, contributorRoleId)
  scope: webApp
  properties: {
    principalId: ciIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', contributorRoleId)
  }
}

resource ollamaContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(ollamaApp.id, ciIdentity.id, contributorRoleId)
  scope: ollamaApp
  properties: {
    principalId: ciIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', contributorRoleId)
  }
}

// Read-only on the resource group, for the CLI's own lookups (the environment
// behind an app, revision status) during a deploy.
resource rgReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, ciIdentity.id, readerRoleId)
  properties: {
    principalId: ciIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', readerRoleId)
  }
}

output clientId string = ciIdentity.properties.clientId
output principalId string = ciIdentity.properties.principalId
