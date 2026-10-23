// Azure SRE Agent resource + its supporting resources, mirroring a portal-created FinOps agent:
// user-assigned identity, Log Analytics workspace, workspace-based Application Insights, and the
// Microsoft.App/agents resource (Low access, Review action mode, Automatic model, Limited egress).
// Deployed into the target resource group by main.bicep.

@description('SRE Agent name (2-32 chars, letters/numbers/hyphens, starts with a letter).')
@minLength(2)
@maxLength(32)
param agentName string

@description('Azure region for the agent and its supporting resources.')
param location string

@description('Full ARM IDs the agent manages (knowledge graph scope). Defaults to the subscription.')
param managedResources array

@description('Object ID of the deploying user or service principal (granted SRE Agent Administrator).')
param deployerObjectId string

@allowed(['User', 'ServicePrincipal', 'Group'])
param deployerPrincipalType string = 'User'

@allowed(['Low', 'High'])
param accessLevel string = 'Low'

@allowed(['Review', 'Autonomous', 'ReadOnly'])
param actionMode string = 'Review'

@allowed(['Stable', 'Preview'])
param upgradeChannel string = 'Stable'

param monthlyAgentUnitLimit int = 10000
param defaultModelProvider string = 'Anthropic'
param defaultModelName string = 'Automatic'

@allowed(['Limited', 'Unrestricted'])
param egressMode string = 'Limited'

param allowedHosts array = [
  '*.vault.azure.net'
  '*.azmk8s.io'
  '*.applicationinsights.azure.com'
  'api.applicationinsights.io'
  'api.loganalytics.io'
  'api.loganalytics.azure.com'
  'graph.microsoft.com'
  'aka.ms'
  'learn.microsoft.com'
  'www.bing.com'
  'azcliprod.blob.core.windows.net'
  'azcliextensionsync.blob.core.windows.net'
  'azurecliextensionsync.blob.core.windows.net'
  '*.dev.azure.com'
  '*.scm.azurewebsites.net'
  '*.prometheus.monitor.azure.com'
  'api.securitycenter.microsoft.com'
  'api.powerbi.com'
  'api.fabric.microsoft.com'
  'adf.azure.com'
  'dev.azure.com'
  'msazure.visualstudio.com'
  'management.azure.com'
  'api.github.com'
]
param allowedRegistries array = ['pypi', 'npmjs', 'nuget-org', 'ubuntu']
param allowedCodeRepositories array = ['Github', 'AzureDevOps', 'GithubEnterprise']

param tags object = {}

var suffix = uniqueString(resourceGroup().id, agentName)
var sreAgentAdministrator = 'e79298df-d852-4c6d-84f9-5d13249d1e55'
var readerRole = 'acdd72a7-3385-48ef-bd42-f606fba81ae7'
var monitoringContributor = '749f88d5-cbae-40b8-bcfc-e573ddc772fa'

resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'workspace${suffix}'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${agentName}-app-insights'
  location: location
  kind: 'web'
  tags: tags
  properties: {
    Application_Type: 'web'
    Request_Source: 'SreAgent'
    WorkspaceResourceId: law.id
  }
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${agentName}-${suffix}'
  location: location
  tags: tags
}

// Monitoring Contributor on the agent resource group (portal parity).
resource rgMonitoringContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, identity.id, monitoringContributor)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', monitoringContributor)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

#disable-next-line BCP081
resource sreAgent 'Microsoft.App/agents@2025-05-01-preview' = {
  name: agentName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned, UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    knowledgeGraphConfiguration: {
      identity: identity.id
      managedResources: managedResources
    }
    actionConfiguration: {
      accessLevel: accessLevel
      identity: identity.id
      mode: actionMode
    }
    logConfiguration: {
      applicationInsightsConfiguration: {
        appId: appInsights.properties.AppId
        connectionString: appInsights.properties.ConnectionString
        applicationInsightsResourceId: appInsights.id
      }
    }
    upgradeChannel: upgradeChannel
    monthlyAgentUnitLimit: monthlyAgentUnitLimit
    defaultModel: {
      provider: defaultModelProvider
      name: defaultModelName
    }
    experimentalSettings: {
      EnableConnectorsV2: true
    }
    sandboxConfiguration: egressMode == 'Limited' ? {
      egress: {
        mode: egressMode
        allowedHosts: allowedHosts
        allowedRegistries: allowedRegistries
        allowedCodeRepositories: allowedCodeRepositories
        allowHttpMcpServerNetworkAccess: true
      }
    } : null
  }
  dependsOn: [rgMonitoringContributor]
}

// Data-plane access (skills, subagents, plugins, scheduled tasks) for the deployer.
resource deployerAdmin 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(sreAgent.id, deployerObjectId, sreAgentAdministrator)
  scope: sreAgent
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', sreAgentAdministrator)
    principalId: deployerObjectId
    principalType: deployerPrincipalType
  }
}

// install-api.sh requires the agent identity to read its own resource (managed-scope discovery).
resource uamiAgentReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(sreAgent.id, identity.id, readerRole)
  scope: sreAgent
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', readerRole)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

output agentId string = sreAgent.id
output identityId string = identity.id
output identityPrincipalId string = identity.properties.principalId
output appInsightsId string = appInsights.id
output workspaceId string = law.id
