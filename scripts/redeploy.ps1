param(
    [string]$Context = "",
    [string]$ImageName = "im-agent",
    [string]$ContainerName = "im-agent",
    [string]$NetworkName = "feishu-agent-net",
    [string]$EnvFile = ".env",
    [switch]$NoCache,
    [switch]$NoFollow,
    [int]$HostPort = 9000,
    [int]$ContainerPort = 9000
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

$projectRoot = Split-Path -Parent $PSScriptRoot
$resolvedEnvFile = Join-Path $projectRoot $EnvFile

if (-not (Test-Path $resolvedEnvFile)) {
    throw "Env file not found: $resolvedEnvFile"
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

Write-Step "Ensuring Docker network '$NetworkName' exists"
$networkExists = docker @dockerArgs network ls --format "{{.Name}}" | Select-String -SimpleMatch $NetworkName
if (-not $networkExists) {
    docker @dockerArgs network create $NetworkName | Out-Null
}

Write-Step "Building image '$ImageName'"
$buildArgs = @()
if (-not [string]::IsNullOrWhiteSpace($Context)) {
    $buildArgs += @("--context", $Context)
}
$buildArgs += @("build", "-t", $ImageName)
if ($NoCache) {
    $buildArgs += "--no-cache"
}
$buildArgs += $projectRoot
docker @buildArgs

Write-Step "Stopping old container if it exists"
$containerExists = docker @dockerArgs ps -a --format "{{.Names}}" | Select-String -SimpleMatch $ContainerName
if ($containerExists) {
    docker @dockerArgs rm -f $ContainerName | Out-Null
}
else {
    Write-Host "Container '$ContainerName' does not exist yet, skipping removal." -ForegroundColor DarkGray
}

Write-Step "Starting new container '$ContainerName'"
docker @dockerArgs run -d `
    --name $ContainerName `
    --restart unless-stopped `
    --network $NetworkName `
    --env-file $resolvedEnvFile `
    -p "${HostPort}:${ContainerPort}" `
    $ImageName | Out-Null

Write-Step "Container status"
docker @dockerArgs ps --filter "name=$ContainerName"

Write-Step "Container image digest"
docker @dockerArgs inspect $ContainerName --format "{{.Image}}"

Write-Step "Recent logs"
docker @dockerArgs logs --tail 50 $ContainerName

Write-Step "Done"
Write-Host "Health check URL: http://127.0.0.1:$HostPort/api/health" -ForegroundColor Green

if (-not $NoFollow) {
    Write-Step "Following logs for '$ContainerName' (Ctrl+C to stop)"
    docker @dockerArgs logs -f $ContainerName
}
