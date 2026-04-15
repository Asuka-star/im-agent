param(
    [string]$Context = "mylinux",
    [string]$ImageName = "feishu-im-agent-mvp",
    [string]$ContainerName = "feishu-im-agent-mvp",
    [string]$EnvFile = ".env",
    [int]$HostPort = 9000,
    [int]$ContainerPort = 9000
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$resolvedEnvFile = Join-Path $projectRoot $EnvFile

if (-not (Test-Path $resolvedEnvFile)) {
    throw "Env file not found: $resolvedEnvFile"
}

Write-Step "Using Docker context '$Context'"
docker --context $Context context inspect $Context | Out-Null

Write-Step "Building image '$ImageName'"
docker --context $Context build -t $ImageName $projectRoot

Write-Step "Stopping old container if it exists"
docker --context $Context rm -f $ContainerName 2>$null | Out-Null

Write-Step "Starting new container '$ContainerName'"
docker --context $Context run -d `
    --name $ContainerName `
    --restart unless-stopped `
    --env-file $resolvedEnvFile `
    -p "${HostPort}:${ContainerPort}" `
    $ImageName | Out-Null

Write-Step "Container status"
docker --context $Context ps --filter "name=$ContainerName"

Write-Step "Recent logs"
docker --context $Context logs --tail 50 $ContainerName

Write-Step "Done"
Write-Host "Health check URL: http://127.0.0.1:$HostPort/api/health" -ForegroundColor Green
