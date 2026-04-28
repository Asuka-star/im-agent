param(
    [string]$Context = "",
    [string]$ContainerName = "im-agent",
    [string]$SessionId,
    [switch]$All
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
    throw "Please provide -SessionId <chat_id/session_id>, or use -All to clear every stored task run."
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

if ($All) {
    Write-Step "Clearing all task runs in container '$ContainerName'"
    $pythonCode = @"
import json
from app.db.database import SessionLocal
from app.db.models import Artifact, ConfirmationRequest, TaskRun, TaskRunStep

with SessionLocal() as session:
    deleted = {}
    deleted['confirmation_requests'] = session.query(ConfirmationRequest).delete(synchronize_session=False)
    deleted['artifacts'] = session.query(Artifact).delete(synchronize_session=False)
    deleted['task_run_steps'] = session.query(TaskRunStep).delete(synchronize_session=False)
    deleted['task_runs'] = session.query(TaskRun).delete(synchronize_session=False)
    session.commit()

print(json.dumps({
    'scope': 'all',
    'deleted': deleted,
}, ensure_ascii=False))
"@
}
else {
    $sessionJson = ($SessionId | ConvertTo-Json -Compress)
    Write-Step "Clearing task runs for session '$SessionId' in container '$ContainerName'"
    $pythonCode = @"
import json
from app.db.database import SessionLocal
from app.db.models import Artifact, ConfirmationRequest, TaskRun, TaskRunStep

with SessionLocal() as session:
    task_run_ids = [
        row[0]
        for row in session.query(TaskRun.task_run_id).filter(TaskRun.session_id == $sessionJson).all()
    ]
    deleted = {}
    if task_run_ids:
        deleted['confirmation_requests'] = (
            session.query(ConfirmationRequest)
            .filter(ConfirmationRequest.task_run_id.in_(task_run_ids))
            .delete(synchronize_session=False)
        )
        deleted['artifacts'] = (
            session.query(Artifact)
            .filter(Artifact.task_run_id.in_(task_run_ids))
            .delete(synchronize_session=False)
        )
        deleted['task_run_steps'] = (
            session.query(TaskRunStep)
            .filter(TaskRunStep.task_run_id.in_(task_run_ids))
            .delete(synchronize_session=False)
        )
        deleted['task_runs'] = (
            session.query(TaskRun)
            .filter(TaskRun.session_id == $sessionJson)
            .delete(synchronize_session=False)
        )
    else:
        deleted = {
            'confirmation_requests': 0,
            'artifacts': 0,
            'task_run_steps': 0,
            'task_runs': 0,
        }
    session.commit()

print(json.dumps({
    'scope': 'session',
    'session_id': $sessionJson,
    'deleted': deleted,
}, ensure_ascii=False))
"@
}

docker @dockerArgs exec $ContainerName python -c $pythonCode

Write-Step "Done"
if ($All) {
    Write-Host "All task runs have been cleared." -ForegroundColor Green
}
else {
    Write-Host "Task runs for session '$SessionId' have been cleared." -ForegroundColor Green
}
