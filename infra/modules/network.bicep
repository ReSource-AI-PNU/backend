// VNet: Container Apps와 Ollama VM이 내부에서만 통신한다.
param location string
param tags object
param vnetName string
param nsgVmName string
@description('Microsoft.App 공급자가 등록된 뒤에만 위임을 건다.')
param delegateContainerAppsSubnet bool = false

resource nsgVm 'Microsoft.Network/networkSecurityGroups@2023-11-01' = {
  name: nsgVmName
  location: location
  tags: tags
  properties: {
    securityRules: [
      {
        name: 'AllowOllamaFromVnet'
        properties: {
          description: 'Ollama(11434)는 VNet 안에서만 받는다'
          priority: 100
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'VirtualNetwork'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '11434'
        }
      }
      {
        name: 'DenyInternetInbound'
        properties: {
          description: '인터넷에서 들어오는 모든 연결을 막는다'
          priority: 4000
          direction: 'Inbound'
          access: 'Deny'
          protocol: '*'
          sourceAddressPrefix: 'Internet'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
        }
      }
    ]
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2023-11-01' = {
  name: vnetName
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [
        '10.60.0.0/16'
      ]
    }
    subnets: [
      {
        name: 'snet-aca'
        properties: {
          addressPrefix: '10.60.0.0/23'
          delegations: delegateContainerAppsSubnet ? [
            {
              name: 'aca'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ] : []
        }
      }
      {
        name: 'snet-vm'
        properties: {
          addressPrefix: '10.60.2.0/24'
          networkSecurityGroup: {
            id: nsgVm.id
          }
        }
      }
      {
        name: 'snet-pe'
        properties: {
          addressPrefix: '10.60.3.0/24'
        }
      }
    ]
  }
}

output vnetId string = vnet.id
