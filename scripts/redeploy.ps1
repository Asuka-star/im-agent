param(
    [string]$Context = "mylinux",
    [string]$ImageName = "feishu-im-agent-mvp",
    [string]$ContainerName = "feishu-im-agent-mvp",
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

$projectRoot = Split-Path -Parent $PSScriptRoot
$resolvedEnvFile = Join-Path $projectRoot $EnvFile

if (-not (Test-Path $resolvedEnvFile)) {
    throw "Env file not found: $resolvedEnvFile"
}

Write-Step "Using Docker context '$Context'"
docker --context $Context context inspect $Context | Out-Null

Write-Step "Ensuring Docker network '$NetworkName' exists"
$networkExists = docker --context $Context network ls --format "{{.Name}}" | Select-String -SimpleMatch $NetworkName
if (-not $networkExists) {
    docker --context $Context network create $NetworkName | Out-Null
}

Write-Step "Building image '$ImageName'"
$buildArgs = @("--context", $Context, "build", "-t", $ImageName)
if ($NoCache) {
    $buildArgs += "--no-cache"
}
$buildArgs += $projectRoot
docker @buildArgs

Write-Step "Stopping old container if it exists"
docker --context $Context rm -f $ContainerName 2>$null | Out-Null

Write-Step "Starting new container '$ContainerName'"
docker --context $Context run -d `
    --name $ContainerName `
    --restart unless-stopped `
    --network $NetworkName `
    --env-file $resolvedEnvFile `
    -p "${HostPort}:${ContainerPort}" `
    $ImageName | Out-Null

Write-Step "Container status"
docker --context $Context ps --filter "name=$ContainerName"

Write-Step "Container image digest"
docker --context $Context inspect $ContainerName --format "{{.Image}}"

Write-Step "Recent logs"
docker --context $Context logs --tail 50 $ContainerName

Write-Step "Done"
Write-Host "Health check URL: http://127.0.0.1:$HostPort/api/health" -ForegroundColor Green

if (-not $NoFollow) {
    Write-Step "Following logs for '$ContainerName' (Ctrl+C to stop)"
    docker --context $Context logs -f $ContainerName
}
