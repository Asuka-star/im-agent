import json
import logging
import threading
from datetime import datetime, timezone

from sqlalchemy import delete, desc, select

from app.core.config import settings
from app.db.database import SessionLocal
from app.db.models import Episode, Memory, MemoryChunk, Message, Session, Task, TaskChangeLog
from app.schemas.analyze import AnalyzeResponse
from app.schemas.task import TaskItem
from app.services.embeddings import EmbeddingService

logger = logging.getLogger(__name__)


class MemoryService:
    """Stores discussion history, active episodes, task snapshots, and memory context."""

    def __init__(self) -> None:
        self.embedding_service = EmbeddingService()

    def ensure_session(self, session_id: str) -> None:
        with SessionLocal() as session:
            existing = session.execute(
                select(Session.id).where(Session.session_id == session_id)
            ).scalar_one_or_none()
            if existing is None:
                session.add(Session(session_id=session_id))
                session.commit()

    def get_active_episode(self, session_id: str) -> Episode | None:
        with SessionLocal() as session:
            return session.execute(
                select(Episode)
                .where(Episode.session_id == session_id, Episode.status == "active")
                .order_by(desc(Episode.id))
                .limit(1)
            ).scalar_one_or_none()

    def ensure_active_episode(self, session_id: str) -> Episode:
        self.ensure_session(session_id)
        with SessionLocal() as session:
            episode = session.execute(
                select(Episode)
                .where(Episode.session_id == session_id, Episode.status == "active")
                .order_by(desc(Episode.id))
                .limit(1)
            ).scalar_one_or_none()
            if episode is not None:
                return episode

            episode = Episode(session_id=session_id, status="active")
            session.add(episode)
            session.commit()
            session.refresh(episode)
            logger.info("Opened new discussion episode: session_id=%s episode_id=%s", session_id, episode.id)
            return episode

    def close_active_episode(self, session_id: str, *, title: str | None = None) -> int | None:
        with SessionLocal() as session:
            episode = session.execute(
                select(Episode)
                .where(Episode.session_id == session_id, Episode.status == "active")
                .order_by(desc(Episode.id))
                .limit(1)
            ).scalar_one_or_none()
            if episode is None:
                return None

            episode.status = "closed"
            episode.closed_at = datetime.now(timezone.utc)
            if title:
                episode.title = title.strip()[:255]
            session.commit()
            logger.info("Closed discussion episode: session_id=%s episode_id=%s", session_id, episode.id)
            return episode.id

    def save_user_message(
        self,
        *,
        session_id: str,
        message_id: str | None,
        sender_id: str | None,
        content: str,
        episode_id: int | None = None,
        embed: bool = True,
    ) -> None:
        self.ensure_session(session_id)
        with SessionLocal() as session:
            if message_id:
                existing = session.execute(
                    select(Message.id).where(Message.message_id == message_id)
                ).scalar_one_or_none()
                if existing is not None:
                    return

            session.add(
                Message(
                    message_id=message_id,
                    session_id=session_id,
                    episode_id=episode_id,
                    role="user",
                    sender_id=sender_id,
                    content=content,
                )
            )
            session.commit()

        self.save_memory_chunk(
            session_id=session_id,
            source_type="message",
            source_id=message_id,
            content=content,
            metadata={"sender_id": sender_id or "", "role": "user", "episode_id": episode_id},
            embed=embed,
        )

    def save_assistant_message(
        self,
        *,
        session_id: str,
        content: str,
        episode_id: int | None = None,
        embed: bool = True,
    ) -> None:
        self.ensure_session(session_id)
        with SessionLocal() as session:
            session.add(
                Message(
                    session_id=session_id,
                    episode_id=episode_id,
                    role="assistant",
                    sender_id="assistant",
                    content=content,
                )
            )
            session.commit()

        self.save_memory_chunk(
            session_id=session_id,
            source_type="assistant_reply",
            source_id=None,
            content=content,
            metadata={"role": "assistant", "episode_id": episode_id},
            embed=embed,
        )

    def save_round(
        self,
        *,
        session_id: str,
        analysis: AnalyzeResponse,
        episode_id: int | None = None,
        embed: bool = True,
        async_embed: bool = False,
    ) -> None:
        self.ensure_session(session_id)
        previous_tasks = self.get_current_tasks(session_id)
        payload = {
            "summary": analysis.summary,
            "risks": analysis.risks,
            "next_actions": analysis.next_actions,
            "episode_id": episode_id,
        }

        with SessionLocal() as session:
            session.add(
                Memory(
                    session_id=session_id,
                    summary=analysis.summary,
                    payload=json.dumps(payload, ensure_ascii=False),
                )
            )

            session.query(Task).filter(Task.session_id == session_id).delete()
            for task in analysis.tasks:
                session.add(
                    Task(
                        session_id=session_id,
                        title=task.title,
                        owner=task.owner,
                        priority=task.priority,
                        due_date=task.due_date,
                        status=task.status,
                        notes=task.notes,
                    )
                )

            session.commit()

        self._record_task_changes(
            session_id=session_id,
            episode_id=episode_id,
            previous_tasks=previous_tasks,
            current_tasks=analysis.tasks,
            reason=analysis.summary,
        )

        chunk_kwargs = {
            "session_id": session_id,
            "source_type": "summary",
            "source_id": None,
            "content": analysis.summary,
            "metadata": {
                "risks": analysis.risks,
                "next_actions": analysis.next_actions,
                "task_count": len(analysis.tasks),
                "episode_id": episode_id,
            },
            "embed": embed,
        }
        if async_embed and embed and self.embedding_service.is_configured():
            threading.Thread(target=self.save_memory_chunk, kwargs=chunk_kwargs, daemon=True).start()
        else:
            self.save_memory_chunk(**chunk_kwargs)

    def save_memory_chunk(
        self,
        *,
        session_id: str,
        source_type: str,
        source_id: str | None,
        content: str,
        metadata: dict | None = None,
        embed: bool = True,
    ) -> None:
        if not content.strip():
            return

        embedding = None
        if embed and self.embedding_service.is_configured():
            try:
                embedding = self.embedding_service.embed_text(content)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Embedding write skipped due to embedding error: %s", exc)
                embedding = None

        with SessionLocal() as session:
            session.add(
                MemoryChunk(
                    session_id=session_id,
                    source_type=source_type,
                    source_id=source_id,
                    content=content,
                    metadata_json=json.dumps(metadata or {}, ensure_ascii=False),
                    embedding=embedding,
                )
            )
            session.commit()

        self._prune_memory_chunks(session_id=session_id, source_type=source_type)

    def get_recent_messages(self, session_id: str, limit: int = 12) -> list[Message]:
        with SessionLocal() as session:
            return (
                session.execute(
                    select(Message)
                    .where(Message.session_id == session_id)
                    .order_by(desc(Message.id))
                    .limit(limit)
                )
                .scalars()
                .all()
            )

    def get_current_tasks(self, session_id: str) -> list[Task]:
        with SessionLocal() as session:
            return (
                session.execute(
                    select(Task)
                    .where(Task.session_id == session_id)
                    .order_by(Task.id.asc())
                )
                .scalars()
                .all()
            )

    def get_recent_memories(self, session_id: str, limit: int = 3) -> list[Memory]:
        with SessionLocal() as session:
            return (
                session.execute(
                    select(Memory)
                    .where(Memory.session_id == session_id)
                    .order_by(desc(Memory.id))
                    .limit(limit)
                )
                .scalars()
                .all()
            )

    def get_recent_task_changes(self, session_id: str, limit: int = 8) -> list[TaskChangeLog]:
        with SessionLocal() as session:
            return (
                session.execute(
                    select(TaskChangeLog)
                    .where(TaskChangeLog.session_id == session_id)
                    .order_by(desc(TaskChangeLog.id))
                    .limit(limit)
                )
                .scalars()
                .all()
            )

    def search_relevant_memories(
        self,
        session_id: str,
        query_text: str,
        *,
        limit: int = 5,
    ) -> list[MemoryChunk]:
        if not self.embedding_service.is_configured() or not query_text.strip():
            return []

        try:
            query_embedding = self.embedding_service.embed_text(query_text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Embedding retrieval skipped due to embedding error: %s", exc)
            return []

        with SessionLocal() as session:
            statement = (
                select(MemoryChunk)
                .where(
                    MemoryChunk.session_id == session_id,
                    MemoryChunk.embedding.is_not(None),
                )
                .order_by(MemoryChunk.embedding.cosine_distance(query_embedding))
                .limit(limit)
            )
            return session.execute(statement).scalars().all()

    def get_episode_messages(
        self,
        session_id: str,
        *,
        episode_id: int | None,
        exclude_message_id: str | None = None,
        limit: int = 20,
    ) -> list[Message]:
        if episode_id is None:
            return []

        with SessionLocal() as session:
            statement = (
                select(Message)
                .where(
                    Message.session_id == session_id,
                    Message.role == "user",
                    Message.episode_id == episode_id,
                )
                .order_by(Message.id.asc())
            )
            if exclude_message_id:
                statement = statement.where(Message.message_id != exclude_message_id)

            messages = session.execute(statement).scalars().all()
            return messages[-limit:] if limit and len(messages) > limit else messages

    def build_discussion_block(
        self,
        session_id: str,
        *,
        episode_id: int | None = None,
        exclude_message_id: str | None = None,
        limit: int = 20,
    ) -> str:
        target_episode_id = episode_id
        if target_episode_id is None:
            active_episode = self.get_active_episode(session_id)
            target_episode_id = active_episode.id if active_episode else None

        messages = self.get_episode_messages(
            session_id,
            episode_id=target_episode_id,
            exclude_message_id=exclude_message_id,
            limit=limit,
        )
        if not messages:
            return ""

        lines = ["[近期群聊讨论]"]
        for message in messages:
            speaker = message.sender_id or "成员"
            lines.append(f"- {speaker}: {message.content}")
        return "\n".join(lines)

    def build_workspace_context(
        self,
        session_id: str,
        *,
        include_pending: bool = True,
        exclude_message_id: str | None = None,
        query_text: str | None = None,
        include_semantic_search: bool = True,
        episode_id: int | None = None,
    ) -> str:
        tasks = self.get_current_tasks(session_id)
        memories = self.get_recent_memories(session_id)
        task_changes = self.get_recent_task_changes(session_id)
        recent_messages = self.get_recent_messages(session_id)
        retrieved_chunks = (
            self.search_relevant_memories(session_id, query_text or "", limit=5)
            if include_semantic_search
            else []
        )
        pending_block = (
            self.build_discussion_block(
                session_id,
                episode_id=episode_id,
                exclude_message_id=exclude_message_id,
            )
            if include_pending
            else ""
        )

        if not tasks and not memories and not task_changes and not recent_messages and not pending_block and not retrieved_chunks:
            return ""

        lines: list[str] = ["[协作上下文]"]

        if pending_block:
            lines.append(pending_block)

        if tasks:
            lines.append("[当前任务快照]")
            for task in tasks:
                lines.append(
                    f"- {task.title} | 负责人: {task.owner} | 截止: {task.due_date} | 优先级: {task.priority} | 状态: {task.status}"
                )

        if task_changes:
            lines.append("[任务变更记录]")
            for change in reversed(task_changes):
                line = (
                    f"- {change.action}: {change.title} | 负责人: {change.owner} | 截止: {change.due_date} | 优先级: {change.priority}"
                )
                if change.reason:
                    line += f" | 原因: {change.reason}"
                lines.append(line)

        if memories:
            lines.append("[最近总结]")
            for memory in reversed(memories):
                lines.append(f"- {memory.summary}")

        if retrieved_chunks:
            lines.append("[相关历史记忆]")
            for chunk in retrieved_chunks:
                lines.append(f"- ({chunk.source_type}) {chunk.content}")

        if recent_messages:
            lines.append("[最近消息]")
            for message in reversed(recent_messages[-6:]):
                speaker = message.sender_id or ("成员" if message.role == "user" else "助手")
                lines.append(f"- {speaker}: {message.content}")

        return "\n".join(lines)

    def load_memory_payload(self, session_id: str) -> dict:
        memories = self.get_recent_memories(session_id, limit=1)
        if not memories or not memories[0].payload:
            return {}

        try:
            payload = json.loads(memories[0].payload)
        except json.JSONDecodeError:
            return {}

        return payload if isinstance(payload, dict) else {}

    def _record_task_changes(
        self,
        *,
        session_id: str,
        episode_id: int | None,
        previous_tasks: list[Task],
        current_tasks: list[TaskItem],
        reason: str,
    ) -> None:
        previous_map = {self._task_key(task.title): task for task in previous_tasks}
        current_map = {self._task_key(task.title): task for task in current_tasks}
        rows: list[TaskChangeLog] = []

        for key in sorted(set(previous_map) | set(current_map)):
            before = previous_map.get(key)
            after = current_map.get(key)

            if before is None and after is not None:
                rows.append(
                    TaskChangeLog(
                        session_id=session_id,
                        episode_id=episode_id,
                        action="created",
                        title=after.title,
                        owner=after.owner,
                        priority=after.priority,
                        due_date=after.due_date,
                        status=after.status,
                        notes=after.notes,
                        reason=reason,
                        details_json=json.dumps({"before": None, "after": after.model_dump()}, ensure_ascii=False),
                    )
                )
                continue

            if before is not None and after is None:
                rows.append(
                    TaskChangeLog(
                        session_id=session_id,
                        episode_id=episode_id,
                        action="removed",
                        title=before.title,
                        owner=before.owner,
                        priority=before.priority,
                        due_date=before.due_date,
                        status=before.status,
                        notes=before.notes,
                        reason=reason,
                        details_json=json.dumps(
                            {"before": self._task_snapshot(before), "after": None},
                            ensure_ascii=False,
                        ),
                    )
                )
                continue

            if before is not None and after is not None:
                before_snapshot = self._task_snapshot(before)
                after_snapshot = after.model_dump()
                if before_snapshot != after_snapshot:
                    changed_fields = [field for field in after_snapshot if before_snapshot.get(field) != after_snapshot.get(field)]
                    rows.append(
                        TaskChangeLog(
                            session_id=session_id,
                            episode_id=episode_id,
                            action="updated",
                            title=after.title,
                            owner=after.owner,
                            priority=after.priority,
                            due_date=after.due_date,
                            status=after.status,
                            notes=after.notes,
                            reason=reason,
                            details_json=json.dumps(
                                {
                                    "before": before_snapshot,
                                    "after": after_snapshot,
                                    "changed_fields": changed_fields,
                                },
                                ensure_ascii=False,
                            ),
                        )
                    )

        if not rows:
            return

        with SessionLocal() as session:
            for row in rows:
                session.add(row)
            session.commit()

    def _prune_memory_chunks(self, *, session_id: str, source_type: str) -> None:
        keep_count = self._chunk_keep_count(source_type)
        if keep_count <= 0:
            return

        with SessionLocal() as session:
            stale_ids = (
                session.execute(
                    select(MemoryChunk.id)
                    .where(
                        MemoryChunk.session_id == session_id,
                        MemoryChunk.source_type == source_type,
                    )
                    .order_by(desc(MemoryChunk.id))
                    .offset(keep_count)
                )
                .scalars()
                .all()
            )
            if not stale_ids:
                return

            session.execute(delete(MemoryChunk).where(MemoryChunk.id.in_(stale_ids)))
            session.commit()
            logger.info(
                "Pruned stale memory chunks: session_id=%s source_type=%s removed=%s keep=%s",
                session_id,
                source_type,
                len(stale_ids),
                keep_count,
            )

    def _chunk_keep_count(self, source_type: str) -> int:
        if source_type == "message":
            return settings.memory_message_chunk_keep
        if source_type == "assistant_reply":
            return settings.memory_assistant_chunk_keep
        if source_type == "summary":
            return settings.memory_summary_chunk_keep
        return settings.memory_summary_chunk_keep

    def _task_key(self, title: str) -> str:
        return " ".join((title or "").lower().split())

    def _task_snapshot(self, task: Task) -> dict:
        return {
            "title": task.title,
            "owner": task.owner,
            "priority": task.priority,
            "due_date": task.due_date,
            "status": task.status,
            "notes": task.notes or "",
        }
