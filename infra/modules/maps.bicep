// Azure Maps (Gen2): 도로 거리. Microsoft.Maps 공급자 등록이 필요하다.
// 한국 경로는 최신 Route API에서만 되고, 계정에서 글로벌 데이터 처리 동의를 켜야 한다.
param tags object
param mapsName string
param keyVaultName string

resource maps 'Microsoft.Maps/accounts@2023-06-01' = {
  name: mapsName
  location: 'global'
  tags: tags
  kind: 'Gen2'
  sku: {
    name: 'G2'
  }
  properties: {
    disableLocalAuth: false
  }
}

resource kv 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource key 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: 'maps-key'
  properties: {
    value: maps.listKeys().primaryKey
  }
}
