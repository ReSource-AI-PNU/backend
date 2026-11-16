// 미실행·미검증: 이 파일은 bicep build도, 배포도 해 보지 못했다(작성 환경에 az와 bicep이 없다).
// 처음 쓸 때는 `az deployment group what-if`로 먼저 확인한다.
//
// Container Apps가 Microsoft.App 공급자 미등록으로 막혀 있는 동안 쓰는 대안이다.
// App Service(Linux, 컨테이너) 요금제 하나에 앱 네 개를 둔다: API, 프런트, 업체 측 에이전트, 판단 도구 MCP.
// 배포: infra/deploy.sh 가 PLATFORM=appservice 일 때 이 파일을 쓴다.
//
// Container Apps와 다른 점 (설계와 어긋나는 부분이므로 알고 써야 한다)
// - 리비전이 없다. 새 이미지로 바꾸면 곧바로 바뀐다. 되돌리려면 이전 태그로 다시 배포한다.
// - 최소 복제본 0이 없다. 요금제(B1)는 켜 둔 동안 계속 과금된다. 멈추려면 요금제를 지운다.
// - 내부 전용 수신이 없다. MCP 서버와 업체 측 에이전트도 공개 HTTPS 주소를 갖고 서비스 키로만 막는다.
// - VNet 통합을 넣지 않았다. Ollama VM(VNet 내부 전용)에는 닿지 못한다.
// 비밀: 앱 비밀은 Key Vault 참조로만 넣는다(앱의 시스템 할당 ID에 접근 정책 get 권한을 준다).
//       레지스트리 비밀번호만은 역할 할당 권한이 없어 ACR 관리자 자격을 앱 설정에 직접 넣는다.
//       Key Vault 참조는 접근 정책이 생긴 뒤에 풀리므로 배포 뒤에 앱을 한 번 다시 시작한다(deploy.sh가 한다).
targetScope = 'resourceGroup'

param location string = resourceGroup().location
param env string = 'demo'
param prefix string = 'rsai'

@description('App Service 요금제 SKU. 컨테이너와 항상 켜기를 쓰려면 B1 이상이다.')
param planSku string = 'B1'

@description('이미 있는 Container Registry 이름')
param acrName string
@description('이미 있는 Key Vault 이름')
param keyVaultName string

// ── 어떤 앱을 배포할지 (deploy.sh가 고른다) ───────────────────
param deployBackend bool = false
param deployFrontend bool = false
param deployVendorAgent bool = false
param deployMcp bool = false

@description('백엔드 이미지 "저장소:태그" (API, 업체 측 에이전트, MCP 서버가 같은 이미지를 쓴다)')
param backendImage string = ''
@description('프런트 이미지 "저장소:태그"')
param frontendImage string = ''

// ── v3 설정 ─────────────────────────────────────────────────
@allowed([
  'rule'
  'foundry'
])
param v3Llm string = 'foundry'
@description('Foundry 리소스 주소 (뒤에 /openai/... 를 붙이지 않는다)')
param foundryEndpoint string = ''
param foundryDeployment string = 'gpt-5-mini'
@description('업체 측 에이전트 서비스가 대변하는 업체 조직 ID')
param vendorOrgId string = 'org_saegil'
@description('Ollama 주소. VNet 통합이 없어 App Service에서는 비워 둔다.')
param ollamaBaseUrl string = ''

var suffix = take(uniqueString(resourceGroup().id), 6)
var tags = {
  project: 'resource-ai'
  env: env
  'managed-by': 'agent'
}
var names = {
  plan: 'asp-${prefix}-${env}'
  backend: 'app-${prefix}-${env}-api-${suffix}'
  frontend: 'app-${prefix}-${env}-web-${suffix}'
  vendorAgent: 'app-${prefix}-${env}-vendor-${suffix}'
  mcp: 'app-${prefix}-${env}-mcp-${suffix}'
}
var hosts = {
  backend: '${names.backend}.azurewebsites.net'
  frontend: '${names.frontend}.azurewebsites.net'
  vendorAgent: '${names.vendorAgent}.azurewebsites.net'
  mcp: '${names.mcp}.azurewebsites.net'
}
var secretRef = {
  databaseUrl: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=database-url)'
  foundryApiKey: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=foundry-api-key)'
  storage: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=storage-connection-string)'
  vendorServiceKey: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=vendor-service-key)'
  mcpServiceKey: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=mcp-service-key)'
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: acrName
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: names.plan
  location: location
  tags: tags
  kind: 'linux'
  sku: {
    name: planSku
  }
  properties: {
    reserved: true
  }
}

var registrySettings = [
  {
    name: 'DOCKER_REGISTRY_SERVER_URL'
    value: 'https://${acr.properties.loginServer}'
  }
  {
    name: 'DOCKER_REGISTRY_SERVER_USERNAME'
    value: acr.listCredentials().username
  }
  {
    name: 'DOCKER_REGISTRY_SERVER_PASSWORD'
    value: acr.listCredentials().passwords[0].value
  }
  {
    name: 'WEBSITES_ENABLE_APP_SERVICE_STORAGE'
    value: 'false'
  }
]
var modelSettings = [
  {
    name: 'DATABASE_URL'
    value: secretRef.databaseUrl
  }
  {
    name: 'V3_LLM'
    value: v3Llm
  }
  {
    name: 'FOUNDRY_ENDPOINT'
    value: foundryEndpoint
  }
  {
    name: 'FOUNDRY_DEPLOYMENT'
    value: foundryDeployment
  }
  {
    name: 'FOUNDRY_API_KEY'
    value: secretRef.foundryApiKey
  }
  {
    name: 'OLLAMA_BASE_URL'
    value: ollamaBaseUrl
  }
]

// ── API ─────────────────────────────────────────────────────
resource backend 'Microsoft.Web/sites@2023-12-01' = if (deployBackend) {
  name: names.backend
  location: location
  tags: tags
  kind: 'app,linux,container'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'DOCKER|${acr.properties.loginServer}/${backendImage}'
      alwaysOn: true
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      healthCheckPath: '/api/v1/health'
      appSettings: concat(registrySettings, modelSettings, [
        {
          name: 'WEBSITES_PORT'
          value: '8000'
        }
        {
          name: 'APP_ROLE'
          value: 'api'
        }
        {
          name: 'APP_ENV'
          value: env
        }
        {
          name: 'CORS_ORIGINS'
          value: '["https://${hosts.frontend}"]'
        }
        {
          name: 'AZURE_STORAGE_CONNECTION_STRING'
          value: secretRef.storage
        }
      ])
    }
  }
}

// ── 프런트 (Next.js standalone, 3000번) ─────────────────────
resource frontend 'Microsoft.Web/sites@2023-12-01' = if (deployFrontend) {
  name: names.frontend
  location: location
  tags: tags
  kind: 'app,linux,container'
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'DOCKER|${acr.properties.loginServer}/${frontendImage}'
      alwaysOn: true
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      healthCheckPath: '/'
      appSettings: concat(registrySettings, [
        {
          name: 'WEBSITES_PORT'
          value: '3000'
        }
        {
          name: 'PORT'
          value: '3000'
        }
        {
          name: 'HOSTNAME'
          value: '0.0.0.0'
        }
      ])
    }
  }
}

// ── 업체 측 에이전트 ────────────────────────────────────────
resource vendorAgent 'Microsoft.Web/sites@2023-12-01' = if (deployVendorAgent) {
  name: names.vendorAgent
  location: location
  tags: tags
  kind: 'app,linux,container'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'DOCKER|${acr.properties.loginServer}/${backendImage}'
      alwaysOn: true
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      healthCheckPath: '/healthz'
      appSettings: concat(registrySettings, modelSettings, [
        {
          name: 'WEBSITES_PORT'
          value: '8000'
        }
        {
          name: 'APP_ROLE'
          value: 'vendor-agent'
        }
        {
          name: 'APP_ENV'
          value: env
        }
        {
          name: 'VENDOR_ORG_ID'
          value: vendorOrgId
        }
        {
          name: 'VENDOR_SERVICE_KEY'
          value: secretRef.vendorServiceKey
        }
      ])
    }
  }
}

// ── 판단 도구 MCP 서버 ──────────────────────────────────────
resource mcp 'Microsoft.Web/sites@2023-12-01' = if (deployMcp) {
  name: names.mcp
  location: location
  tags: tags
  kind: 'app,linux,container'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'DOCKER|${acr.properties.loginServer}/${backendImage}'
      alwaysOn: true
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      healthCheckPath: '/healthz'
      appSettings: concat(registrySettings, [
        {
          name: 'WEBSITES_PORT'
          value: '8000'
        }
        {
          name: 'APP_ROLE'
          value: 'mcp'
        }
        {
          name: 'APP_ENV'
          value: env
        }
        {
          name: 'DATABASE_URL'
          value: secretRef.databaseUrl
        }
        {
          name: 'MCP_SERVICE_KEY'
          value: secretRef.mcpServiceKey
        }
      ])
    }
  }
}

// ── Key Vault 접근 정책: 앱의 시스템 할당 ID가 비밀을 읽게 한다 (역할 할당 대신) ──
var readSecrets = {
  secrets: [
    'get'
  ]
}
resource keyVaultAccess 'Microsoft.KeyVault/vaults/accessPolicies@2023-07-01' = if (deployBackend || deployVendorAgent || deployMcp) {
  parent: keyVault
  name: 'add'
  properties: {
    accessPolicies: concat(
      deployBackend
        ? [
            {
              tenantId: subscription().tenantId
              objectId: backend!.identity.principalId
              permissions: readSecrets
            }
          ]
        : [],
      deployVendorAgent
        ? [
            {
              tenantId: subscription().tenantId
              objectId: vendorAgent!.identity.principalId
              permissions: readSecrets
            }
          ]
        : [],
      deployMcp
        ? [
            {
              tenantId: subscription().tenantId
              objectId: mcp!.identity.principalId
              permissions: readSecrets
            }
          ]
        : []
    )
  }
}

output hosts object = hosts
output appNames object = names
