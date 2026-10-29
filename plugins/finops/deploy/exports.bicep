// Additive export infrastructure for either an existing or a new SRE Agent.
targetScope = 'subscription'

param resourceGroupName string
param agentPrincipalId string
param location string = 'centralus'
param storageAccountName string = 'stfinops${uniqueString(subscription().id, resourceGroupName)}'
@description('Native cost-and-usage dataset version supported by the billing agreement. MOSA: 2019-11-01; EA/MCA: 2021-01-01.')
@minLength(1)
param dataVersion string
@description('Future UTC recurrence start; explicitly supplied so redeployment does not shift schedules.')
param scheduleStart string
@description('Renew before this UTC date; the service requires an explicit future end.')
param scheduleEnd string = dateTimeAdd(scheduleStart, 'P1Y')
@allowed(['Active', 'Inactive'])
param exportStatus string = 'Inactive'
@allowed(['Disabled', 'Enabled'])
param publicNetworkAccess string = 'Disabled'
param dailyRetentionDays int = 45
param closedRetentionDays int = 400

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' existing = { name: resourceGroupName }

module storage 'export-storage.bicep' = {
  name: 'finops-export-storage'
  scope: rg
  params: {
    location: location
    storageAccountName: storageAccountName
    agentPrincipalId: agentPrincipalId
    publicNetworkAccess: publicNetworkAccess
    dailyRetentionDays: dailyRetentionDays
    closedRetentionDays: closedRetentionDays
    tags: { workload: 'finops-exports', managedBy: 'finops-sre-agent-pack' }
  }
}

var jobs = [
  { name: 'actual-daily', basis: 'ActualCost', period: 'MonthToDate', recurrence: 'Daily', container: 'finops-daily' }
  { name: 'amortized-daily', basis: 'AmortizedCost', period: 'MonthToDate', recurrence: 'Daily', container: 'finops-daily' }
  { name: 'actual-closed', basis: 'ActualCost', period: 'TheLastMonth', recurrence: 'Monthly', container: 'finops-closed' }
  { name: 'amortized-closed', basis: 'AmortizedCost', period: 'TheLastMonth', recurrence: 'Monthly', container: 'finops-closed' }
]

resource exports 'Microsoft.CostManagement/exports@2025-03-01' = [for job in jobs: {
  name: 'finops-${job.name}'
  location: location
  identity: { type: 'SystemAssigned' }
  properties: {
    format: 'Csv'
    compressionMode: 'gzip'
    partitionData: true
    dataOverwriteBehavior: 'CreateNewReport'
    definition: {
      type: job.basis
      timeframe: job.period
      dataSet: { granularity: 'Daily', configuration: { dataVersion: dataVersion } }
    }
    deliveryInfo: {
      destination: {
        resourceId: storage.outputs.accountId
        container: job.container
        rootFolderPath: job.name
        type: 'AzureBlob'
      }
    }
    schedule: {
      status: exportStatus
      recurrence: job.recurrence
      recurrencePeriod: { from: scheduleStart, to: scheduleEnd }
    }
  }
}]

output storageAccountName string = storage.outputs.accountName
output storageAccountId string = storage.outputs.accountId
output exportNames array = [for job in jobs: 'finops-${job.name}']
