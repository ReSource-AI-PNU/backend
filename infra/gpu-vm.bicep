// 업체 측 오픈 모델(Ollama)을 돌리는 GPU VM. 구독의 GPU 할당량이 East US의 A10 계열(NVADSA10v5)에만 있어서
// 앱 VM(Central US, infra/vm.bicep)과 지역이 다르다. 그래서 VNet을 따로 두고 앱 VNet과 양방향으로 피어링한다.
//
//   az deployment group create -g team_AIBIC2026_03_RG_EUS2 -f infra/gpu-vm.bicep \
//     -p sshPublicKey="$(cat ~/.ssh/rsai_demo_vm.pub)" sshSource=<지금 공용 IP>
//   bash infra/gpu/setup-ollama.sh   # 드라이버 확인 → Ollama 설치(사설 IP에만 바인딩) → 모델 받기
//
// - 11434는 앱 VNet(피어링)에서만 닿는다. 인터넷 인바운드는 SSH(관리자 IP 하나)만 연다.
// - GPU 드라이버는 A10을 나눠 쓰는 크기라 GRID 드라이버다. Azure 확장 대신 setup-ollama.sh가 깔고(아래 설명),
//   Ollama 설치 스크립트가 다른 드라이버를 깔지 않도록 Ollama는 드라이버를 확인한 뒤에 깐다.
// - 비용(정가, Linux 종량제): NV18ads_A10_v5 시간당 1.6달러. 쓰지 않을 때는 할당 해제한다(infra/stop.sh).
param location string = 'eastus'
param vmName string = 'vm-rsai-gpu'
param vmSize string = 'Standard_NV18ads_A10_v5'
param adminUsername string = 'rsai'
@secure()
param sshPublicKey string
@description('SSH를 허용할 공용 IP 하나(배포하는 컴퓨터)')
param sshSource string
param addressPrefix string = '10.71.0.0/24'
param subnetPrefix string = '10.71.0.0/26'
@description('앱 VM의 VNet(infra/vm.bicep). 같은 리소스 그룹에 있다')
param appVnetName string = 'vnet-rsai-demo-app'
param appAddressPrefix string = '10.70.0.0/16'
param osDiskSizeGB int = 128
param tags object = {
  project: 'resource-ai'
  role: 'ollama-gpu'
}

resource nsg 'Microsoft.Network/networkSecurityGroups@2023-11-01' = {
  name: 'nsg-rsai-gpu'
  location: location
  tags: tags
  properties: {
    securityRules: [
      {
        name: 'AllowSshFromAdmin'
        properties: {
          priority: 100
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: '${sshSource}/32'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '22'
        }
      }
      {
        name: 'AllowOllamaFromApp'
        properties: {
          priority: 110
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: appAddressPrefix
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '11434'
        }
      }
      {
        name: 'DenyOllamaOthers'
        properties: {
          priority: 120
          direction: 'Inbound'
          access: 'Deny'
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '11434'
        }
      }
    ]
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2023-11-01' = {
  name: 'vnet-rsai-gpu'
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [addressPrefix]
    }
    subnets: [
      {
        name: 'snet-gpu'
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

resource appVnet 'Microsoft.Network/virtualNetworks@2023-11-01' existing = {
  name: appVnetName
}

resource gpuToApp 'Microsoft.Network/virtualNetworks/virtualNetworkPeerings@2023-11-01' = {
  parent: vnet
  name: 'peer-gpu-to-app'
  properties: {
    remoteVirtualNetwork: {
      id: appVnet.id
    }
    allowVirtualNetworkAccess: true
    allowForwardedTraffic: false
    allowGatewayTransit: false
    useRemoteGateways: false
  }
}

resource appToGpu 'Microsoft.Network/virtualNetworks/virtualNetworkPeerings@2023-11-01' = {
  parent: appVnet
  name: 'peer-app-to-gpu'
  properties: {
    remoteVirtualNetwork: {
      id: vnet.id
    }
    allowVirtualNetworkAccess: true
    allowForwardedTraffic: false
    allowGatewayTransit: false
    useRemoteGateways: false
  }
}

// SSH와 모델 내려받기(아웃바운드)용. 인바운드는 위 NSG가 SSH 하나만 연다
resource pip 'Microsoft.Network/publicIPAddresses@2023-11-01' = {
  name: 'pip-rsai-gpu'
  location: location
  tags: tags
  sku: {
    name: 'Standard'
  }
  properties: {
    publicIPAllocationMethod: 'Static'
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
      computerName: 'rsai-gpu'
      adminUsername: adminUsername
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

// GPU 드라이버 확장(Microsoft.HpcCompute/NvidiaGpuDriverLinux)은 쓰지 않는다. 2026-11-18 NV18ads_A10_v5에서
// 확장이 GRID 드라이버와 함께 ubuntu-desktop 전체를 깔다가 Firefox 스냅 설정의 대화형 질문에서 멈췄다(끝나지 않음).
// 드라이버는 infra/gpu/setup-ollama.sh가 Azure 문서의 수동 절차(GRID 드라이버 + gridd.conf)로 깐다.

output privateIp string = nic.properties.ipConfigurations[0].properties.privateIPAddress
output publicIp string = pip.properties.ipAddress
