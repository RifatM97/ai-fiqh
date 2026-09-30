// Private DNS for an internal Container Apps environment (docs/deployment.md
// §4b). An internal environment on a custom VNet gets no DNS of its own, so
// without this APIM cannot resolve ai-fiqh-web.internal.<defaultDomain> and
// every call fails to reach the backend.
//
// The zone is named after the environment's own default domain, and one
// wildcard record points every app in it at the environment's static IP.

param environmentDefaultDomain string
param environmentStaticIp string
param vnetId string

resource zone 'Microsoft.Network/privateDnsZones@2020-06-01' = {
  name: environmentDefaultDomain
  location: 'global'
}

resource link 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = {
  parent: zone
  name: 'link-to-vnet'
  location: 'global'
  properties: {
    virtualNetwork: {
      id: vnetId
    }
    // No autoregistration: the records here are written by this template, not
    // by VMs registering themselves.
    registrationEnabled: false
  }
}

resource wildcard 'Microsoft.Network/privateDnsZones/A@2020-06-01' = {
  parent: zone
  name: '*'
  properties: {
    ttl: 3600
    aRecords: [
      {
        ipv4Address: environmentStaticIp
      }
    ]
  }
}

output zoneName string = zone.name
