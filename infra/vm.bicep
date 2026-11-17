// 앱 VM: 백엔드 API, 프런트, Ollama(CPU), Caddy(HTTPS)를 Docker Compose로 한 대에 띄운다.
// App Service는 구독의 할당량이 0이고(B1·B2·F1·S1·P0v3 모두 SubscriptionIsOverQuotaForSku, 2026-11-18 확인),
// Container Apps는 Microsoft.App 공급자가 등록되지 않아 이 VM을 쓴다. GPU 할당량도 없어 Ollama는 CPU로 돈다.
//
// 기존 vnet-rsai-demo(base 묶음)를 건드리지 않도록 VNet을 따로 둔다. PostgreSQL은 공용 접근 + "Azure 서비스 허용"
// 규칙이라 이 VM에서 닿는다. 인터넷에는 80·443만 열고, 22는 sshSourcePrefix에서만 연다.
// Ollama 포트(11434)는 호스트에 열지 않는다. 같은 Compose 네트워크의 백엔드만 닿는다(infra/vm/compose.yml).
//
//   az deployment group what-if -g <RG> -f infra/vm.bicep -p sshPublicKey=@~/.ssh/rsai_demo_vm.pub sshSourcePrefix=<내 IP>/32
param location string = resourceGroup().location
param tags object = {
  project: 'resource-ai'
  env: 'demo'
  'managed-by': 'agent'
}
param vmName string = 'vm-rsai-demo-app'
@description('4 vCPU, 16GB. 앱 네 개와 Ollama(qwen2.5:7b, bge-m3) CPU 추론이 함께 들어간다')
param vmSize string = 'Standard_D4as_v5'
param adminUsername string = 'rsai'
@description('SSH 공개 키 한 줄')
param sshPublicKey string
@description('SSH(22)를 허용할 주소 범위. 예: 203.0.113.7/32')
param sshSourcePrefix string
@description('공용 주소 이름. <dnsLabel>.<지역>.cloudapp.azure.com 이 된다')
param dnsLabel string = 'rsai-${uniqueString(resourceGroup().id)}'
param osDiskSizeGB int = 128
param addressPrefix string = '10.70.0.0/16'
param subnetPrefix string = '10.70.1.0/24'

var cloudInit = format('''#cloud-config
package_update: true
runcmd:
  - curl -fsSL https://get.docker.com | sh
  - usermod -aG docker {0}
  - mkdir -p /opt/rsai
  - chown {0}:{0} /opt/rsai
''', adminUsername)

resource nsg 'Microsoft.Network/networkSecurityGroups@2023-11-01' = {
  name: 'nsg-rsai-demo-app'
  location: location
  tags: tags
  properties: {
    securityRules: [
      {
        name: 'AllowHttpFromInternet'
        properties: {
          priority: 100
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'Internet'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '80'
        }
      }
      {
        name: 'AllowHttpsFromInternet'
        properties: {
          priority: 110
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'Internet'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '443'
        }
      }
      {
        name: 'AllowSshFromAdmin'
        properties: {
          priority: 120
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: sshSourcePrefix
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '22'
        }
      }
    ]
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2023-11-01' = {
  name: 'vnet-rsai-demo-app'
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [addressPrefix]
    }
    subnets: [
      {
        name: 'snet-app'
        properties: {
          addressPrefix: subnetPrefix
          networkSecurityGroup: {
            id: nsg.id
          }
        }
      }
    ]
  }
}

resource pip 'Microsoft.Network/publicIPAddresses@2023-11-01' = {
  name: 'pip-rsai-demo-app'
  location: location
  tags: tags
  sku: {
    name: 'Standard'
  }
  properties: {
    publicIPAllocationMethod: 'Static'
    dnsSettings: {
      domainNameLabel: dnsLabel
    }
  }
}

resource nic 'Microsoft.Network/networkInterfaces@2023-11-01' = {
  name: '${vmName}-nic'
  location: location
  tags: tags
  properties: {
    ipConfigurations: [
      {
        name: 'ipconfig1'
        properties: {
          privateIPAllocationMethod: 'Dynamic'
          subnet: {
            id: vnet.properties.subnets[0].id
          }
          publicIPAddress: {
            id: pip.id
          }
        }
      }
    ]
  }
}

resource vm 'Microsoft.Compute/virtualMachines@2024-03-01' = {
  name: vmName
  location: location
  tags: tags
  properties: {
    hardwareProfile: {
      vmSize: vmSize
    }
    osProfile: {
      computerName: 'rsai-app'
      adminUsername: adminUsername
      customData: base64(cloudInit)
      linuxConfiguration: {
        disablePasswordAuthentication: true
        ssh: {
          publicKeys: [
            {
              path: '/home/${adminUsername}/.ssh/authorized_keys'
              keyData: sshPublicKey
            }
          ]
        }
      }
    }
    storageProfile: {
      imageReference: {
        publisher: 'Canonical'
        offer: '0001-com-ubuntu-server-jammy'
        sku: '22_04-lts-gen2'
        version: 'latest'
      }
      osDisk: {
        createOption: 'FromImage'
        diskSizeGB: osDiskSizeGB
        managedDisk: {
          storageAccountType: 'StandardSSD_LRS'
        }
        deleteOption: 'Delete'
      }
    }
    networkProfile: {
      networkInterfaces: [
        {
          id: nic.id
          properties: {
            deleteOption: 'Delete'
          }
        }
      ]
    }
  }
}

output fqdn string = pip.properties.dnsSettings.fqdn
output publicIp string = pip.properties.ipAddress
