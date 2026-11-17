// Re:Source AI 인프라 (리소스 그룹 범위)
// 묶음: base(기반), ai(AI·데이터), ollama(GPU VM), ml(Azure ML·Fabric)
// 배포: infra/deploy-infra.sh what-if|apply <묶음>
targetScope = 'resourceGroup'

@description('리소스 지역. 기본은 리소스 그룹 지역.')
param location string = resourceGroup().location

@description('환경 이름. 태그와 리소스 이름에 쓴다.')
param env string = 'demo'

@description('리소스 이름 접두어')
param prefix string = 'rsai'

// ── 묶음 선택 ───────────────────────────────────────────────
param deployBase bool = false
param deployAi bool = false
param deployOllama bool = false
param deployMl bool = false

// ── 공급자 등록이나 할당량이 풀려야 켤 수 있는 것 ─────────────
@description('Container Apps 환경. Microsoft.App 공급자 등록이 필요하다.')
param enableContainerApps bool = false
@description('Azure Maps. Microsoft.Maps 공급자 등록이 필요하다.')
param enableMaps bool = false
@description('Fabric 용량. Microsoft.Fabric 공급자 등록이 필요하다.')
param enableFabric bool = false
@description('Azure ML GPU 클러스터. 지금 할당량은 H100 계열뿐이다.')
param enableGpuCluster bool = false

// ── 기반 ────────────────────────────────────────────────────
@description('Key Vault 접근 정책을 받을 배포자 objectId (역할 할당 권한이 없어 접근 정책 방식을 쓴다)')
param deployerObjectId string

@secure()
@description('PostgreSQL 관리자 비밀번호. deploy-infra.sh가 만들어 Key Vault에만 둔다.')
param postgresAdminPassword string = ''

param postgresSku string = 'Standard_B1ms'
param postgresTier string = 'Burstable'
param postgresZone string = ''
@description('PostgreSQL 지역. East US 2는 이 구독에서 B1ms를 거부해(SkuNotAvailable, 영역 1~3 모두) Central US에 둔다.')
param postgresLocation string = 'centralus'

// ── AI·데이터 ───────────────────────────────────────────────
param chatModelName string = 'gpt-5-mini'
param chatModelVersion string = '2025-08-07'
@description('채팅 모델 용량(천 TPM). 구독 전체가 나눠 쓰는 할당량이라 작게 잡는다.')
param chatModelCapacity int = 200
param searchSku string = 'basic'
@description('AI Search 지역. East US 2는 신규 서비스 용량이 없어(InsufficientResourcesAvailable) Central US에 둔다.')
param searchLocation string = 'centralus'

// ── Ollama ──────────────────────────────────────────────────
param ollamaVmSize string = 'Standard_NV6ads_A10_v5'
param ollamaPullModels array = [
  'bge-m3'
]
@secure()
param ollamaSshPublicKey string = ''

// ── ML·Fabric ───────────────────────────────────────────────
@description('재사용할 기존 Azure ML 작업 영역')
param mlWorkspaceName string = 'team-workspace'
param mlCpuVmSize string = 'Standard_D4a_v4'
param mlGpuVmSize string = 'Standard_NC40ads_H100_v5'
param fabricAdmins array = []

var suffix = take(uniqueString(resourceGroup().id), 6)
var tags = {
  project: 'resource-ai'
  env: env
  'managed-by': 'agent'
}
var names = {
  vnet: 'vnet-${prefix}-${env}'
  nsgVm: 'nsg-${prefix}-${env}-vm'
  log: 'log-${prefix}-${env}'
  appInsights: 'appi-${prefix}-${env}'
  acr: 'acr${prefix}${env}${suffix}'
  keyVault: 'kv-${prefix}-${env}-${suffix}'
  storage: 'st${prefix}${env}${suffix}'
  // 지역을 바꿔 다시 만들 때 이름이 겹치지 않도록 지역을 이름 해시에 넣는다
  postgres: 'psql-${prefix}-${env}-${take(uniqueString(resourceGroup().id, postgresLocation), 6)}'
  foundry: 'aif-${prefix}-${env}-${suffix}'
  foundryProject: 'resource-ai'
  docIntel: 'di-${prefix}-${env}-${suffix}'
  contentSafety: 'cs-${prefix}-${env}-${suffix}'
  search: 'srch-${prefix}-${env}-${suffix}'
  maps: 'maps-${prefix}-${env}-${suffix}'
  containerAppsEnv: 'cae-${prefix}-${env}'
  ollamaVm: 'vm-${prefix}-${env}-ollama'
  fabric: 'fab${prefix}${env}${suffix}'
}

// ── 기반 ────────────────────────────────────────────────────
module network 'modules/network.bicep' = if (deployBase) {
  name: 'rsai-network'
  params: {
    location: location
    tags: tags
    vnetName: names.vnet
    nsgVmName: names.nsgVm
    delegateContainerAppsSubnet: enableContainerApps
  }
}

module keyVault 'modules/keyvault.bicep' = if (deployBase) {
  name: 'rsai-keyvault'
  params: {
    location: location
    tags: tags
    keyVaultName: names.keyVault
    deployerObjectId: deployerObjectId
  }
}

module observability 'modules/observability.bicep' = if (deployBase) {
  name: 'rsai-observability'
  params: {
    location: location
    tags: tags
    logName: names.log
    appInsightsName: names.appInsights
    keyVaultName: names.keyVault
  }
  dependsOn: [
    keyVault
  ]
}

module registry 'modules/registry.bicep' = if (deployBase) {
  name: 'rsai-registry'
  params: {
    location: location
    tags: tags
    acrName: names.acr
    keyVaultName: names.keyVault
  }
  dependsOn: [
    keyVault
  ]
}

module storage 'modules/storage.bicep' = if (deployBase) {
  name: 'rsai-storage'
  params: {
    location: location
    tags: tags
    storageName: names.storage
    keyVaultName: names.keyVault
  }
  dependsOn: [
    keyVault
  ]
}

module postgres 'modules/postgres.bicep' = if (deployBase) {
  name: 'rsai-postgres'
  params: {
    location: postgresLocation
    tags: tags
    serverName: names.postgres
    skuName: postgresSku
    skuTier: postgresTier
    availabilityZone: postgresZone
    adminPassword: postgresAdminPassword
    keyVaultName: names.keyVault
  }
  dependsOn: [
    keyVault
  ]
}

module containerAppsEnv 'modules/containerapps-env.bicep' = if (deployBase && enableContainerApps) {
  name: 'rsai-containerapps-env'
  params: {
    location: location
    tags: tags
    envName: names.containerAppsEnv
    logName: names.log
    vnetName: names.vnet
  }
  dependsOn: [
    network
    observability
  ]
}

// ── AI·데이터 ───────────────────────────────────────────────
module ai 'modules/ai.bicep' = if (deployAi) {
  name: 'rsai-ai'
  params: {
    location: location
    tags: tags
    foundryName: names.foundry
    projectName: names.foundryProject
    docIntelName: names.docIntel
    contentSafetyName: names.contentSafety
    chatModelName: chatModelName
    chatModelVersion: chatModelVersion
    chatModelCapacity: chatModelCapacity
    keyVaultName: names.keyVault
  }
}

module search 'modules/search.bicep' = if (deployAi) {
  name: 'rsai-search'
  params: {
    location: searchLocation
    tags: tags
    searchName: names.search
    skuName: searchSku
    keyVaultName: names.keyVault
  }
}

module maps 'modules/maps.bicep' = if (deployAi && enableMaps) {
  name: 'rsai-maps'
  params: {
    tags: tags
    mapsName: names.maps
    keyVaultName: names.keyVault
  }
}

// ── Ollama ──────────────────────────────────────────────────
module ollama 'modules/ollama-vm.bicep' = if (deployOllama) {
  name: 'rsai-ollama'
  params: {
    location: location
    tags: tags
    vmName: names.ollamaVm
    vmSize: ollamaVmSize
    vnetName: names.vnet
    pullModels: ollamaPullModels
    sshPublicKey: ollamaSshPublicKey
  }
}

// ── ML·Fabric ───────────────────────────────────────────────
module ml 'modules/ml.bicep' = if (deployMl) {
  name: 'rsai-ml'
  params: {
    location: location
    tags: tags
    workspaceName: mlWorkspaceName
    cpuVmSize: mlCpuVmSize
    gpuVmSize: mlGpuVmSize
    enableGpuCluster: enableGpuCluster
  }
}

module fabric 'modules/fabric.bicep' = if (deployMl && enableFabric) {
  name: 'rsai-fabric'
  params: {
    location: location
    tags: tags
    capacityName: names.fabric
    admins: fabricAdmins
  }
}

output resourceNames object = names
