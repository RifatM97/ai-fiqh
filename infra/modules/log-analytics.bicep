// Log Analytics workspace — a hard dependency of any Container Apps
// environment (docs/deployment.md §4), not an optional add-on. Application
// Insights is a deliberate follow-on phase, not bundled here.

param location string
param name string

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: name
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

output id string = workspace.id
output name string = workspace.name
output customerId string = workspace.properties.customerId

@secure()
output primarySharedKey string = workspace.listKeys().primarySharedKey
