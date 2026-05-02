import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import desc, select

from app.db.database import SessionLocal
from app.db.models import Artifact, ConfirmationRequest, TaskRun, TaskRunStep
from app.schemas.task_run import (
    ArtifactCheckRecord,
    ArtifactRecord,
    ConfirmationAnswerResponse,
    ConfirmationRequestRecord,
    ContextPackRecord,
    SessionDocumentRecord,
    TaskRunDetail,
    TaskRunStepRecord,
    TaskRunSummary,
)
from app.services.realtime_hub import realtime_hub
from app.services.session_display_service import SessionDisplayService
from app.services.session_document_service import SessionDocumentService
from app.services.task_artifact_verifier import TaskArtifactVerifier
from app.services.task_context_pack import TaskContextPackBuilder
from app.utils.values import coerce_positive_int


logger = logging.getLogger(__name__)


class TaskRunService:
    """Stores task-run level state for the dashboard and multi-end collaboration."""

    def __init__(
        self,
        *,
        session_display_service: SessionDisplayService | None = None,
        session_document_service: SessionDocumentService | None = None,
        artifact_verifier: TaskArtifactVerifier | None = None,
        context_pack_builder: TaskContextPackBuilder | None = None,
    ) -> None:
        self.session_display_service = session_display_service or SessionDisplayService()
        self.session_document_service = session_document_service or SessionDocumentService()
        self.artifact_verifier = artifact_verifier or TaskArtifactVerifier()
        self.context_pack_builder = context_pack_builder or TaskContextPackBuilder()

    def create_task_run(
        self,
        *,
        session_id: str,
        title: str,
        source_type: str,
        source_ref: str | None = None,
        trigger_message_id: str | None = None,
        created_by: str | None = None,
        intent: str | None = None,
        metadata: dict | None = None,
    ) -> TaskRunSummary:
        task_run_id = f"run_{uuid.uuid4().hex[:12]}"
        with SessionLocal() as session:
            row = TaskRun(
                task_run_id=task_run_id,
                session_id=session_id,
                source_type=source_type,
                source_ref=source_ref,
                trigger_message_id=trigger_message_id,
                created_by=created_by,
                intent=intent,
                title=title.strip()[:255] or "协作任务",
                metadata_json=json.dumps(metadata or {}, ensure_ascii=False),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            summary = self._summary_from_row(row)

        self._publish_task_run_event(task_run_id, event_type="task_run.created")
        return summary

    def list_task_runs(
        self,
        *,
        session_id: str | None = None,
        session_query: str | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> list[TaskRunSummary]:
        with SessionLocal() as session:
            query_active = bool((session_query or "").strip())
            statement = select(TaskRun).order_by(desc(TaskRun.id)).limit(limit if not query_active else max(limit * 4, limit))
            if session_id:
                statement = statement.where(TaskRun.session_id == session_id)
            if status:
                statement = statement.where(TaskRun.status == status)
            rows = session.execute(statement).scalars().all()
            summaries = [self._summary_from_row(row) for row in rows]
            normalized_query = (session_query or "").strip().lower()
            if normalized_query:
                summaries = [
                    item
                    for item in summaries
                    if normalized_query
                    in ((item.session_label or item.session_id).strip().lower())
                ]
            return summaries[:limit]

    def get_task_run(self, task_run_id: str) -> TaskRunDetail | None:
        with SessionLocal() as session:
            row = session.execute(
                select(TaskRun).where(TaskRun.task_run_id == task_run_id)
            ).scalar_one_or_none()
            if row is None:
                return None

            steps = session.execute(
                select(TaskRunStep)
                .where(TaskRunStep.task_run_id == task_run_id)
                .order_by(TaskRunStep.id.asc())
            ).scalars().all()
            artifacts = session.execute(
                select(Artifact)
                .where(Artifact.task_run_id == task_run_id)
                .order_by(Artifact.id.asc())
            ).scalars().all()
            confirmations = session.execute(
                select(ConfirmationRequest)
                .where(ConfirmationRequest.task_run_id == task_run_id)
                .order_by(ConfirmationRequest.id.asc())
            ).scalars().all()

            detail = TaskRunDetail(
                **self._summary_from_row(row).model_dump(),
                metadata_json=row.metadata_json,
                steps=[self._step_from_row(item) for item in steps],
                artifacts=[self._artifact_from_row(item) for item in artifacts],
                confirmations=[self._confirmation_from_row(item) for item in confirmations],
                session_documents=self._session_documents_for_session(row.session_id),
            )
            detail.artifact_checks = [
                ArtifactCheckRecord(**item)
                for item in self.artifact_verifier.build_for_task_run(detail)
            ]
            detail.context_pack = ContextPackRecord(**self.context_pack_builder.build_for_task_run(detail))
            return detail

    def update_task_run(
        self,
        task_run_id: str,
        *,
        intent: str | None = None,
        title: str | None = None,
        stage: str | None = None,
        status: str | None = None,
        latest_summary: str | None = None,
        latest_reply_preview: str | None = None,
        latest_error: str | None = None,
        metadata: dict | None = None,
    ) -> TaskRunSummary | None:
        with SessionLocal() as session:
            row = session.execute(
                select(TaskRun).where(TaskRun.task_run_id == task_run_id)
            ).scalar_one_or_none()
            if row is None:
                return None

            if intent is not None:
                row.intent = intent
            if title:
                row.title = title.strip()[:255]
            if stage:
                row.stage = stage
            if status:
                row.status = status
                if status in {"completed", "failed"}:
                    row.completed_at = datetime.now(timezone.utc)
            if latest_summary is not None:
                row.latest_summary = latest_summary
            if latest_reply_preview is not None:
                row.latest_reply_preview = latest_reply_preview
            if latest_error is not None:
                row.latest_error = latest_error
            if metadata is not None:
                row.metadata_json = json.dumps(metadata, ensure_ascii=False)

            session.commit()
            session.refresh(row)
            summary = self._summary_from_row(row)

        self._publish_task_run_event(task_run_id, event_type="task_run.updated")
        return summary

    def get_task_run_metadata(self, task_run_id: str) -> dict:
        detail = self.get_task_run(task_run_id)
        if detail is None or not detail.metadata_json:
            return {}
        try:
            parsed = json.loads(detail.metadata_json)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def merge_task_run_metadata(self, task_run_id: str, patch: dict) -> TaskRunSummary | None:
        current = self.get_task_run_metadata(task_run_id)
        current.update(patch)
        return self.update_task_run(task_run_id, metadata=current)

    def upsert_step(
        self,
        task_run_id: str,
        *,
        step_key: str,
        title: str,
        step_type: str = "system",
        status: str = "pending",
        input_payload: dict | None = None,
        output_payload: dict | None = None,
        error: str | None = None,
    ) -> TaskRunStepRecord:
        now = datetime.now(timezone.utc)
        with SessionLocal() as session:
            row = session.execute(
                select(TaskRunStep).where(
                    TaskRunStep.task_run_id == task_run_id,
                    TaskRunStep.step_key == step_key,
                )
            ).scalar_one_or_none()

            if row is None:
                row = TaskRunStep(
                    task_run_id=task_run_id,
                    step_key=step_key,
                    title=title,
                    step_type=step_type,
                    status=status,
                    input_json=json.dumps(input_payload, ensure_ascii=False) if input_payload is not None else None,
                    output_json=json.dumps(output_payload, ensure_ascii=False) if output_payload is not None else None,
                    error=error,
                )
                if status == "running":
                    row.started_at = now
                if status in {"done", "failed"}:
                    row.finished_at = now
                session.add(row)
            else:
                row.title = title
                row.step_type = step_type
                row.status = status
                if input_payload is not None:
                    row.input_json = json.dumps(input_payload, ensure_ascii=False)
                if output_payload is not None:
                    row.output_json = json.dumps(output_payload, ensure_ascii=False)
                if error is not None:
                    row.error = error
                if status == "running" and row.started_at is None:
                    row.started_at = now
                if status in {"done", "failed"}:
                    row.finished_at = now

            session.commit()
            session.refresh(row)
            step = self._step_from_row(row)

        self._publish_task_run_event(task_run_id, event_type="task_run.step_updated")
        return step

    def create_artifact(
        self,
        task_run_id: str,
        *,
        artifact_type: str,
        title: str,
        provider: str = "local",
        status: str = "ready",
        url: str | None = None,
        preview: dict | None = None,
        version: int = 1,
    ) -> ArtifactRecord:
        artifact_id = f"artifact_{uuid.uuid4().hex[:12]}"
        with SessionLocal() as session:
            row = Artifact(
                artifact_id=artifact_id,
                task_run_id=task_run_id,
                artifact_type=artifact_type,
                provider=provider,
                title=title.strip()[:255] or artifact_type,
                status=status,
                url=url,
                version=version,
                preview_json=json.dumps(preview, ensure_ascii=False) if preview is not None else None,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            artifact = self._artifact_from_row(row)

        self._publish_task_run_event(task_run_id, event_type="task_run.artifact_created")
        return artifact

    def create_confirmation(
        self,
        task_run_id: str,
        *,
        prompt: str,
        options: list[str] | None = None,
    ) -> ConfirmationRequestRecord:
        confirmation_id = f"confirm_{uuid.uuid4().hex[:12]}"
        with SessionLocal() as session:
            row = ConfirmationRequest(
                confirmation_id=confirmation_id,
                task_run_id=task_run_id,
                prompt=prompt,
                options_json=json.dumps(options or [], ensure_ascii=False),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            confirmation = self._confirmation_from_row(row)

        self._publish_task_run_event(task_run_id, event_type="task_run.confirmation_created")
        return confirmation

    def resolve_confirmation(
        self,
        task_run_id: str,
        *,
        confirmation_id: str,
        answer_value: str,
        answered_by: str,
    ) -> ConfirmationAnswerResponse | None:
        now = datetime.now(timezone.utc)
        with SessionLocal() as session:
            row = session.execute(
                select(ConfirmationRequest).where(
                    ConfirmationRequest.task_run_id == task_run_id,
                    ConfirmationRequest.confirmation_id == confirmation_id,
                )
            ).scalar_one_or_none()
            if row is None:
                return None

            row.status = "answered"
            row.answer_value = answer_value
            row.answered_by = answered_by
            row.answered_at = now

            task_run = session.execute(
                select(TaskRun).where(TaskRun.task_run_id == task_run_id)
            ).scalar_one_or_none()
            if task_run is not None and task_run.status == "waiting_confirmation":
                task_run.status = "running"
                task_run.stage = "confirmation_resolved"

            session.commit()
            response = ConfirmationAnswerResponse(
                task_run_id=task_run_id,
                confirmation_id=confirmation_id,
                status=row.status,
                answer_value=row.answer_value or answer_value,
            )

        self._publish_task_run_event(task_run_id, event_type="task_run.confirmation_answered")
        return response

    def _summary_from_row(self, row: TaskRun) -> TaskRunSummary:
        return TaskRunSummary(
            task_run_id=row.task_run_id,
            session_id=row.session_id,
            session_label=self.session_display_service.resolve_session_label(
                session_id=row.session_id,
                source_type=row.source_type,
                source_ref=row.source_ref,
                created_by=row.created_by,
            ),
            source_type=row.source_type,
            source_ref=row.source_ref,
            trigger_message_id=row.trigger_message_id,
            intent=row.intent,
            title=row.title,
            stage=row.stage,
            status=row.status,
            latest_summary=row.latest_summary,
            latest_reply_preview=row.latest_reply_preview,
            latest_error=row.latest_error,
            created_by=row.created_by,
            created_at=row.created_at,
            updated_at=row.updated_at,
            completed_at=row.completed_at,
        )

    def _step_from_row(self, row: TaskRunStep) -> TaskRunStepRecord:
        return TaskRunStepRecord(
            step_key=row.step_key,
            title=row.title,
            step_type=row.step_type,
            status=row.status,
            input_json=row.input_json,
            output_json=row.output_json,
            error=row.error,
            started_at=row.started_at,
            finished_at=row.finished_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def _artifact_from_row(self, row: Artifact) -> ArtifactRecord:
        return ArtifactRecord(
            artifact_id=row.artifact_id,
            artifact_type=row.artifact_type,
            provider=row.provider,
            title=row.title,
            status=row.status,
            url=row.url,
            version=row.version,
            preview_json=row.preview_json,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def _confirmation_from_row(self, row: ConfirmationRequest) -> ConfirmationRequestRecord:
        return ConfirmationRequestRecord(
            confirmation_id=row.confirmation_id,
            prompt=row.prompt,
            options_json=row.options_json,
            status=row.status,
            answer_value=row.answer_value,
            answered_by=row.answered_by,
            answered_at=row.answered_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def _session_document_from_payload(self, payload: dict) -> SessionDocumentRecord:
        updated_at = None
        raw_updated_at = str(payload.get("updated_at") or "").strip()
        if raw_updated_at:
            try:
                updated_at = datetime.fromisoformat(raw_updated_at)
            except ValueError:
                logger.warning(
                    "Ignoring invalid session document updated_at: session_id=%s document_id=%s updated_at=%s",
                    payload.get("session_id"),
                    payload.get("document_id"),
                    raw_updated_at,
                )
        return SessionDocumentRecord(
            session_id=str(payload.get("session_id") or ""),
            document_id=str(payload.get("document_id") or ""),
            url=str(payload.get("url") or "").strip() or None,
            title=str(payload.get("title") or ""),
            version=coerce_positive_int(payload.get("version")),
            sync_mode=str(payload.get("sync_mode") or "created"),
            task_run_id=str(payload.get("task_run_id") or "").strip() or None,
            updated_at=updated_at,
            is_current=bool(payload.get("is_current")),
        )

    def _session_documents_for_session(self, session_id: str) -> list[SessionDocumentRecord]:
        try:
            payloads = self.session_document_service.list_documents(session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load session documents for task run detail: session_id=%s error=%s", session_id, exc)
            return []
        return [self._session_document_from_payload(item) for item in payloads]

    def _publish_task_run_event(self, task_run_id: str, *, event_type: str) -> None:
        detail = self.get_task_run(task_run_id)
        if detail is None:
            return

        task_message = {
            "type": event_type,
            "task_run_id": task_run_id,
            "session_id": detail.session_id,
            "task_run": detail.model_dump(mode="json"),
        }
        session_message = {
            "type": event_type,
            "task_run_id": task_run_id,
            "session_id": detail.session_id,
            "task_run": self._summary_payload(detail),
        }
        realtime_hub.emit_room(self._task_room(task_run_id), task_message)
        realtime_hub.emit_room(self._session_room(detail.session_id), session_message)
        realtime_hub.emit_room(realtime_hub.all_task_runs_room(), session_message)

    def _summary_payload(self, detail: TaskRunDetail) -> dict:
        return {
            "task_run_id": detail.task_run_id,
            "session_id": detail.session_id,
            "session_label": detail.session_label,
            "source_type": detail.source_type,
            "source_ref": detail.source_ref,
            "trigger_message_id": detail.trigger_message_id,
            "intent": detail.intent,
            "title": detail.title,
            "stage": detail.stage,
            "status": detail.status,
            "latest_summary": detail.latest_summary,
            "latest_reply_preview": detail.latest_reply_preview,
            "latest_error": detail.latest_error,
            "created_by": detail.created_by,
            "created_at": detail.created_at.isoformat() if detail.created_at else None,
            "updated_at": detail.updated_at.isoformat() if detail.updated_at else None,
            "completed_at": detail.completed_at.isoformat() if detail.completed_at else None,
        }

    def _task_room(self, task_run_id: str) -> str:
        return realtime_hub.task_run_room(task_run_id)

    def _session_room(self, session_id: str) -> str:
        return realtime_hub.session_room(session_id)
