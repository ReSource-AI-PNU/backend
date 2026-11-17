// Azure ML: 기존 작업 영역을 재사용하고 컴퓨팅 클러스터만 더한다 (최소 노드 0, 쉬면 과금 없음).
param location string
param tags object
param workspaceName string
param cpuVmSize string
param gpuVmSize string
param enableGpuCluster bool = false

resource ws 'Microsoft.MachineLearningServices/workspaces@2024-04-01' existing = {
  name: workspaceName
}

resource cpu 'Microsoft.MachineLearningServices/workspaces/computes@2024-04-01' = {
  parent: ws
  name: 'rsai-cpu'
  location: location
  tags: tags
  properties: {
    computeType: 'AmlCompute'
    description: 'Re:Source AI 등급 모델 학습(AutoML)과 평가용 CPU 클러스터'
    properties: {
      vmSize: cpuVmSize
      vmPriority: 'Dedicated'
      scaleSettings: {
        minNodeCount: 0
        maxNodeCount: 2
        nodeIdleTimeBeforeScaleDown: 'PT10M'
      }
    }
  }
}

resource gpu 'Microsoft.MachineLearningServices/workspaces/computes@2024-04-01' = if (enableGpuCluster) {
  parent: ws
  name: 'rsai-gpu'
  location: location
  tags: tags
  properties: {
    computeType: 'AmlCompute'
    description: 'Re:Source AI 탐지 모델 학습용 GPU 클러스터'
    properties: {
      vmSize: gpuVmSize
      vmPriority: 'Dedicated'
      scaleSettings: {
        minNodeCount: 0
        maxNodeCount: 1
        nodeIdleTimeBeforeScaleDown: 'PT5M'
      }
    }
  }
}
