import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import desc, select

from app.db.database import SessionLocal
from app.db.models import Artifact, ConfirmationRequest, Requirement, TaskRun, TaskRunStep
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
from app.services.task_run_state import TERMINAL_STATUSES, transition_task_run_status
from app.utils.values import coerce_positive_int


logger = logging.getLogger(__name__)


def _ordered_unique(values: list[str]) -> list[str]:
    ordered: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if normalized and normalized not in ordered:
            ordered.append(normalized)
    return ordered


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
        requirement_id: str | None = None,
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
                requirement_id=requirement_id,
                session_id=session_id,
                source_type=source_type,
                source_ref=source_ref,
                trigger_message_id=trigger_message_id,
                created_by=created_by,
                intent=intent,
                title=title.strip()[:255] or "协作运行",
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
        requirement_id: str | None = None,
        session_query: str | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> list[TaskRunSummary]:
        with SessionLocal() as session:
            query_active = bool((session_query or "").strip())
            statement = select(TaskRun).order_by(desc(TaskRun.id)).limit(limit if not query_active else max(limit * 4, limit))
            if session_id:
                statement = statement.where(TaskRun.session_id == session_id)
            if requirement_id:
                statement = statement.where(TaskRun.requirement_id == requirement_id)
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
            artifacts = self._artifacts_for_task_run_detail(session, row)
            confirmations = session.execute(
                select(ConfirmationRequest)
                .where(ConfirmationRequest.task_run_id == task_run_id)
                .order_by(ConfirmationRequest.id.asc())
            ).scalars().all()

            step_records = [self._step_from_row(item) for item in steps]
            metadata = self._decode_json_object(row.metadata_json)
            detail = TaskRunDetail(
                **self._summary_from_row(row).model_dump(),
                metadata_json=row.metadata_json,
                graph_trace=self._build_graph_trace(metadata, step_records),
                steps=step_records,
                artifacts=[self._artifact_from_row(item) for item in artifacts],
                confirmations=[self._confirmation_from_row(item) for item in confirmations],
                session_documents=self._session_documents_for_task_run_detail(session, row),
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
                transition = transition_task_run_status(row.status, status)
                if transition.accepted:
                    row.status = transition.requested_status
                    if transition.completed_at is not None and row.completed_at is None:
                        row.completed_at = transition.completed_at
                    elif transition.requested_status not in TERMINAL_STATUSES:
                        row.completed_at = None
                else:
                    logger.warning(
                        "Rejected task run status transition: task_run_id=%s current=%s requested=%s reason=%s",
                        task_run_id,
                        transition.current_status,
                        transition.requested_status,
                        transition.reason,
                    )
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
                ).with_for_update()
            ).scalar_one_or_none()
            if row is None:
                return None

            if row.status == "answered":
                response = ConfirmationAnswerResponse(
                    task_run_id=task_run_id,
                    confirmation_id=confirmation_id,
                    status=row.status,
                    answer_value=row.answer_value or answer_value,
                    already_answered=True,
                )
                return response

            row.status = "answered"
            row.answer_value = answer_value
            row.answered_by = answered_by
            row.answered_at = now

            task_run = session.execute(
                select(TaskRun).where(TaskRun.task_run_id == task_run_id)
            ).scalar_one_or_none()
            if task_run is not None and task_run.status == "waiting_confirmation":
                transition = transition_task_run_status(task_run.status, "running")
                if transition.accepted:
                    task_run.status = transition.requested_status
                    task_run.completed_at = None
                else:
                    logger.warning(
                        "Rejected confirmation task run transition: task_run_id=%s current=%s requested=%s reason=%s",
                        task_run_id,
                        transition.current_status,
                        transition.requested_status,
                        transition.reason,
                    )
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
        metadata = self._decode_json_object(row.metadata_json)
        return TaskRunSummary(
            task_run_id=row.task_run_id,
            requirement_id=row.requirement_id,
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
            run_kind=str(metadata.get("run_kind") or "").strip() or None,
            primary_object=str(metadata.get("primary_object") or "").strip() or None,
            lifecycle_stage=str(metadata.get("lifecycle_stage") or "").strip() or None,
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

    def _artifacts_for_task_run_detail(self, session, row: TaskRun) -> list[Artifact]:
        run_ids = self._context_task_run_ids(session, row)
        if row.task_run_id not in run_ids:
            run_ids.append(row.task_run_id)
        artifacts = session.execute(
            select(Artifact)
            .where(Artifact.task_run_id.in_(run_ids))
            .order_by(Artifact.id.asc())
        ).scalars().all()
        seen: set[str] = set()
        deduped: list[Artifact] = []
        for artifact in artifacts:
            artifact_id = str(artifact.artifact_id or "")
            if artifact_id in seen:
                continue
            seen.add(artifact_id)
            deduped.append(artifact)
        if row.requirement_id:
            return self._requirement_current_artifact_view(session, row, deduped)
        return deduped

    def _requirement_current_artifact_view(self, session, row: TaskRun, artifacts: list[Artifact]) -> list[Artifact]:
        requirement = session.execute(
            select(Requirement).where(Requirement.requirement_id == row.requirement_id)
        ).scalar_one_or_none()
        current_by_group = {
            group: artifact_id
            for group, artifact_id in {
                "slides": str(getattr(requirement, "current_slides_artifact_id", None) or "").strip(),
                "canvas": str(getattr(requirement, "current_canvas_artifact_id", None) or "").strip(),
                "delivery": str(getattr(requirement, "current_delivery_artifact_id", None) or "").strip(),
            }.items()
            if artifact_id
        }
        latest_by_group: dict[str, Artifact] = {}
        passthrough: list[Artifact] = []
        for artifact in artifacts:
            group = self._singleton_artifact_group(str(artifact.artifact_type or ""))
            if not group:
                passthrough.append(artifact)
                continue
            current_id = current_by_group.get(group)
            if current_id and artifact.artifact_id != current_id:
                continue
            current = latest_by_group.get(group)
            if current is None or self._artifact_sort_key(artifact) >= self._artifact_sort_key(current):
                latest_by_group[group] = artifact
        selected = list(latest_by_group.values()) + passthrough
        selected.sort(key=lambda item: item.id)
        return selected

    @staticmethod
    def _singleton_artifact_group(artifact_type: str) -> str:
        normalized = artifact_type.strip()
        if normalized in {"slides", "slides_package"}:
            return "slides"
        if normalized == "canvas":
            return "canvas"
        if normalized == "delivery_bundle":
            return "delivery"
        return ""

    @staticmethod
    def _artifact_sort_key(artifact: Artifact) -> tuple:
        return (
            artifact.updated_at or artifact.created_at,
            coerce_positive_int(artifact.version),
            artifact.id,
        )

    def _context_task_run_ids(self, session, row: TaskRun) -> list[str]:
        if row.requirement_id:
            return list(
                session.execute(
                    select(TaskRun.task_run_id)
                    .where(TaskRun.requirement_id == row.requirement_id)
                    .order_by(TaskRun.id.asc())
                ).scalars().all()
            )
        return list(
            session.execute(
                select(TaskRun.task_run_id)
                .where(TaskRun.session_id == row.session_id)
                .order_by(desc(TaskRun.id))
                .limit(20)
            ).scalars().all()
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

    def _session_documents_for_task_run_detail(self, session, row: TaskRun) -> list[SessionDocumentRecord]:
        documents = self._session_documents_for_sessions(
            self._context_session_ids(session, row) if row.requirement_id else [row.session_id]
        )
        if not row.requirement_id:
            return documents
        allowed_run_ids = set(self._context_task_run_ids(session, row))
        allowed_doc_ids: set[str] = set()
        requirement = session.execute(
            select(Requirement.current_document_id).where(Requirement.requirement_id == row.requirement_id)
        ).scalar_one_or_none()
        if requirement:
            allowed_doc_ids.add(str(requirement))
        return [
            document
            for document in documents
            if (document.task_run_id and document.task_run_id in allowed_run_ids)
            or (document.document_id and document.document_id in allowed_doc_ids)
        ]

    def _context_session_ids(self, session, row: TaskRun) -> list[str]:
        if not row.requirement_id:
            return [row.session_id]
        session_ids = list(
            session.execute(
                select(TaskRun.session_id)
                .where(TaskRun.requirement_id == row.requirement_id)
                .order_by(TaskRun.id.asc())
            ).scalars().all()
        )
        return _ordered_unique([row.session_id, *session_ids])

    def _session_documents_for_sessions(self, session_ids: list[str]) -> list[SessionDocumentRecord]:
        documents: list[SessionDocumentRecord] = []
        seen: set[tuple[str, str]] = set()
        for session_id in _ordered_unique(session_ids):
            for document in self._session_documents_for_session(session_id):
                key = (document.session_id, document.document_id)
                if key in seen:
                    continue
                seen.add(key)
                documents.append(document)
        return documents

    def _session_documents_for_session(self, session_id: str) -> list[SessionDocumentRecord]:
        try:
            payloads = self.session_document_service.list_documents(session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load session documents for task run detail: session_id=%s error=%s", session_id, exc)
            return []
        return [self._session_document_from_payload(item) for item in payloads]

    def _build_graph_trace(self, metadata: dict, steps: list[TaskRunStepRecord]) -> dict | None:
        execution = metadata.get("langgraph_execution")
        shadow = metadata.get("langgraph_shadow")
        if isinstance(execution, dict):
            source = "execution"
            payload = execution
        elif isinstance(shadow, dict):
            source = "shadow"
            payload = shadow
        else:
            return None

        command = payload.get("command") if isinstance(payload.get("command"), dict) else {}
        plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else {}
        raw_plan_steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
        worker_results = payload.get("worker_results") if isinstance(payload.get("worker_results"), dict) else {}
        worker_steps = self._graph_worker_steps(steps)
        worker_status_by_id = {item["step_id"]: item for item in worker_steps if item.get("step_id")}

        plan_steps = []
        for item in raw_plan_steps:
            if not isinstance(item, dict):
                continue
            step_id = str(item.get("step_id") or "")
            worker_status = worker_status_by_id.get(step_id, {})
            plan_steps.append(
                {
                    "step_id": step_id,
                    "worker": str(item.get("worker") or ""),
                    "operation": str(item.get("operation") or ""),
                    "agent": str((item.get("input") or {}).get("agent") or "") if isinstance(item.get("input"), dict) else "",
                    "goal": self._truncate_text((item.get("input") or {}).get("goal") or "") if isinstance(item.get("input"), dict) else "",
                    "depends_on": item.get("depends_on") if isinstance(item.get("depends_on"), list) else [],
                    "can_run_parallel": bool(item.get("can_run_parallel")),
                    "status": worker_status.get("status") or self._worker_result_status(worker_results.get(step_id)),
                    "elapsed_ms": worker_status.get("elapsed_ms") or self._worker_result_elapsed(worker_results.get(step_id)),
                }
            )

        workers = worker_steps
        for step_id, result in worker_results.items():
            if not isinstance(result, dict) or step_id in worker_status_by_id:
                continue
            workers.append(self._worker_result_trace(str(step_id), result))

        review = payload.get("review") if isinstance(payload.get("review"), dict) else None
        reply = payload.get("reply") if isinstance(payload.get("reply"), dict) else None
        trace = payload.get("trace") if isinstance(payload.get("trace"), list) else []
        errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
        return {
            "source": source,
            "command": self._compact_graph_command(command),
            "plan": {
                "plan_id": str(plan.get("plan_id") or ""),
                "step_count": len(plan_steps),
                "parallel_step_count": len([item for item in plan_steps if item.get("can_run_parallel")]),
                "steps": plan_steps,
            },
            "workers": workers,
            "review": self._compact_graph_review(review),
            "reply_preview": self._truncate_text(reply.get("text") if isinstance(reply, dict) else ""),
            "legacy_route": payload.get("legacy_route") if isinstance(payload.get("legacy_route"), dict) else None,
            "comparison": self._compact_graph_comparison(payload.get("comparison")),
            "trace": [item for item in trace if isinstance(item, dict)],
            "errors": [item for item in errors if isinstance(item, dict)],
        }

    def _graph_worker_steps(self, steps: list[TaskRunStepRecord]) -> list[dict]:
        workers: list[dict] = []
        for step in steps:
            if step.step_type != "graph_worker":
                continue
            payload = self._decode_json_object(step.output_json)
            output = payload.get("output") if isinstance(payload.get("output"), dict) else {}
            workers.append(
                {
                    "step_key": step.step_key,
                    "step_id": str(payload.get("step_id") or step.step_key.removeprefix("graph.worker.")),
                    "worker": str(payload.get("worker") or ""),
                    "operation": str(payload.get("operation") or ""),
                    "status": step.status,
                    "elapsed_ms": self._float_or_none(payload.get("elapsed_ms")),
                    "reply_preview": self._truncate_text(output.get("reply_preview") or output.get("summary") or ""),
                    "artifact_count": len(output.get("artifacts")) if isinstance(output.get("artifacts"), list) else 0,
                    "error": step.error,
                }
            )
        return workers

    def _worker_result_trace(self, step_id: str, result: dict) -> dict:
        output = result.get("output") if isinstance(result.get("output"), dict) else {}
        return {
            "step_id": step_id,
            "worker": str(result.get("worker") or ""),
            "operation": "",
            "status": str(result.get("status") or ("done" if result.get("ok") else "failed")),
            "elapsed_ms": self._float_or_none(result.get("elapsed_ms")),
            "reply_preview": self._truncate_text(output.get("reply_preview") or output.get("summary") or ""),
            "artifact_count": len(output.get("artifacts")) if isinstance(output.get("artifacts"), list) else 0,
            "error": str(result.get("error") or "") or None,
        }

    def _compact_graph_command(self, command: dict) -> dict:
        return {
            "mode": str(command.get("mode") or ""),
            "operation": str(command.get("operation") or ""),
            "object": str(command.get("object") or ""),
            "target_text": self._truncate_text(command.get("target_text") or "", max_chars=120),
            "target_owner": str(command.get("target_owner") or ""),
            "target_status": str(command.get("target_status") or ""),
            "requested_outputs": command.get("requested_outputs") if isinstance(command.get("requested_outputs"), list) else [],
            "artifact_goals": command.get("artifact_goals") if isinstance(command.get("artifact_goals"), dict) else {},
            "destructive": bool(command.get("destructive")),
            "batch": bool(command.get("batch")),
            "confidence": self._float_or_none(command.get("confidence")),
            "needs_clarification": bool(command.get("needs_clarification")),
            "reason": self._truncate_text(command.get("reason") or ""),
        }

    def _compact_graph_review(self, review: dict | None) -> dict | None:
        if not isinstance(review, dict):
            return None
        clarification = review.get("clarification") if isinstance(review.get("clarification"), dict) else {}
        return {
            "ok": bool(review.get("ok")),
            "needs_clarification": bool(review.get("needs_clarification")),
            "risks": review.get("risks") if isinstance(review.get("risks"), list) else [],
            "missing_outputs": review.get("missing_outputs") if isinstance(review.get("missing_outputs"), list) else [],
            "checks": self._compact_graph_review_checks(review.get("checks")),
            "clarification_question": self._truncate_text(clarification.get("question") or ""),
        }

    def _compact_graph_review_checks(self, checks: object) -> list[dict]:
        if not isinstance(checks, list):
            return []
        compacted: list[dict] = []
        for item in checks:
            if not isinstance(item, dict):
                continue
            compacted.append(
                {
                    "agent": str(item.get("agent") or ""),
                    "status": str(item.get("status") or ""),
                    "ok": bool(item.get("ok")),
                    "summary": self._truncate_text(item.get("summary") or ""),
                    "risk_count": len(item.get("risks")) if isinstance(item.get("risks"), list) else 0,
                }
            )
        return compacted

    def _compact_graph_comparison(self, comparison: object) -> dict | None:
        if not isinstance(comparison, dict):
            return None
        return {
            "status": str(comparison.get("status") or ""),
            "route_match": bool(comparison.get("route_match")),
            "outputs_match": bool(comparison.get("outputs_match")),
            "needs_clarification_match": bool(comparison.get("needs_clarification_match")),
            "legacy_route": str(comparison.get("legacy_route") or ""),
            "graph_route": str(comparison.get("graph_route") or ""),
            "legacy_outputs": comparison.get("legacy_outputs") if isinstance(comparison.get("legacy_outputs"), list) else [],
            "graph_outputs": comparison.get("graph_outputs") if isinstance(comparison.get("graph_outputs"), list) else [],
            "legacy_confidence": self._float_or_none(comparison.get("legacy_confidence")),
            "graph_confidence": self._float_or_none(comparison.get("graph_confidence")),
            "confidence_delta": self._float_or_none(comparison.get("confidence_delta")),
            "notes": comparison.get("notes") if isinstance(comparison.get("notes"), list) else [],
        }

    @staticmethod
    def _worker_result_status(result: object) -> str:
        if isinstance(result, dict):
            return str(result.get("status") or ("done" if result.get("ok") else "failed"))
        return ""

    def _worker_result_elapsed(self, result: object) -> float | None:
        if isinstance(result, dict):
            return self._float_or_none(result.get("elapsed_ms"))
        return None

    @staticmethod
    def _decode_json_object(raw: str | None) -> dict:
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _float_or_none(value: object) -> float | None:
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _truncate_text(value: object, *, max_chars: int = 220) -> str:
        text = str(value or "").strip()
        if len(text) <= max_chars:
            return text
        return f"{text[: max_chars - 3].rstrip()}..."

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
            "requirement_id": detail.requirement_id,
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
            "run_kind": detail.run_kind,
            "primary_object": detail.primary_object,
            "lifecycle_stage": detail.lifecycle_stage,
            "created_by": detail.created_by,
            "created_at": detail.created_at.isoformat() if detail.created_at else None,
            "updated_at": detail.updated_at.isoformat() if detail.updated_at else None,
            "completed_at": detail.completed_at.isoformat() if detail.completed_at else None,
        }

    def _task_room(self, task_run_id: str) -> str:
        return realtime_hub.task_run_room(task_run_id)

    def _session_room(self, session_id: str) -> str:
        return realtime_hub.session_room(session_id)
