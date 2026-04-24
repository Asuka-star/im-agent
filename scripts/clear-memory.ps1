param(
    [string]$Context = "",
    [string]$ContainerName = "im-agent",
    [string]$SessionId,
    [switch]$All,
    [switch]$DropAliases
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Get-DockerArgs {
    if ([string]::IsNullOrWhiteSpace($Context)) {
        return @()
    }

    return @("--context", $Context)
}

if (-not $All -and [string]::IsNullOrWhiteSpace($SessionId)) {
    throw "Please provide -SessionId <chat_id/session_id>, or use -All to clear every stored session."
}

$dockerArgs = Get-DockerArgs

if ([string]::IsNullOrWhiteSpace($Context)) {
    $currentContext = docker context show
    Write-Step "Using current Docker context '$currentContext'"
}
else {
    Write-Step "Using Docker context '$Context'"
    docker @dockerArgs context inspect $Context | Out-Null
}

$keepAliases = if ($DropAliases) { "False" } else { "True" }

if ($All) {
    Write-Step "Clearing all persisted demo memory in container '$ContainerName'"
    $pythonCode = @"
import json
from app.services.memory_service import MemoryService

result = MemoryService().clear_all_memory(keep_aliases=$keepAliases)
print(json.dumps(result, ensure_ascii=False))
"@
}
else {
    $sessionJson = ($SessionId | ConvertTo-Json -Compress)
    Write-Step "Clearing persisted memory for session '$SessionId' in container '$ContainerName'"
    $pythonCode = @"
import json
from app.services.memory_service import MemoryService

result = MemoryService().clear_session_memory(session_id=$sessionJson, keep_aliases=$keepAliases)
print(json.dumps(result, ensure_ascii=False))
"@
}

docker @dockerArgs exec $ContainerName python -c $pythonCode

Write-Step "Done"
if ($All) {
    Write-Host "All persisted demo memory has been cleared." -ForegroundColor Green
}
else {
    Write-Host "Session '$SessionId' has been reset." -ForegroundColor Green
}
