// Ollama VM: bge-m3 임베딩과 업체 측 오픈 모델. 공용 IP가 없고 11434는 VNet 안에서만 열린다.
// Ollama는 0.0.0.0이 아니라 VM의 사설 IP에만 바인딩한다(설계안 v3 10.2, tests/v3/test_rules_adversarial.py).
// 그래서 같은 VM 안의 ollama 명령도 OLLAMA_HOST로 그 주소를 가리켜야 한다.
// 주의: 새 VNet은 기본 아웃바운드가 없을 수 있다. 모델을 내려받으려면 NAT Gateway 같은 아웃바운드 경로를 따로 승인받아야 한다.
param location string
param tags object
param vmName string
param vmSize string
param vnetName string
param subnetName string = 'snet-vm'
param adminUsername string = 'rsai'
@secure()
param sshPublicKey string
param pullModels array
param osDiskSizeGB int = 128

var isGpu = startsWith(vmSize, 'Standard_N')
var cloudInit = format('''#cloud-config
package_update: true
runcmd:
  - curl -fsSL https://ollama.com/install.sh | sh
  - mkdir -p /etc/systemd/system/ollama.service.d
  - printf '[Service]\nEnvironment="OLLAMA_HOST=%s:11434"\n' "$(hostname -I | cut -d' ' -f1)" > /etc/systemd/system/ollama.service.d/override.conf
  - systemctl daemon-reload
  - systemctl enable ollama
  - systemctl restart ollama
  - sleep 15
  - for m in {0}; do OLLAMA_HOST="$(hostname -I | cut -d' ' -f1):11434" ollama pull "$m"; done
''', join(pullModels, ' '))

resource vnet 'Microsoft.Network/virtualNetworks@2023-11-01' existing = {
  name: vnetName
}

resource subnet 'Microsoft.Network/virtualNetworks/subnets@2023-11-01' existing = {
  parent: vnet
  name: subnetName
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
            id: subnet.id
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
      computerName: 'rsai-ollama'
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

resource gpuDriver 'Microsoft.Compute/virtualMachines/extensions@2024-03-01' = if (isGpu) {
  parent: vm
  name: 'NvidiaGpuDriverLinux'
  location: location
  tags: tags
  properties: {
    publisher: 'Microsoft.HpcCompute'
    type: 'NvidiaGpuDriverLinux'
    typeHandlerVersion: '1.9'
    autoUpgradeMinorVersion: true
  }
}

output privateIp string = nic.properties.ipConfigurations[0].properties.privateIPAddress
