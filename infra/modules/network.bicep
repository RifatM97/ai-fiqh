// VNet for phase 3 (docs/deployment.md §4b): one subnet for the Container Apps
// environment, one for APIM, so APIM can reach an internal-only backend over
// the private network instead of the internet.
//
// Subnet sizes cannot be changed after the environment is created, so the ACA
// subnet is a /23 rather than the /27 minimum: workload-profile environments
// reserve 12 addresses for infrastructure, take one per node on dedicated
// profiles, and temporarily double their requirement during a revision change.

param location string
param vnetName string = 'vnet-aifiqh'

resource nsgApim 'Microsoft.Network/networkSecurityGroups@2023-11-01' = {
  name: 'nsg-apim'
  location: location
  properties: {
    // APIM in External VNet mode will not provision unless these are present.
    // Service tags rather than address ranges, so they survive Azure changing
    // its own IPs.
    securityRules: [
      {
        name: 'apim-in-client-https'
        properties: {
          description: 'Client traffic to the gateway'
          priority: 100
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'Internet'
          sourcePortRange: '*'
          destinationAddressPrefix: 'VirtualNetwork'
          destinationPortRanges: [
            '80'
            '443'
          ]
        }
      }
      {
        name: 'apim-in-management'
        properties: {
          description: 'Required: APIM management endpoint'
          priority: 110
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'ApiManagement'
          sourcePortRange: '*'
          destinationAddressPrefix: 'VirtualNetwork'
          destinationPortRange: '3443'
        }
      }
      {
        name: 'apim-in-load-balancer'
        properties: {
          description: 'Required: Azure infrastructure load balancer'
          priority: 120
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'AzureLoadBalancer'
          sourcePortRange: '*'
          destinationAddressPrefix: 'VirtualNetwork'
          destinationPortRange: '6390'
        }
      }
      {
        name: 'apim-out-storage'
        properties: {
          description: 'Required dependency'
          priority: 100
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'VirtualNetwork'
          sourcePortRange: '*'
          destinationAddressPrefix: 'Storage'
          destinationPortRange: '443'
        }
      }
      {
        name: 'apim-out-sql'
        properties: {
          description: 'Required dependency'
          priority: 110
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'VirtualNetwork'
          sourcePortRange: '*'
          destinationAddressPrefix: 'Sql'
          destinationPortRange: '1433'
        }
      }
      {
        name: 'apim-out-key-vault'
        properties: {
          description: 'Required dependency'
          priority: 120
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'VirtualNetwork'
          sourcePortRange: '*'
          destinationAddressPrefix: 'AzureKeyVault'
          destinationPortRange: '443'
        }
      }
      {
        name: 'apim-out-certificate-validation'
        properties: {
          description: 'Required: CRL/OCSP over HTTP'
          priority: 130
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'VirtualNetwork'
          sourcePortRange: '*'
          destinationAddressPrefix: 'Internet'
          destinationPortRange: '80'
        }
      }
    ]
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2023-11-01' = {
  name: vnetName
  location: location
  properties: {
    addressSpace: {
      addressPrefixes: [
        '10.0.0.0/16'
      ]
    }
    subnets: [
      {
        name: 'snet-aca'
        properties: {
          addressPrefix: '10.0.0.0/23'
          delegations: [
            {
              name: 'aca-environments'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
        }
      }
      {
        name: 'snet-apim'
        properties: {
          addressPrefix: '10.0.4.0/27'
          networkSecurityGroup: {
            id: nsgApim.id
          }
        }
      }
    ]
  }
}

output vnetId string = vnet.id
output vnetName string = vnet.name
output acaSubnetId string = vnet.properties.subnets[0].id
output apimSubnetId string = vnet.properties.subnets[1].id
