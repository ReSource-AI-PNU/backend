// Foundry(AI Services) + 프로젝트 + 채팅 모델, Document Intelligence, Content Safety
// 역할 할당 권한이 없어 키 인증을 쓰고, 키는 Key Vault에만 둔다.
param location string
param tags object
param foundryName string
param projectName string
param docIntelName string
param contentSafetyName string
param chatModelName string
param chatModelVersion string
param chatModelCapacity int
param keyVaultName string

resource foundry 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: foundryName
  location: location
  tags: tags
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    allowProjectManagement: true
    customSubDomainName: foundryName
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: false
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: foundry
  name: projectName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: 'Re:Source AI'
    description: '회사 측 에이전트와 업체 측 에이전트 (Foundry Agent Service)'
  }
}

resource chat 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: foundry
  name: chatModelName
  sku: {
    name: 'GlobalStandard'
    capacity: chatModelCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: chatModelName
      version: chatModelVersion
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
  dependsOn: [
    project
  ]
}

resource docIntel 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: docIntelName
  location: location
  tags: tags
  kind: 'FormRecognizer'
  sku: {
    name: 'S0'
  }
  properties: {
    customSubDomainName: docIntelName
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: false
  }
}

resource contentSafety 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: contentSafetyName
  location: location
  tags: tags
  kind: 'ContentSafety'
  sku: {
    name: 'S0'
  }
  properties: {
    customSubDomainName: contentSafetyName
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: false
  }
}

resource kv 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource foundryKey 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: 'foundry-api-key'
  properties: {
    value: foundry.listKeys().key1
  }
}

resource docIntelKey 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: 'docintel-key'
  properties: {
    value: docIntel.listKeys().key1
  }
}

resource contentSafetyKey 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: 'contentsafety-key'
  properties: {
    value: contentSafety.listKeys().key1
  }
}

output foundryEndpoint string = foundry.properties.endpoint
output projectEndpoint string = 'https://${foundryName}.services.ai.azure.com/api/projects/${projectName}'
output chatDeployment string = chat.name
output docIntelEndpoint string = docIntel.properties.endpoint
output contentSafetyEndpoint string = contentSafety.properties.endpoint
