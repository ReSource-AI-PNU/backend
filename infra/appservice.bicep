// 미실행·미검증: 이 파일은 bicep build도, 배포도 해 보지 못했다(작성 환경에 az와 bicep이 없다).
// 처음 쓸 때는 `az deployment group what-if`로 먼저 확인한다.
// 주의: 앱 설정(appSettings)은 배포할 때마다 이 파일의 목록으로 통째로 바뀐다. 포털이나 az로 손수 넣은
//       설정은 다음 배포에서 사라진다. 필요한 설정은 아래 매개변수로 넣는다.
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
//       비밀 값은 배포 매개변수로 지나가지 않는다. 고른 기능의 비밀이 Key Vault에 없으면 참조가 풀리지 않고
//       앱 설정에 참조 글이 그대로 남으므로, 참조는 그 기능을 켰을 때만 넣고 deploy.sh가 배포 전에 비밀이
//       있는지 확인한다. 이 파일만 따로 쓸 때는 아래 secretRef의 이름으로 비밀을 먼저 넣는다.
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
@description('두 에이전트의 모델. rule(규칙 정책, 모델 미연결) | foundry | anthropic. anthropic이면 Key Vault 비밀 anthropic-api-key가 있어야 한다.')
@allowed([
  'rule'
  'foundry'
  'anthropic'
])
param v3Llm string = 'foundry'
@description('v3Llm=anthropic일 때의 모델 이름. 비우면 앱의 기본값(claude-sonnet-5-5)을 쓴다.')
param anthropicModel string = ''
@description('true면 ollama를 고른 업체도(Ollama 주소가 없을 때) anthropic 모델이 답한다. 화면에는 설정한 모델과 답한 모델이 따로 나온다.')
param v3LlmAllVendors bool = false
@description('Foundry 리소스 주소 (뒤에 /openai/... 를 붙이지 않는다)')
param foundryEndpoint string = ''
param foundryDeployment string = 'gpt-5-mini'
@description('업체 측 에이전트 서비스가 대변하는 업체 조직 ID')
param vendorOrgId string = 'org_saegil'
@description('Ollama 주소. VNet 통합이 없어 App Service에서는 비워 둔다.')
param ollamaBaseUrl string = ''
@description('요청한 조직을 정하는 방법. header는 시연 전용(X-Org-Id를 그대로 믿는다)이고, 공개 주소에는 token을 쓴다. token이면 Key Vault 비밀 v3-org-tokens(JSON {"조직 ID": "토큰"})가 있어야 한다.')
@allowed([
  'header'
  'token'
])
param v3Auth string = 'header'
@description('true면 시연 되돌리기(POST /api/v3/demo/reset)를 누구나 부른다. 기본은 관리자 조직만.')
param v3DemoOpen bool = false
@description('API가 프런트 주소 말고 더 허용할 브라우저 출처. 쉼표로 나눈다(https://호스트[:포트]). 프런트가 /api/v3 프록시로 부르면 비워 둔다.')
param v3CorsOrigins string = ''
@description('도로 거리 제공자. 비움(없음) | none | azure(Key Vault 비밀 maps-key) | kakao(Key Vault 비밀 kakao-rest-api-key). 없으면 직선 거리에 우회 계수를 곱한 가정값으로 계산한다.')
@allowed([
  ''
  'none'
  'azure'
  'kakao'
])
param v3Distance string = ''
@description('따로 띄운 업체 측 에이전트의 주소 {"업체 조직 ID": "https://..."}. 비어 있지 않으면 API 앱에 VENDOR_SERVICE_KEY(Key Vault 비밀 vendor-service-key)도 넣는다.')
param v3VendorAgents object = {}
@description('Azure AI Search 주소. 주면 Key Vault 비밀 search-admin-key를 함께 넣는다. 비우면 로컬 검색으로 돈다.')
param searchEndpoint string = ''
param searchIndex string = 'rsai-evidence'
@description('true면 Key Vault 비밀 appinsights-connection-string을 API 앱에 넣어 실행 기록을 Application Insights로 보낸다.')
param enableAppInsights bool = false
@description('탐지 모델(Azure ML 온라인 엔드포인트)의 채점 주소. 주면 Key Vault 비밀 detector-api-key를 함께 넣는다.')
param detectorEndpoint string = ''
param detectorDeployment string = ''

// ── 프런트 설정 (Auth.js와 /api/v3 프록시) ───────────────────
@description('프런트의 공개 주소(AUTH_URL). 비우면 이 파일이 만드는 프런트 앱 주소를 쓴다.')
param authUrl string = ''
@description('true면 이름과 이메일만으로 들어오는 시연용 로그인을 켠다. 공개 주소에서는 시연 동안만 켠다.')
param authDevLogin bool = false
@description('Entra External ID 앱 등록의 클라이언트 ID. 주면 Key Vault 비밀 auth-entra-secret(클라이언트 비밀)이 있어야 한다.')
param authEntraId string = ''
@description('Entra External ID 발급자 주소 https://<subdomain>.ciamlogin.com/<tenant-id>/v2.0')
param authEntraIssuer string = ''
@description('프런트 서버가 /api/v3 요청을 넘길 백엔드 주소(V3_BACKEND_URL). 비우면 이 파일이 만드는 API 앱의 https://<API 주소>/api/v3 이다.')
param v3BackendUrl string = ''

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
  orgTokens: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=v3-org-tokens)'
  anthropicApiKey: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=anthropic-api-key)'
  kakaoRestApiKey: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=kakao-rest-api-key)'
  mapsKey: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=maps-key)'
  searchAdminKey: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=search-admin-key)'
  appInsights: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=appinsights-connection-string)'
  detectorApiKey: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=detector-api-key)'
  authSecret: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=auth-secret)'
  authEntraSecret: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=auth-entra-secret)'
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
  {
    // anthropic을 골랐을 때만 참조를 넣는다. 다른 모드에서는 앱이 이 값을 읽지 않는다.
    name: 'ANTHROPIC_API_KEY'
    value: v3Llm == 'anthropic' ? secretRef.anthropicApiKey : ''
  }
  {
    name: 'ANTHROPIC_MODEL'
    value: anthropicModel
  }
  {
    name: 'V3_LLM_ALL_VENDORS'
    value: v3LlmAllVendors ? 'true' : 'false'
  }
]
// API 앱에만 넣는 연결 설정. 값이 비면 그 연결은 꺼진다(앱이 빈 값을 "없음"으로 읽는다).
var apiOptionalSettings = [
  {
    name: 'V3_CORS_ORIGINS'
    value: v3CorsOrigins
  }
  {
    name: 'V3_DISTANCE'
    value: v3Distance
  }
  {
    name: 'KAKAO_REST_API_KEY'
    value: v3Distance == 'kakao' ? secretRef.kakaoRestApiKey : ''
  }
  {
    name: 'AZURE_MAPS_KEY'
    value: v3Distance == 'azure' ? secretRef.mapsKey : ''
  }
  {
    name: 'V3_VENDOR_AGENTS'
    value: string(v3VendorAgents)
  }
  {
    name: 'VENDOR_SERVICE_KEY'
    value: empty(v3VendorAgents) ? '' : secretRef.vendorServiceKey
  }
  {
    name: 'AZURE_SEARCH_ENDPOINT'
    value: searchEndpoint
  }
  {
    name: 'AZURE_SEARCH_API_KEY'
    value: empty(searchEndpoint) ? '' : secretRef.searchAdminKey
  }
  {
    name: 'AZURE_SEARCH_INDEX'
    value: searchIndex
  }
  {
    name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
    value: enableAppInsights ? secretRef.appInsights : ''
  }
  {
    name: 'V3_DETECTOR_ENDPOINT'
    value: detectorEndpoint
  }
  {
    name: 'V3_DETECTOR_API_KEY'
    value: empty(detectorEndpoint) ? '' : secretRef.detectorApiKey
  }
  {
    name: 'V3_DETECTOR_DEPLOYMENT'
    value: detectorDeployment
  }
]
// 프런트: Auth.js가 읽는 값과, 프런트 서버가 /api/v3 요청을 백엔드로 넘길 때 쓰는 값. 모두 서버 전용이다
// (NEXT_PUBLIC_ 으로 시작하지 않으므로 브라우저 번들에 들어가지 않는다).
var frontendAuthSettings = [
  {
    name: 'AUTH_SECRET'
    value: secretRef.authSecret
  }
  {
    name: 'AUTH_URL'
    value: empty(authUrl) ? 'https://${hosts.frontend}' : authUrl
  }
  {
    name: 'AUTH_TRUST_HOST'
    value: 'true'
  }
  {
    name: 'AUTH_DEV_LOGIN'
    value: authDevLogin ? 'true' : 'false'
  }
  {
    name: 'V3_BACKEND_URL'
    value: empty(v3BackendUrl) ? 'https://${hosts.backend}/api/v3' : v3BackendUrl
  }
]
// 조직별 토큰은 백엔드와 같은 Key Vault 비밀(v3-org-tokens)을 본다. token 모드일 때만 넣는다.
var frontendTokenSettings = v3Auth == 'token'
  ? [
      {
        name: 'V3_ORG_TOKENS'
        value: secretRef.orgTokens
      }
    ]
  : []
// Entra 로그인은 클라이언트 ID를 줬을 때만 넣는다(없으면 로그인 화면의 Microsoft 버튼이 꺼진다).
var frontendEntraSettings = empty(authEntraId)
  ? []
  : [
      {
        name: 'AUTH_MICROSOFT_ENTRA_ID_ID'
        value: authEntraId
      }
      {
        name: 'AUTH_MICROSOFT_ENTRA_ID_ISSUER'
        value: authEntraIssuer
      }
      {
        name: 'AUTH_MICROSOFT_ENTRA_ID_SECRET'
        value: secretRef.authEntraSecret
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
      appSettings: concat(registrySettings, modelSettings, apiOptionalSettings, [
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
        {
          name: 'V3_AUTH'
          value: v3Auth
        }
        {
          name: 'V3_DEMO_OPEN'
          value: v3DemoOpen ? 'true' : 'false'
        }
        {
          // header 모드에서는 앱이 이 값을 읽지 않는다. 비밀이 없을 때 풀리지 않은 참조가 남지 않게 비워 둔다.
          name: 'V3_ORG_TOKENS'
          value: v3Auth == 'token' ? secretRef.orgTokens : ''
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
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'DOCKER|${acr.properties.loginServer}/${frontendImage}'
      alwaysOn: true
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      healthCheckPath: '/'
      appSettings: concat(registrySettings, frontendAuthSettings, frontendTokenSettings, frontendEntraSettings, [
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
resource keyVaultAccess 'Microsoft.KeyVault/vaults/accessPolicies@2023-07-01' = if (deployBackend || deployFrontend || deployVendorAgent || deployMcp) {
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
      deployFrontend
        ? [
            {
              tenantId: subscription().tenantId
              objectId: frontend!.identity.principalId
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
