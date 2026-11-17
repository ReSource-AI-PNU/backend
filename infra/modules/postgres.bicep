// PostgreSQL Flexible Server: 근거 레지스트리, 조직, 흐름, 대화, 노선. PostGIS와 pgvector를 허용한다.
param location string
param tags object
param serverName string
param skuName string
param skuTier string
@description('가용 영역. 비우면 Azure가 고른다. 용량 문제로 실패하면 1, 2, 3을 차례로 넣어 본다.')
param availabilityZone string = ''
param adminLogin string = 'rsaiadmin'
@secure()
param adminPassword string
param databaseName string = 'resource_ai'
param keyVaultName string

resource server 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: serverName
  location: location
  tags: tags
  sku: {
    name: skuName
    tier: skuTier
  }
  properties: {
    version: '16'
    availabilityZone: empty(availabilityZone) ? null : availabilityZone
    administratorLogin: adminLogin
    administratorLoginPassword: adminPassword
    storage: {
      storageSizeGB: 32
      autoGrow: 'Disabled'
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
    network: {
      publicNetworkAccess: 'Enabled'
    }
    authConfig: {
      passwordAuth: 'Enabled'
      activeDirectoryAuth: 'Disabled'
    }
  }
}

// 서버 작업은 겹치면 실패하므로 차례로 건다.
resource allowAzure 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = {
  parent: server
  name: 'AllowAllAzureServicesAndResourcesWithinAzureIps'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

resource extensions 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2024-08-01' = {
  parent: server
  name: 'azure.extensions'
  properties: {
    value: 'POSTGIS,VECTOR,PG_TRGM'
    source: 'user-override'
  }
  dependsOn: [
    allowAzure
  ]
}

resource db 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: server
  name: databaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
  dependsOn: [
    extensions
  ]
}

resource kv 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource passwordSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: 'postgres-admin-password'
  properties: {
    value: adminPassword
  }
}

resource urlSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: 'database-url'
  properties: {
    value: 'postgresql://${adminLogin}:${uriComponent(adminPassword)}@${server.properties.fullyQualifiedDomainName}:5432/${databaseName}?sslmode=require'
  }
  dependsOn: [
    db
  ]
}

output fqdn string = server.properties.fullyQualifiedDomainName
