// FinOps SRE Agent — infrastructure (phase 1 of deploy.sh).
// Creates the resource group, the SRE Agent with its identity/monitoring, and the read-only
// subscription RBAC the FinOps skills need. Phase 2 (plugin, subagent, scheduled tasks) runs
// against the agent data plane from deploy.sh -> install-api.sh.
//
//   az deployment sub create -l <region> -f main.bicep \
//     -p agentName=<name> resourceGroupName=<rg> location=<region> deployerObjectId=<oid>

targetScope = 'subscription'

@description('SRE Agent name.')
param agentName string = 'sre-finops-demo'

@description('Resource group for the agent (created if missing).')
param resourceGroupName string = 'SRE-FinOps-Demo'

@description('Azure region (must be an SRE Agent region, e.g. eastus2, centralus, swedencentral, uksouth, australiaeast).')
param location string = 'centralus'

@description('Object ID of the deploying identity (az ad signed-in-user show --query id -o tsv).')
param deployerObjectId string

@allowed(['User', 'ServicePrincipal', 'Group'])
param deployerPrincipalType string = 'User'

@description('Grant the agent identity Reader, Monitoring Contributor, Log Analytics Reader and Cost Management Reader on this subscription.')
param assignSubscriptionRoles bool = true

param tags object = {
  workload: 'finops-sre-agent'
}

var roles = [
  'acdd72a7-3385-48ef-bd42-f606fba81ae7' // Reader
  '749f88d5-cbae-40b8-bcfc-e573ddc772fa' // Monitoring Contributor
  '73c42c96-874c-492b-b04d-ab87d138a893' // Log Analytics Reader
  '72fafb9e-0641-4937-9268-a91bfd8191a3' // Cost Management Reader
]

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module agent 'agent.bicep' = {
  name: 'finops-sre-agent-${uniqueString(agentName)}'
  scope: rg
  params: {
    agentName: agentName
    location: location
    managedResources: [subscription().id]
    deployerObjectId: deployerObjectId
    deployerPrincipalType: deployerPrincipalType
    tags: tags
  }
}

resource subRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for roleId in roles: if (assignSubscriptionRoles) {
  name: guid(subscription().id, resourceGroupName, agentName, roleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleId)
    principalId: agent.outputs.identityPrincipalId
    principalType: 'ServicePrincipal'
  }
}]

output agentId string = agent.outputs.agentId
output identityId string = agent.outputs.identityId
output identityPrincipalId string = agent.outputs.identityPrincipalId
output resourceGroupName string = rg.name
