// Azure Container Registry — Basic SKU (sufficient at this scale). Admin
// user stays disabled: image pulls are authenticated via each container
// app's managed identity + an AcrPull role assignment (see
// container-apps.bicep), never a shared admin credential.

param location string
param name string

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: name
  location: location
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
  }
}

output id string = acr.id
output name string = acr.name
output loginServer string = acr.properties.loginServer
