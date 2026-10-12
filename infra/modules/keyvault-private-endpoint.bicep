// Private path to Key Vault (docs/deployment.md §4e).
//
// The endpoint gives the vault an address inside snet-pe. The private DNS
// zone, linked to the VNet, makes the vault's ordinary hostname
// (kv-….vault.azure.net) resolve to that address for anything inside the VNet
// — and only there; elsewhere it still resolves publicly. Nothing has to
// change its configuration to use the private path.
//
// This module only adds the path. Closing the public one is a separate switch
// in keyvault.bicep, deployed afterwards, so it can be tested on its own.

param location string
param keyVaultId string
param keyVaultName string
param subnetId string
param vnetId string

// Fixed by Azure for Key Vault; the zone name has to be exactly this.
var zoneName = 'privatelink.vaultcore.azure.net'

resource zone 'Microsoft.Network/privateDnsZones@2020-06-01' = {
  name: zoneName
  location: 'global'
}

resource zoneLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = {
  parent: zone
  name: 'link-to-vnet'
  location: 'global'
  properties: {
    virtualNetwork: {
      id: vnetId
    }
    registrationEnabled: false
  }
}

resource endpoint 'Microsoft.Network/privateEndpoints@2023-11-01' = {
  name: 'pe-${keyVaultName}'
  location: location
  properties: {
    subnet: {
      id: subnetId
    }
    privateLinkServiceConnections: [
      {
        name: 'vault'
        properties: {
          privateLinkServiceId: keyVaultId
          groupIds: [
            'vault'
          ]
        }
      }
    ]
  }
}

// Writes the endpoint's A record into the zone and keeps it in sync if the
// endpoint's address ever changes — no hand-maintained DNS record.
resource zoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2023-11-01' = {
  parent: endpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'vault'
        properties: {
          privateDnsZoneId: zone.id
        }
      }
    ]
  }
  dependsOn: [
    zoneLink
  ]
}

output privateEndpointId string = endpoint.id
