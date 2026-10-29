[CmdletBinding()]
param(
    [Parameter(Mandatory)][guid]$SubscriptionId,
    [Parameter(Mandatory)][string]$ResourceGroupName,
    [Parameter(Mandatory)][string]$AgentName,
    [Parameter(Mandatory)][guid]$AgentPrincipalId,
    [Parameter(Mandatory)][string]$RecordPath,
    [string]$Location = 'centralus',
    [switch]$Rollback
)
$ErrorActionPreference = 'Stop'
$scope = "/subscriptions/$SubscriptionId"
$legacyRole = '749f88d5-cbae-40b8-bcfc-e573ddc772fa'
$readerRole = '43d0d8ad-25c7-4714-9337-8ba259a9fe05'
function Invoke-AzJson([string[]]$Arguments) {
    $result = & az @Arguments --subscription "$SubscriptionId" --output json
    if ($LASTEXITCODE -ne 0) { throw "Azure command failed: $($Arguments[0..1] -join ' ')" }
    if ($result) { return ($result -join "`n" | ConvertFrom-Json) }
}
function Get-Assignments {
    @(Invoke-AzJson @('role', 'assignment', 'list', '--all', '--fill-principal-name', 'false',
        '--query', "[?principalId=='$AgentPrincipalId']"))
}
function Assert-Legacy($Assignment, [string]$ExpectedId) {
    if ($Assignment.id -ne $ExpectedId -or $Assignment.scope -ne $scope -or
        $Assignment.principalId -ne "$AgentPrincipalId" -or
        $Assignment.roleDefinitionId -ne "$scope/providers/Microsoft.Authorization/roleDefinitions/$legacyRole") {
        throw 'Refusing to modify an assignment outside the exact legacy principal/scope/role.'
    }
}

$agentId = "$scope/resourceGroups/$ResourceGroupName/providers/Microsoft.App/agents/$AgentName"
$agent = Invoke-AzJson @('resource', 'show', '--ids', $agentId)
$principals = @($agent.identity.userAssignedIdentities.PSObject.Properties.Value.principalId)
if ($principals -notcontains "$AgentPrincipalId") { throw 'Principal does not belong to this agent identity.' }

if ($Rollback) {
    $record = Get-Content -LiteralPath $RecordPath -Raw | ConvertFrom-Json
    if ($record.agentId -ne $agentId -or -not $record.legacyAssignment) {
        throw 'Rollback record is for a different agent or contains no removed assignment.'
    }
    Assert-Legacy $record.legacyAssignment $record.legacyAssignmentId
    $body = @{ properties = @{
        principalId = "$AgentPrincipalId"
        principalType = 'ServicePrincipal'
        roleDefinitionId = $record.legacyAssignment.roleDefinitionId
    } }
    $temp = New-TemporaryFile
    try {
        $body | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $temp.FullName -Encoding utf8
        $null = Invoke-AzJson @('rest', '--method', 'put', '--url',
            "https://management.azure.com$($record.legacyAssignmentId)?api-version=2022-04-01",
            '--body', "@$($temp.FullName)")
    } finally { Remove-Item -LiteralPath $temp.FullName }
    $restored = @(Get-Assignments | Where-Object id -EQ $record.legacyAssignmentId)
    if ($restored.Count -ne 1) { throw 'Rollback assignment was not found after restoration.' }
    Assert-Legacy $restored[0] $record.legacyAssignmentId
    Write-Output 'Legacy assignment restored; replacement reader retained.'
    return
}

if (Test-Path -LiteralPath $RecordPath) { throw 'Refusing to overwrite an existing rollback record.' }
$before = Get-Assignments
$deployment = Invoke-AzJson @('deployment', 'sub', 'create', '--name', 'finops-monitoring-reader',
    '--location', $Location, '--template-file', "$PSScriptRoot\monitoring-reader.bicep", '--parameters',
    "resourceGroupName=$ResourceGroupName", "agentName=$AgentName", "agentPrincipalId=$AgentPrincipalId")
$legacyId = $deployment.properties.outputs.legacyAssignmentId.value
$readerId = $deployment.properties.outputs.replacementAssignmentId.value
$afterGrant = Get-Assignments
$reader = @($afterGrant | Where-Object { $_.id -eq $readerId -and $_.scope -eq $scope -and
    $_.principalId -eq "$AgentPrincipalId" -and $_.roleDefinitionId.EndsWith("/$readerRole") })
if ($reader.Count -ne 1) { throw 'Replacement reader assignment was not verified; legacy role untouched.' }
$legacy = @($before | Where-Object id -EQ $legacyId)
if ($legacy.Count -gt 1) { throw 'Ambiguous legacy assignment; nothing removed.' }
if ($legacy.Count -eq 1) { Assert-Legacy $legacy[0] $legacyId }
@{
    agentId = $agentId; legacyAssignmentId = $legacyId; replacementAssignmentId = $readerId
    legacyAssignment = $(if ($legacy.Count) { $legacy[0] } else { $null })
    originalAssignments = $before; capturedAt = [DateTime]::UtcNow.ToString('o')
} | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $RecordPath -Encoding utf8
if ($legacy.Count) {
    $null = Invoke-AzJson @('role', 'assignment', 'delete', '--ids', $legacyId)
}
$after = Get-Assignments
if ($after.id -contains $legacyId -or $after.id -notcontains $readerId) {
    throw 'Migration verification failed; use the captured rollback record.'
}
foreach ($assignment in $before) {
    if ($assignment.id -ne $legacyId -and $after.id -notcontains $assignment.id) {
        throw 'An unrelated role changed during migration; investigate the captured baseline.'
    }
}
Write-Output 'Monitoring Reader verified; only the exact legacy pack assignment was removed if present.'
