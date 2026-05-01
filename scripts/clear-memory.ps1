param(
    [string]$Context = "",
    [string]$ContainerName = "im-agent",
    [string]$SessionId,
    [switch]$All,
    [switch]$DropAliases,
    [switch]$KeepDocuments
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
from app.db.database import SessionLocal
from app.db.models import AppSetting

def clear_all_session_documents():
    with SessionLocal() as session:
        keys = [
            row[0]
            for row in session.query(AppSetting.key).all()
            if str(row[0] or "").startswith(("session_doc:", "session_docs:"))
        ]
        if not keys:
            return {"scope": "all", "deleted_app_settings": 0}
        deleted = (
            session.query(AppSetting)
            .filter(AppSetting.key.in_(keys))
            .delete(synchronize_session=False)
        )
        session.commit()
        return {"scope": "all", "deleted_app_settings": deleted}

result = {
    "memory": MemoryService().clear_all_memory(keep_aliases=$keepAliases),
    "session_documents": (
        {"skipped": True}
        if $($KeepDocuments.ToString())
        else clear_all_session_documents()
    ),
}
print(json.dumps(result, ensure_ascii=False))
"@
}
else {
    $sessionJson = ($SessionId | ConvertTo-Json -Compress)
    Write-Step "Clearing persisted memory for session '$SessionId' in container '$ContainerName'"
    $pythonCode = @"
import json
from app.services.memory_service import MemoryService
from app.db.database import SessionLocal
from app.db.models import AppSetting

def clear_session_documents(session_id):
    keys = [f"session_doc:{session_id}", f"session_docs:{session_id}"]
    with SessionLocal() as session:
        deleted = (
            session.query(AppSetting)
            .filter(AppSetting.key.in_(keys))
            .delete(synchronize_session=False)
        )
        session.commit()
        return {
            "scope": "session",
            "session_id": session_id,
            "deleted_app_settings": deleted,
            "keys": keys,
        }

result = {
    "memory": MemoryService().clear_session_memory(session_id=$sessionJson, keep_aliases=$keepAliases),
    "session_documents": (
        {"skipped": True, "session_id": $sessionJson}
        if $($KeepDocuments.ToString())
        else clear_session_documents($sessionJson)
    ),
}
print(json.dumps(result, ensure_ascii=False))
"@
}

$pythonCode | docker @dockerArgs exec -i $ContainerName python -
if ($LASTEXITCODE -ne 0) {
    throw "Failed to clear memory in container '$ContainerName'."
}

Write-Step "Done"
if ($All) {
    Write-Host "All persisted demo memory has been cleared." -ForegroundColor Green
}
else {
    Write-Host "Session '$SessionId' has been reset." -ForegroundColor Green
}
if ($KeepDocuments) {
    Write-Host "Session document pointers were kept because -KeepDocuments was provided." -ForegroundColor Yellow
}
else {
    Write-Host "Remembered Feishu document pointers/history were cleared. Remote Feishu docs were not deleted." -ForegroundColor Green
}
