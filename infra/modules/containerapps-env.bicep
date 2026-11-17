// Container Apps 환경 (VNet 통합, 소비 기반). Microsoft.App 공급자 등록이 필요하다.
param location string
param tags object
param envName string
param logName string
param vnetName string
param subnetName string = 'snet-aca'

resource log 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logName
}

resource vnet 'Microsoft.Network/virtualNetworks@2023-11-01' existing = {
  name: vnetName
}

resource subnet 'Microsoft.Network/virtualNetworks/subnets@2023-11-01' existing = {
  parent: vnet
  name: subnetName
}

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: envName
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: log.properties.customerId
        sharedKey: log.listKeys().primarySharedKey
      }
    }
    vnetConfiguration: {
      infrastructureSubnetId: subnet.id
      internal: false
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
  }
}

output envId string = env.id
output defaultDomain string = env.properties.defaultDomain
