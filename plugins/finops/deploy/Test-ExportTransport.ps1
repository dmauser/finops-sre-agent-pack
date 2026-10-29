<#
.SYNOPSIS
Runs a bounded, read-only SRE transport canary. Does not enable export-backed reports.
.DESCRIPTION
The operator supplies one completed export manifest blob name. The temporary task has one
manual execution and no delivery recipients. Inspect the returned execution/thread evidence;
an execution marked successful is not itself proof that the file reached the sandbox.
#>
param(
    [Parameter(Mandatory)][guid]$SubscriptionId,
    [Parameter(Mandatory)][string]$AgentResourceId,
    [Parameter(Mandatory)][string]$StorageAccountName,
    [Parameter(Mandatory)][string]$ManifestBlob,
    [string]$Container = 'finops-daily',
    [string]$Agent = 'finops-investigator'
)
$ErrorActionPreference = 'Stop'
if ($AgentResourceId -notmatch "^/subscriptions/$SubscriptionId/resourceGroups/[^/]+/providers/Microsoft.App/agents/[^/]+$") {
    throw 'Agent resource ID does not match the specified subscription and resource type'
}
if ($StorageAccountName -notmatch '^[a-z0-9]{3,24}$' -or
    $Container -notmatch '^[a-z0-9-]+$' -or
    $ManifestBlob -notmatch '^[a-zA-Z0-9_./-]+$' -or $ManifestBlob.Contains('..')) {
    throw 'Unsafe storage argument'
}
$endpoint = az resource show --subscription $SubscriptionId --ids $AgentResourceId --query properties.agentEndpoint -o tsv
if ($LASTEXITCODE -ne 0) { throw 'Agent lookup failed' }
if ($endpoint -notmatch '^https://[a-zA-Z0-9.-]+\.azuresre\.ai/?$') { throw 'Unexpected SRE Agent endpoint' }
$token = az account get-access-token --subscription $SubscriptionId --resource https://azuresre.dev --query accessToken -o tsv
if ($LASTEXITCODE -ne 0) { throw 'SRE authentication failed' }
$headers = @{ Authorization = ('Bearer ' + $token) }
$run = [guid]::NewGuid().ToString('N')
$directory = "/mnt/data/finops-runs/$run"
$prompt = @"
Read-only export transport validation only, not a financial report. Dynamically read and enforce
managedResources on $AgentResourceId. Do not fetch UsageDetails or save a report.
Use sandbox Python to create '$directory'. Use RunAzCliReadCommands to execute:
az storage blob download --subscription $SubscriptionId --auth-mode login --account-name $StorageAccountName --container-name $Container --name $ManifestBlob --file $directory/manifest.json --no-progress
Then use sandbox Python to open that EXACT path, parse JSON and return the SHA256 of its bytes,
byte count, runInfo.runId, blobCount and dataRowCount. Do not transcribe the content from chat,
copy it using a write tool, download with Python network calls, or change any Azure resource.
If the CLI command is rejected, or its file is not visible to Python, return TRANSPORT_UNSUPPORTED
with the exact error. Do not improvise another transport or retry more than once.
"@
$taskName = "FinOps: Export transport canary $run"
$body = @{
    name = $taskName
    description = 'One-shot production transport verification; no recipients'
    status = 'Active'
    cronExpression = '0 0 1 1 *'
    startTime = [datetime]::UtcNow.ToString('o')
    endTime = [datetime]::UtcNow.AddDays(1).ToString('o')
    maxExecutions = 1
    agentPrompt = $prompt
    agent = $Agent
    agentMode = 'autonomous'
    modelTier = 'ReasoningHeavy'
} | ConvertTo-Json
$null = Invoke-RestMethod "$endpoint/api/v1/scheduledtasks" -Method Post -Headers $headers -ContentType application/json -Body $body
$tasks = Invoke-RestMethod "$endpoint/api/v1/scheduledtasks" -Headers $headers
$matches = @($tasks | Where-Object { $_.name -eq $taskName -and $_.agent -eq $Agent })
if ($matches.Count -ne 1 -or -not $matches[0].id) { throw 'Could not identify exactly one created canary task' }
$task = $matches[0]
Invoke-RestMethod "$endpoint/api/v1/scheduledtasks/$($task.id)/execute" -Method Post -Headers $headers -ContentType application/json -Body '{}' | Out-Null
[pscustomobject]@{taskId = $task.id; runId = $run; executionUrl = "$endpoint/api/v1/scheduledtasks/$($task.id)/executions"}
