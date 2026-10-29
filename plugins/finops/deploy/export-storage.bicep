targetScope = 'resourceGroup'

param location string
param storageAccountName string
param agentPrincipalId string
@allowed(['Disabled', 'Enabled'])
param publicNetworkAccess string = 'Disabled'
@minValue(1)
param dailyRetentionDays int = 45
@minValue(32)
param closedRetentionDays int = 400
param tags object = {}

resource account 'Microsoft.Storage/storageAccounts@2025-01-01' = {
  name: storageAccountName
  location: location
  kind: 'StorageV2'
  sku: { name: 'Standard_ZRS' }
  tags: tags
  properties: {
    accessTier: 'Hot'
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    publicNetworkAccess: publicNetworkAccess
    networkAcls: { bypass: 'AzureServices', defaultAction: 'Allow' }
    encryption: {
      keySource: 'Microsoft.Storage'
      requireInfrastructureEncryption: true
      services: { blob: { enabled: true, keyType: 'Account' } }
    }
  }
}

resource blobs 'Microsoft.Storage/storageAccounts/blobServices@2025-01-01' = {
  parent: account
  name: 'default'
  properties: {
    deleteRetentionPolicy: { enabled: true, days: 7 }
    containerDeleteRetentionPolicy: { enabled: true, days: 7 }
  }
}

var containerNames = ['finops-daily', 'finops-closed']
resource containers 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-01-01' = [for name in containerNames: {
  parent: blobs
  name: name
  properties: { publicAccess: 'None' }
}]

var readerRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1')
resource readers 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (name, i) in containerNames: {
  name: guid(containers[i].id, agentPrincipalId, readerRole)
  scope: containers[i]
  properties: {
    roleDefinitionId: readerRole
    principalId: agentPrincipalId
    principalType: 'ServicePrincipal'
  }
}]

resource retention 'Microsoft.Storage/storageAccounts/managementPolicies@2025-01-01' = {
  parent: account
  name: 'default'
  properties: {
    policy: {
      rules: [for (name, i) in containerNames: {
        name: 'retain-${name}'
        enabled: true
        type: 'Lifecycle'
        definition: {
          filters: { blobTypes: ['blockBlob'], prefixMatch: ['${name}/'] }
          actions: {
            baseBlob: {
              delete: { daysAfterModificationGreaterThan: i == 0 ? dailyRetentionDays : closedRetentionDays }
            }
          }
        }
      }]
    }
  }
}

output accountId string = account.id
output accountName string = account.name
