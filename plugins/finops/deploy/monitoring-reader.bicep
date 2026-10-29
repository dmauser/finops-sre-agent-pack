// Add the replacement before the operator removes the exact legacy pack assignment.
targetScope = 'subscription'
param resourceGroupName string
param agentName string
param agentPrincipalId string
var reader = '43d0d8ad-25c7-4714-9337-8ba259a9fe05'
resource readOnly 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(subscription().id, resourceGroupName, agentName, reader)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', reader)
    principalId: agentPrincipalId
    principalType: 'ServicePrincipal'
  }
}
output legacyAssignmentId string = subscriptionResourceId('Microsoft.Authorization/roleAssignments',
  guid(subscription().id, resourceGroupName, agentName, '749f88d5-cbae-40b8-bcfc-e573ddc772fa'))
output replacementAssignmentId string = readOnly.id
