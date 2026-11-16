// Microsoft Fabric 용량 (F2): 기준값 시계열 Lakehouse. Microsoft.Fabric 공급자 등록이 필요하다.
param location string
param tags object
param capacityName string
@description('용량 관리자 UPN 목록')
param admins array

resource capacity 'Microsoft.Fabric/capacities@2023-11-01' = {
  name: capacityName
  location: location
  tags: tags
  sku: {
    name: 'F2'
    tier: 'Fabric'
  }
  properties: {
    administration: {
      members: admins
    }
  }
}
