from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, func, select

from app.db.database import SessionLocal
from app.db.models import Artifact, Message, Requirement, RequirementSource, TaskRun, UserAlias
from app.schemas.next_action import NextActionBundle
from app.schemas.requirement import (
    RequirementDetail,
    RequirementSourceRecord,
    RequirementSummary,
    RequirementTimelineItem,
)
from app.schemas.task_run import ArtifactRecord, SessionDocumentRecord, TaskRunSummary
from app.services.next_action_service import ContextualNextActionService
from app.services.realtime_hub import realtime_hub
from app.services.session_display_service import SessionDisplayService
from app.services.session_document_service import SessionDocumentService
from app.services.task_run_service import TaskRunService
from app.utils.values import coerce_positive_int

logger = logging.getLogger(__name__)


class RequirementService:
    """Stores and aggregates requirement-level collaboration workspaces."""

    def __init__(
        self,
        *,
        task_run_service: TaskRunService | None = None,
        session_document_service: SessionDocumentService | None = None,
        next_action_service: ContextualNextActionService | None = None,
        session_display_service: SessionDisplayService | None = None,
    ) -> None:
        self.task_run_service = task_run_service or TaskRunService()
        self.session_document_service = session_document_service or SessionDocumentService()
        self.next_action_service = next_action_service or ContextualNextActionService()
        self.session_display_service = session_display_service or SessionDisplayService()

    def create_requirement(
        self,
        *,
        title: str,
        primary_session_id: str,
        summary: str | None = None,
        created_by: str | None = None,
        source_message_id: str | None = None,
        source_type: str = "im",
        metadata: dict[str, Any] | None = None,
    ) -> RequirementSummary:
        requirement_id = f"req_{uuid.uuid4().hex[:12]}"
        cleaned_title = _clean_title(title) or "未命名需求"
        with SessionLocal() as session:
            row = Requirement(
                requirement_id=requirement_id,
                title=cleaned_title[:255],
                summary=str(summary or "").strip() or None,
                primary_session_id=primary_session_id,
                created_by=created_by,
                metadata_json=json.dumps(metadata or {}, ensure_ascii=False),
            )
            session.add(row)
            session.flush()
            self._add_source_row(
                session,
                requirement_id=requirement_id,
                session_id=primary_session_id,
                message_id=source_message_id,
                source_type=source_type,
            )
            session.commit()
            session.refresh(row)
            summary_record = self._summary_from_row(row)
        self._publish_requirement_event(summary_record, event_type="requirement.created")
        return summary_record

    def list_requirements(
        self,
        *,
        session_id: str | None = None,
        status: str | None = "active",
        query: str | None = None,
        limit: int = 20,
    ) -> list[RequirementSummary]:
        with SessionLocal() as session:
            statement = select(Requirement).order_by(desc(Requirement.updated_at), desc(Requirement.id))
            if status:
                statement = statement.where(Requirement.status == status)
            if session_id:
                source_requirement_ids = (
                    select(RequirementSource.requirement_id)
                    .where(RequirementSource.session_id == session_id)
                )
                statement = statement.where(
                    (Requirement.primary_session_id == session_id)
                    | (Requirement.requirement_id.in_(source_requirement_ids))
                )
            rows = session.execute(statement.limit(max(limit * 3, limit))).scalars().all()
            requirement_ids = [row.requirement_id for row in rows]
            source_counts = self._source_counts(session, requirement_ids)
            task_run_counts = self._task_run_counts(session, requirement_ids)
            latest_source_types = self._latest_source_types(session, requirement_ids)
            items = [
                self._summary_from_row(
                    row,
                    source_count=source_counts.get(row.requirement_id, 0),
                    task_run_count=task_run_counts.get(row.requirement_id, 0),
                    latest_source_type=latest_source_types.get(row.requirement_id),
                )
                for row in rows
            ]
        normalized_query = _compact(query)
        if normalized_query:
            items = [
                item
                for item in items
                if normalized_query in _compact(item.title)
                or normalized_query in _compact(item.summary)
                or normalized_query in _compact(item.requirement_id)
                or normalized_query in _compact(item.primary_session_id)
                or normalized_query in _compact(item.primary_session_label)
            ]
        return items[:limit]

    def get_requirement(self, requirement_id: str) -> RequirementDetail | None:
        with SessionLocal() as session:
            row = session.execute(
                select(Requirement).where(Requirement.requirement_id == requirement_id)
            ).scalar_one_or_none()
            if row is None:
                return None
            sources = session.execute(
                select(RequirementSource)
                .where(RequirementSource.requirement_id == requirement_id)
                .order_by(RequirementSource.id.asc())
            ).scalars().all()
            task_runs = session.execute(
                select(TaskRun)
                .where(TaskRun.requirement_id == requirement_id)
                .order_by(TaskRun.id.asc())
            ).scalars().all()
            task_run_ids = [item.task_run_id for item in task_runs]
            artifacts = []
            if task_run_ids:
                artifacts = session.execute(
                    select(Artifact)
                    .where(Artifact.task_run_id.in_(task_run_ids))
                    .order_by(Artifact.id.asc())
                ).scalars().all()

            summary = self._summary_from_row(row)
            task_summaries = [self._task_summary_from_row(item) for item in task_runs]
            artifact_records = [self._artifact_from_row(item) for item in artifacts]
            source_records = [self._source_from_row(item) for item in sources]
            current_document = self._current_document_for_requirement(
                summary,
                session_ids=self._document_session_ids(row.primary_session_id, sources, task_runs),
            )
            current_slides = self._latest_artifact(artifact_records, {"slides", "slides_package"}, row.current_slides_artifact_id)
            current_canvas = self._latest_artifact(artifact_records, {"canvas"}, row.current_canvas_artifact_id)
            current_delivery = self._latest_artifact(artifact_records, {"delivery_bundle"}, row.current_delivery_artifact_id)
            recommendations = self._recommendations_for_requirement(task_summaries)
            return RequirementDetail(
                **summary.model_dump(),
                sources=source_records,
                task_runs=task_summaries,
                timeline=self._timeline(task_summaries),
                current_document=current_document,
                current_slides=current_slides,
                current_canvas=current_canvas,
                current_delivery=current_delivery,
                recommendations=recommendations,
            )

    def bind_task_run(
        self,
        *,
        task_run_id: str,
        requirement_id: str,
        session_id: str | None = None,
        message_id: str | None = None,
        source_type: str = "task_run",
    ) -> TaskRunSummary | None:
        requirement_summary: RequirementSummary | None = None
        with SessionLocal() as session:
            requirement = session.execute(
                select(Requirement).where(Requirement.requirement_id == requirement_id)
            ).scalar_one_or_none()
            task_run = session.execute(
                select(TaskRun).where(TaskRun.task_run_id == task_run_id)
            ).scalar_one_or_none()
            if requirement is None or task_run is None:
                return None
            task_run.requirement_id = requirement_id
            self._add_source_row(
                session,
                requirement_id=requirement_id,
                session_id=session_id or task_run.session_id,
                message_id=message_id or task_run.trigger_message_id,
                source_type=source_type,
            )
            self._update_current_artifact_pointers(session, requirement, task_run_id)
            self._touch_requirement(requirement)
            session.commit()
            session.refresh(task_run)
            session.refresh(requirement)
            task_summary = self._task_summary_from_row(task_run)
            requirement_summary = self._summary_from_row(requirement)
        if requirement_summary is not None:
            self._publish_requirement_event(requirement_summary, event_type="requirement.updated")
        return task_summary

    def record_source(
        self,
        *,
        requirement_id: str,
        session_id: str,
        message_id: str | None = None,
        source_type: str = "im_passive",
        touch: bool = True,
    ) -> RequirementSummary | None:
        requirement_summary: RequirementSummary | None = None
        with SessionLocal() as session:
            requirement = session.execute(
                select(Requirement).where(Requirement.requirement_id == requirement_id)
            ).scalar_one_or_none()
            if requirement is None:
                return None
            self._add_source_row(
                session,
                requirement_id=requirement_id,
                session_id=session_id,
                message_id=message_id,
                source_type=source_type,
            )
            if touch:
                self._touch_requirement(requirement)
            session.commit()
            session.refresh(requirement)
            requirement_summary = self._summary_from_row(requirement)
        if requirement_summary is not None:
            self._publish_requirement_event(requirement_summary, event_type="requirement.updated")
        return requirement_summary

    def update_current_artifacts_from_task_run(self, task_run_id: str) -> None:
        try:
            requirement_summary: RequirementSummary | None = None
            with SessionLocal() as session:
                task_run = session.execute(
                    select(TaskRun).where(TaskRun.task_run_id == task_run_id)
                ).scalar_one_or_none()
                if task_run is None or not task_run.requirement_id:
                    return
                requirement = session.execute(
                    select(Requirement).where(Requirement.requirement_id == task_run.requirement_id)
                ).scalar_one_or_none()
                if requirement is None:
                    return
                artifacts = session.execute(
                    select(Artifact).where(Artifact.task_run_id == task_run_id).order_by(Artifact.id.asc())
                ).scalars().all()
                if self._apply_current_artifacts(requirement, artifacts):
                    session.commit()
                    session.refresh(requirement)
                    requirement_summary = self._summary_from_row(requirement)
            if requirement_summary is not None:
                self._publish_requirement_event(requirement_summary, event_type="requirement.updated")
        except Exception as exc:  # noqa: BLE001
            logger.debug("Skipped requirement artifact pointer update: task_run_id=%s error=%s", task_run_id, exc)

    def _add_source_row(
        self,
        session,
        *,
        requirement_id: str,
        session_id: str,
        message_id: str | None,
        source_type: str,
    ) -> None:
        filters = [
            RequirementSource.requirement_id == requirement_id,
            RequirementSource.session_id == session_id,
            RequirementSource.source_type == source_type,
        ]
        if message_id:
            filters.append(RequirementSource.message_id == message_id)
        else:
            filters.append(RequirementSource.message_id.is_(None))
        existing = session.execute(select(RequirementSource).where(*filters)).scalar_one_or_none()
        if existing is not None:
            return
        session.add(
            RequirementSource(
                source_id=f"reqsrc_{uuid.uuid4().hex[:12]}",
                requirement_id=requirement_id,
                session_id=session_id,
                message_id=message_id,
                source_type=source_type,
            )
        )

    def _update_current_artifact_pointers(self, session, requirement: Requirement, task_run_id: str) -> bool:
        artifacts = session.execute(
            select(Artifact).where(Artifact.task_run_id == task_run_id).order_by(Artifact.id.asc())
        ).scalars().all()
        return self._apply_current_artifacts(requirement, artifacts)

    def _apply_current_artifacts(self, requirement: Requirement, artifacts: list[Artifact]) -> bool:
        changed = False
        for artifact in artifacts:
            artifact_type = str(artifact.artifact_type or "")
            if artifact_type in {"slides", "slides_package"}:
                if requirement.current_slides_artifact_id != artifact.artifact_id:
                    requirement.current_slides_artifact_id = artifact.artifact_id
                    changed = True
            elif artifact_type == "canvas":
                if requirement.current_canvas_artifact_id != artifact.artifact_id:
                    requirement.current_canvas_artifact_id = artifact.artifact_id
                    changed = True
            elif artifact_type == "delivery_bundle":
                if requirement.current_delivery_artifact_id != artifact.artifact_id:
                    requirement.current_delivery_artifact_id = artifact.artifact_id
                    changed = True
            elif artifact_type in {"document", "doc", "feishu_doc"}:
                document_id = self._document_id_from_artifact(artifact)
                if document_id and requirement.current_document_id != document_id:
                    requirement.current_document_id = document_id
                    changed = True
        return changed

    @staticmethod
    def _touch_requirement(requirement: Requirement) -> None:
        requirement.updated_at = datetime.now(timezone.utc)

    def _summary_from_row(
        self,
        row: Requirement,
        *,
        source_count: int | None = None,
        task_run_count: int | None = None,
        latest_source_type: str | None = None,
    ) -> RequirementSummary:
        if source_count is None or task_run_count is None or latest_source_type is None:
            counts = self._requirement_counts(row.requirement_id)
            if source_count is None:
                source_count = counts["source_count"]
            if task_run_count is None:
                task_run_count = counts["task_run_count"]
            if latest_source_type is None:
                latest_source_type = counts["latest_source_type"]
        return RequirementSummary(
            requirement_id=row.requirement_id,
            title=row.title,
            status=row.status,
            summary=row.summary,
            primary_session_id=row.primary_session_id,
            primary_session_label=self.session_display_service.resolve_session_label(
                session_id=row.primary_session_id,
                source_type="group" if str(row.primary_session_id or "").startswith("oc_") else None,
            ),
            source_count=max(int(source_count or 0), 0),
            task_run_count=max(int(task_run_count or 0), 0),
            latest_source_type=latest_source_type,
            current_document_id=row.current_document_id,
            current_slides_artifact_id=row.current_slides_artifact_id,
            current_canvas_artifact_id=row.current_canvas_artifact_id,
            current_delivery_artifact_id=row.current_delivery_artifact_id,
            created_by=row.created_by,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def _requirement_counts(self, requirement_id: str) -> dict[str, Any]:
        with SessionLocal() as session:
            return {
                "source_count": self._source_counts(session, [requirement_id]).get(requirement_id, 0),
                "task_run_count": self._task_run_counts(session, [requirement_id]).get(requirement_id, 0),
                "latest_source_type": self._latest_source_types(session, [requirement_id]).get(requirement_id),
            }

    @staticmethod
    def _source_counts(session, requirement_ids: list[str]) -> dict[str, int]:
        if not requirement_ids:
            return {}
        rows = session.execute(
            select(RequirementSource.requirement_id, func.count(RequirementSource.id))
            .where(RequirementSource.requirement_id.in_(requirement_ids))
            .group_by(RequirementSource.requirement_id)
        ).all()
        return {str(requirement_id): int(count or 0) for requirement_id, count in rows}

    @staticmethod
    def _task_run_counts(session, requirement_ids: list[str]) -> dict[str, int]:
        if not requirement_ids:
            return {}
        rows = session.execute(
            select(TaskRun.requirement_id, func.count(TaskRun.id))
            .where(TaskRun.requirement_id.in_(requirement_ids))
            .group_by(TaskRun.requirement_id)
        ).all()
        return {str(requirement_id): int(count or 0) for requirement_id, count in rows if requirement_id}

    @staticmethod
    def _latest_source_types(session, requirement_ids: list[str]) -> dict[str, str]:
        if not requirement_ids:
            return {}
        sources = session.execute(
            select(RequirementSource)
            .where(RequirementSource.requirement_id.in_(requirement_ids))
            .order_by(RequirementSource.id.desc())
        ).scalars().all()
        latest: dict[str, str] = {}
        for source in sources:
            latest.setdefault(source.requirement_id, source.source_type)
        return latest

    @staticmethod
    def _publish_requirement_event(record: RequirementSummary, *, event_type: str) -> None:
        realtime_hub.emit_room(
            realtime_hub.all_task_runs_room(),
            {
                "type": event_type,
                "requirement": record.model_dump(mode="json"),
            },
        )

    def _task_summary_from_row(self, row: TaskRun) -> TaskRunSummary:
        metadata = _decode_json_object(row.metadata_json)
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

    def _source_from_row(self, row: RequirementSource) -> RequirementSourceRecord:
        message = self._message_for_source(row.message_id)
        session_type = self._source_session_type(row.session_id, row.source_type)
        sender_id = str(getattr(message, "sender_id", None) or "").strip() or None
        return RequirementSourceRecord(
            source_id=row.source_id,
            requirement_id=row.requirement_id,
            session_id=row.session_id,
            session_label=self._source_session_label(row.session_id, session_type=session_type, sender_id=sender_id),
            message_id=row.message_id,
            sender_id=sender_id,
            sender_label=self._sender_label(row.session_id, sender_id),
            session_type=session_type,
            message_text=str(getattr(message, "content", "") or "").strip() or None,
            message_status=str(getattr(message, "status", "") or "").strip() or None,
            source_type=row.source_type,
            created_at=row.created_at,
        )

    @staticmethod
    def _message_for_source(message_id: str | None) -> Message | None:
        normalized = str(message_id or "").strip()
        if not normalized:
            return None
        with SessionLocal() as session:
            return session.execute(
                select(Message).where(Message.message_id == normalized)
            ).scalar_one_or_none()

    def _source_session_label(self, session_id: str, *, session_type: str, sender_id: str | None) -> str | None:
        return self.session_display_service.resolve_session_label(
            session_id=session_id,
            source_type="group" if session_type == "group" else "p2p",
            source_ref=session_id if session_type == "group" else None,
            created_by=sender_id,
        )

    def _sender_label(self, session_id: str, sender_id: str | None) -> str | None:
        normalized = str(sender_id or "").strip()
        if not normalized:
            return None
        memory_service = getattr(self.session_display_service, "memory_service", None)
        label = memory_service.get_alias_display_name(session_id, normalized) if memory_service else None
        if not label:
            label = self._alias_label(session_id, normalized)
        if label:
            return label
        return self.session_display_service.resolve_session_label(
            session_id=session_id,
            source_type="p2p",
            created_by=normalized,
        ) or normalized

    @staticmethod
    def _alias_label(session_id: str, sender_id: str) -> str | None:
        with SessionLocal() as session:
            row = session.execute(
                select(UserAlias).where(
                    UserAlias.session_id == session_id,
                    (UserAlias.user_id == sender_id)
                    | (UserAlias.open_id == sender_id)
                    | (UserAlias.union_id == sender_id),
                )
            ).scalar_one_or_none()
            return row.display_name if row else None

    @staticmethod
    def _source_session_type(session_id: str, source_type: str | None) -> str:
        normalized_source = str(source_type or "").lower()
        normalized_session = str(session_id or "").strip()
        if "group" in normalized_source or normalized_session.startswith("oc_"):
            return "group"
        if "p2p" in normalized_source or normalized_session:
            return "p2p"
        return "unknown"

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

    def _current_document_for_requirement(
        self,
        requirement: RequirementSummary,
        *,
        session_ids: list[str] | None = None,
    ) -> SessionDocumentRecord | None:
        if not requirement.current_document_id:
            return None
        for session_id in self._ordered_session_ids(requirement.primary_session_id, session_ids or []):
            try:
                documents = [
                    self.task_run_service._session_document_from_payload(item)
                    for item in self.session_document_service.list_documents(session_id)
                ]
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "Failed to load requirement documents: requirement_id=%s session_id=%s error=%s",
                    requirement.requirement_id,
                    session_id,
                    exc,
                )
                continue
            for document in documents:
                if document.document_id == requirement.current_document_id:
                    return document
        return None

    @staticmethod
    def _document_session_ids(
        primary_session_id: str,
        sources: list[RequirementSource],
        task_runs: list[TaskRun],
    ) -> list[str]:
        candidates = [primary_session_id]
        candidates.extend(str(getattr(source, "session_id", "") or "") for source in sources)
        candidates.extend(str(getattr(task_run, "session_id", "") or "") for task_run in task_runs)
        return RequirementService._ordered_session_ids(primary_session_id, candidates)

    @staticmethod
    def _ordered_session_ids(primary_session_id: str, session_ids: list[str]) -> list[str]:
        ordered: list[str] = []
        for session_id in [primary_session_id, *session_ids]:
            value = str(session_id or "").strip()
            if value and value not in ordered:
                ordered.append(value)
        return ordered

    @staticmethod
    def _latest_artifact(
        artifacts: list[ArtifactRecord],
        artifact_types: set[str],
        preferred_artifact_id: str | None,
    ) -> ArtifactRecord | None:
        if preferred_artifact_id:
            for artifact in artifacts:
                if artifact.artifact_id == preferred_artifact_id:
                    return artifact
        for artifact in reversed(artifacts):
            if artifact.artifact_type in artifact_types:
                return artifact
        return None

    def _recommendations_for_requirement(self, task_runs: list[TaskRunSummary]) -> NextActionBundle | None:
        for task_run in reversed(task_runs):
            detail = self.task_run_service.get_task_run(task_run.task_run_id)
            if detail is None:
                continue
            return self.next_action_service.build_for_task_run(detail)
        return None

    @staticmethod
    def _timeline(task_runs: list[TaskRunSummary]) -> list[RequirementTimelineItem]:
        return [
            RequirementTimelineItem(
                item_id=item.task_run_id,
                item_type="task_run",
                title=item.title,
                status=item.status,
                stage=item.stage,
                summary=item.latest_summary or item.latest_reply_preview,
                created_at=item.created_at,
                updated_at=item.updated_at,
                metadata={
                    "intent": item.intent,
                    "run_kind": item.run_kind,
                    "primary_object": item.primary_object,
                    "lifecycle_stage": item.lifecycle_stage,
                },
            )
            for item in task_runs
        ]

    @staticmethod
    def _document_id_from_artifact(artifact: Artifact) -> str | None:
        preview = _decode_json_object(artifact.preview_json)
        sync = preview.get("sync") if isinstance(preview.get("sync"), dict) else {}
        document_id = str(sync.get("document_id") or "").strip()
        return document_id or None


def _decode_json_object(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _clean_title(value: str | None) -> str:
    return " ".join(str(value or "").split()).strip()


def _compact(value: str | None) -> str:
    return "".join(str(value or "").lower().split())
