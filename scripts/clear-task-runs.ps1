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
from app.db.models import Artifact, ConfirmationRequest, Requirement, RequirementSource, TaskRun, TaskRunStep

with SessionLocal() as session:
    deleted = {}
    deleted['confirmation_requests'] = session.query(ConfirmationRequest).delete(synchronize_session=False)
    deleted['artifacts'] = session.query(Artifact).delete(synchronize_session=False)
    deleted['task_run_steps'] = session.query(TaskRunStep).delete(synchronize_session=False)
    deleted['task_runs'] = session.query(TaskRun).delete(synchronize_session=False)
    deleted['requirement_sources'] = session.query(RequirementSource).delete(synchronize_session=False)
    deleted['requirements'] = session.query(Requirement).delete(synchronize_session=False)
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
from app.db.models import Artifact, ConfirmationRequest, Requirement, RequirementSource, TaskRun, TaskRunStep

with SessionLocal() as session:
    task_run_ids = [
        row[0]
        for row in session.query(TaskRun.task_run_id).filter(TaskRun.session_id == $sessionJson).all()
    ]
    requirement_ids = {
        row[0]
        for row in session.query(Requirement.requirement_id)
        .filter(Requirement.primary_session_id == $sessionJson)
        .all()
    }
    requirement_ids.update(
        row[0]
        for row in session.query(RequirementSource.requirement_id)
        .filter(RequirementSource.session_id == $sessionJson)
        .all()
    )
    if task_run_ids:
        requirement_ids.update(
            row[0]
            for row in session.query(TaskRun.requirement_id)
            .filter(TaskRun.task_run_id.in_(task_run_ids), TaskRun.requirement_id.isnot(None))
            .all()
        )
    requirement_ids = {item for item in requirement_ids if item}
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
    if requirement_ids:
        deleted['task_run_requirement_links'] = (
            session.query(TaskRun)
            .filter(TaskRun.requirement_id.in_(requirement_ids))
            .update({"requirement_id": None}, synchronize_session=False)
        )
        deleted['requirement_sources'] = (
            session.query(RequirementSource)
            .filter(RequirementSource.requirement_id.in_(requirement_ids))
            .delete(synchronize_session=False)
        )
        deleted['requirements'] = (
            session.query(Requirement)
            .filter(Requirement.requirement_id.in_(requirement_ids))
            .delete(synchronize_session=False)
        )
    else:
        deleted['task_run_requirement_links'] = 0
        deleted['requirement_sources'] = 0
        deleted['requirements'] = 0
    session.commit()

print(json.dumps({
    'scope': 'session',
    'session_id': $sessionJson,
    'requirement_ids': sorted(requirement_ids),
    'deleted': deleted,
}, ensure_ascii=False))
"@
}

$pythonCode | docker @dockerArgs exec -i $ContainerName python -
if ($LASTEXITCODE -ne 0) {
    throw "Failed to clear task runs in container '$ContainerName'."
}

Write-Step "Done"
if ($All) {
    Write-Host "All task runs and requirement workspaces have been cleared." -ForegroundColor Green
}
else {
    Write-Host "Task runs and requirement workspaces for session '$SessionId' have been cleared." -ForegroundColor Green
}
