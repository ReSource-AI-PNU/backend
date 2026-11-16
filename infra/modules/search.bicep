// Azure AI Search: 법령·근거 색인 (하이브리드 + 시맨틱 랭커). 중지할 수 없어 켜 두는 동안 과금된다.
param location string
param tags object
param searchName string
param skuName string = 'basic'
param keyVaultName string

resource search 'Microsoft.Search/searchServices@2023-11-01' = {
  name: searchName
  location: location
  tags: tags
  sku: {
    name: skuName
  }
  properties: {
    replicaCount: 1
    partitionCount: 1
    hostingMode: 'default'
    publicNetworkAccess: 'enabled'
    semanticSearch: 'free'
    disableLocalAuth: false
    authOptions: {
      aadOrApiKey: {
        aadAuthFailureMode: 'http401WithBearerChallenge'
      }
    }
  }
}

resource kv 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource adminKey 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: 'search-admin-key'
  properties: {
    value: search.listAdminKeys().primaryKey
  }
}

output endpoint string = 'https://${search.name}.search.windows.net'
