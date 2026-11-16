// Container Registry (Basic). AcrPull 역할을 할당할 권한이 없어 관리자 계정을 쓰고, 비밀번호는 Key Vault에만 둔다.
param location string
param tags object
param acrName string
param keyVaultName string

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: true
    publicNetworkAccess: 'Enabled'
  }
}

resource kv 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource userSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: 'acr-username'
  properties: {
    value: acr.listCredentials().username
  }
}

resource passwordSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: 'acr-password'
  properties: {
    value: acr.listCredentials().passwords[0].value
  }
}

output loginServer string = acr.properties.loginServer
