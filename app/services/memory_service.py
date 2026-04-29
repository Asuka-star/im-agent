import json
import logging
import threading
from datetime import datetime, timezone

from sqlalchemy import delete, desc, or_, select

from app.core.config import settings
from app.db.database import SessionLocal
from app.db.models import Episode, Memory, MemoryChunk, Message, Session, Task, TaskChangeLog, UserAlias
from app.schemas.analyze import AnalyzeResponse
from app.schemas.task import TaskItem
from app.services.app_state import AppStateService
from app.services.embeddings import EmbeddingService

logger = logging.getLogger(__name__)


class MemoryService:
    """Stores discussion history, active episodes, task snapshots, and memory context."""

    TEAM_GROUP_SESSIONS_KEY_PREFIX = "team_group_sessions"

    def __init__(self) -> None:
        self.embedding_service = EmbeddingService()
        self.app_state = AppStateService()

    def register_team_group_session(self, team_id: str | None, session_id: str) -> None:
        normalized_team_id = self.normalize_team_id(team_id)
        normalized_session_id = (session_id or "").strip()
        if not normalized_session_id:
            return

        sessions = self.get_team_group_sessions(normalized_team_id)
        if normalized_session_id not in sessions:
            sessions.append(normalized_session_id)
            self.app_state.set_value(
                self._team_group_sessions_key(normalized_team_id),
                json.dumps(sessions, ensure_ascii=False),
            )

    def get_team_group_sessions(self, team_id: str | None) -> list[str]:
        raw = self.app_state.get_value(self._team_group_sessions_key(self.normalize_team_id(team_id)))
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, list):
            return []
        result: list[str] = []
        for item in parsed:
            value = str(item or "").strip()
            if value and value not in result:
                result.append(value)
        return result

    def normalize_team_id(self, team_id: str | None) -> str:
        return (team_id or "").strip() or "default"

    def build_team_workspace_context(
        self,
        team_id: str | None,
        *,
        current_session_id: str | None = None,
        query_text: str | None = None,
        include_semantic_search: bool = False,
        max_sessions: int = 6,
    ) -> str:
        current = (current_session_id or "").strip()
        group_sessions = [
            session_id
            for session_id in self.get_team_group_sessions(team_id)[-max_sessions:]
            if session_id and session_id != current
        ]
        if not group_sessions:
            return ""

        sections: list[str] = []
        for session_id in group_sessions:
            context = self.build_workspace_context(
                session_id,
                include_pending=True,
                query_text=query_text,
                include_semantic_search=include_semantic_search,
            )
            if context:
                sections.append(f"[群聊会话: {session_id}]\n{context}")

        if not sections:
            return ""
        return "[团队群聊上下文]\n" + "\n\n".join(sections)

    def get_team_current_tasks(
        self,
        team_id: str | None,
        *,
        current_session_id: str | None = None,
    ) -> list[Task]:
        current = (current_session_id or "").strip()
        group_sessions = [
            session_id
            for session_id in self.get_team_group_sessions(team_id)
            if session_id and session_id != current
        ]
        if not group_sessions:
            return []
        with SessionLocal() as session:
            return (
                session.execute(
                    select(Task)
                    .where(Task.session_id.in_(group_sessions))
                    .order_by(Task.session_id.asc(), Task.id.asc())
                )
                .scalars()
                .all()
            )

    def load_team_memory_payload(
        self,
        team_id: str | None,
        *,
        current_session_id: str | None = None,
    ) -> dict:
        current = (current_session_id or "").strip()
        group_sessions = [
            session_id
            for session_id in self.get_team_group_sessions(team_id)
            if session_id and session_id != current
        ]
        if not group_sessions:
            return {}
        with SessionLocal() as session:
            memory = (
                session.execute(
                    select(Memory)
                    .where(Memory.session_id.in_(group_sessions), Memory.payload.is_not(None))
                    .order_by(desc(Memory.id))
                    .limit(1)
                )
                .scalars()
                .first()
            )
        if memory is None or not memory.payload:
            return {}
        try:
            payload = json.loads(memory.payload)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def ensure_session(self, session_id: str) -> None:
        with SessionLocal() as session:
            existing = session.execute(
                select(Session.id).where(Session.session_id == session_id)
            ).scalar_one_or_none()
            if existing is None:
                session.add(Session(session_id=session_id))
                session.commit()

    def _team_group_sessions_key(self, team_id: str) -> str:
        return f"{self.TEAM_GROUP_SESSIONS_KEY_PREFIX}:{team_id}"

    def clear_session_memory(self, session_id: str, *, keep_aliases: bool = True) -> dict:
        """Clear persisted memory for one discussion session.

        This is useful before recording a demo so the bot behaves like a fresh project thread.
        """
        normalized = (session_id or "").strip()
        if not normalized:
            raise ValueError("session_id is required")

        deleted: dict[str, int] = {}
        with SessionLocal() as session:
            deleted["messages"] = (
                session.query(Message).filter(Message.session_id == normalized).delete(synchronize_session=False)
            )
            deleted["tasks"] = session.query(Task).filter(Task.session_id == normalized).delete(synchronize_session=False)
            deleted["task_change_logs"] = (
                session.query(TaskChangeLog).filter(TaskChangeLog.session_id == normalized).delete(synchronize_session=False)
            )
            deleted["memories"] = (
                session.query(Memory).filter(Memory.session_id == normalized).delete(synchronize_session=False)
            )
            deleted["memory_chunks"] = (
                session.query(MemoryChunk).filter(MemoryChunk.session_id == normalized).delete(synchronize_session=False)
            )
            deleted["episodes"] = (
                session.query(Episode).filter(Episode.session_id == normalized).delete(synchronize_session=False)
            )
            if keep_aliases:
                deleted["user_aliases"] = 0
            else:
                deleted["user_aliases"] = (
                    session.query(UserAlias)
                    .filter(UserAlias.session_id == normalized)
                    .delete(synchronize_session=False)
                )
            session.commit()

        logger.info(
            "Cleared session memory: session_id=%s deleted=%s keep_aliases=%s",
            normalized,
            deleted,
            keep_aliases,
        )
        return {
            "scope": "session",
            "session_id": normalized,
            "keep_aliases": keep_aliases,
            "deleted": deleted,
        }

    def clear_all_memory(self, *, keep_aliases: bool = True) -> dict:
        """Clear all persisted demo memory across sessions."""
        deleted: dict[str, int] = {}
        with SessionLocal() as session:
            deleted["messages"] = session.query(Message).delete(synchronize_session=False)
            deleted["tasks"] = session.query(Task).delete(synchronize_session=False)
            deleted["task_change_logs"] = session.query(TaskChangeLog).delete(synchronize_session=False)
            deleted["memories"] = session.query(Memory).delete(synchronize_session=False)
            deleted["memory_chunks"] = session.query(MemoryChunk).delete(synchronize_session=False)
            deleted["episodes"] = session.query(Episode).delete(synchronize_session=False)
            if keep_aliases:
                deleted["user_aliases"] = 0
            else:
                deleted["user_aliases"] = session.query(UserAlias).delete(synchronize_session=False)
            deleted["sessions"] = session.query(Session).delete(synchronize_session=False)
            session.commit()

        logger.info("Cleared all persisted memory: deleted=%s keep_aliases=%s", deleted, keep_aliases)
        return {
            "scope": "all",
            "keep_aliases": keep_aliases,
            "deleted": deleted,
        }

    def get_alias_display_name(self, session_id: str, identifier: str | None) -> str | None:
        normalized = (identifier or "").strip()
        if not normalized:
            return None

        with SessionLocal() as session:
            row = session.execute(
                select(UserAlias).where(
                    UserAlias.session_id == session_id,
                    or_(
                        UserAlias.user_id == normalized,
                        UserAlias.open_id == normalized,
                        UserAlias.union_id == normalized,
                    ),
                )
            ).scalar_one_or_none()
            return row.display_name if row else None

    def upsert_user_alias(
        self,
        session_id: str,
        *,
        display_name: str,
        user_id: str | None = None,
        open_id: str | None = None,
        union_id: str | None = None,
    ) -> None:
        display_name = display_name.strip()
        if not display_name:
            return

        payload = [
            {
                "user_id": user_id,
                "open_id": open_id,
                "union_id": union_id,
                "display_name": display_name,
            }
        ]
        self._upsert_user_aliases(session_id, payload)

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
        mentioned_users: list[dict] | None = None,
        embed: bool = True,
    ) -> None:
        self.ensure_session(session_id)
        if mentioned_users:
            self._upsert_user_aliases(session_id, mentioned_users)
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
                    mentions_json=json.dumps(mentioned_users or [], ensure_ascii=False),
                    content=content,
                )
            )
            session.commit()

        self.save_memory_chunk(
            session_id=session_id,
            source_type="message",
            source_id=message_id,
            content=content,
            metadata={
                "sender_id": sender_id or "",
                "role": "user",
                "episode_id": episode_id,
                "mentioned_users": mentioned_users or [],
            },
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
        preserve_unmatched_previous: bool = True,
    ) -> None:
        self.ensure_session(session_id)
        previous_tasks = self.get_current_tasks(session_id)
        merged_tasks = self._merge_current_tasks(
            previous_tasks,
            analysis.tasks,
            preserve_unmatched_previous=preserve_unmatched_previous,
        )
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
            for task in merged_tasks:
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
            current_tasks=merged_tasks,
            reason=analysis.summary,
            preserve_unmatched_previous=preserve_unmatched_previous,
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

        alias_map = self._build_alias_map(
            session_id,
            [message.sender_id for message in messages if message.sender_id],
        )
        lines = ["[近期群聊讨论]"]
        for message in messages:
            lines.append(self._format_message_line(message, alias_map=alias_map))
        return "\n".join(lines)

    def get_discussion_cutoff_at(
        self,
        session_id: str,
        *,
        episode_id: int | None = None,
        exclude_message_id: str | None = None,
    ) -> datetime | None:
        target_episode_id = episode_id
        if target_episode_id is None:
            active_episode = self.get_active_episode(session_id)
            target_episode_id = active_episode.id if active_episode else None

        if target_episode_id is None:
            return None

        with SessionLocal() as session:
            statement = (
                select(Message.created_at)
                .where(
                    Message.session_id == session_id,
                    Message.role == "user",
                    Message.episode_id == target_episode_id,
                )
                .order_by(desc(Message.created_at), desc(Message.id))
                .limit(1)
            )
            if exclude_message_id:
                statement = statement.where(Message.message_id != exclude_message_id)
            return session.execute(statement).scalar_one_or_none()

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

        if not tasks and not memories and not task_changes and not pending_block and not retrieved_chunks:
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
        preserve_unmatched_previous: bool,
    ) -> None:
        matched_pairs, unmatched_previous, unmatched_current = self._match_task_pairs(previous_tasks, current_tasks)
        rows: list[TaskChangeLog] = []

        for before, after in matched_pairs:
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

        for after in unmatched_current:
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

        if preserve_unmatched_previous:
            for before in unmatched_previous:
                logger.debug(
                    "Retaining previous task without changes: session_id=%s title=%s owner=%s",
                    session_id,
                    before.title,
                    before.owner,
                )
        else:
            for before in unmatched_previous:
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
                        details_json=json.dumps({"before": self._task_snapshot(before), "after": None}, ensure_ascii=False),
                    )
                )

        if not rows:
            return

        with SessionLocal() as session:
            for row in rows:
                session.add(row)
            session.commit()

    def _merge_current_tasks(
        self,
        previous_tasks: list[Task],
        current_tasks: list[TaskItem],
        *,
        preserve_unmatched_previous: bool = True,
    ) -> list[TaskItem]:
        matched_pairs, unmatched_previous, unmatched_current = self._match_task_pairs(previous_tasks, current_tasks)
        merged: list[TaskItem] = [after for _, after in matched_pairs]
        merged.extend(unmatched_current)
        if preserve_unmatched_previous:
            merged.extend(self._task_item_from_row(task) for task in unmatched_previous)
        return merged

    def _match_task_pairs(
        self,
        previous_tasks: list[Task],
        current_tasks: list[TaskItem],
    ) -> tuple[list[tuple[Task, TaskItem]], list[Task], list[TaskItem]]:
        matched_pairs: list[tuple[Task, TaskItem]] = []
        used_previous_ids: set[int] = set()
        unmatched_current: list[TaskItem] = []

        for current in current_tasks:
            previous = self._find_matching_previous_task(previous_tasks, current, used_previous_ids)
            if previous is None:
                unmatched_current.append(current)
                continue
            used_previous_ids.add(previous.id)
            matched_pairs.append((previous, current))

        unmatched_previous = [task for task in previous_tasks if task.id not in used_previous_ids]
        return matched_pairs, unmatched_previous, unmatched_current

    def _find_matching_previous_task(
        self,
        previous_tasks: list[Task],
        current_task: TaskItem,
        used_previous_ids: set[int],
    ) -> Task | None:
        current_title = self._task_key(current_task.title)
        current_owner = self._owner_key(current_task.owner)

        exact_matches = [
            task
            for task in previous_tasks
            if task.id not in used_previous_ids
            and self._task_key(task.title) == current_title
            and self._owner_key(task.owner) == current_owner
        ]
        if exact_matches:
            return exact_matches[-1]

        title_matches = [
            task
            for task in previous_tasks
            if task.id not in used_previous_ids and self._task_key(task.title) == current_title
        ]
        if len(title_matches) == 1:
            return title_matches[0]

        return None

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

    def _owner_key(self, owner: str) -> str:
        value = " ".join((owner or "").lower().split())
        return "" if value == "tbd" else value

    def _task_item_from_row(self, task: Task) -> TaskItem:
        return TaskItem(
            title=task.title,
            owner=task.owner,
            priority=task.priority,
            due_date=task.due_date,
            status=task.status,
            notes=task.notes or "",
        )

    def _task_snapshot(self, task: Task) -> dict:
        return {
            "title": task.title,
            "owner": task.owner,
            "priority": task.priority,
            "due_date": task.due_date,
            "status": task.status,
            "notes": task.notes or "",
        }

    def _format_message_line(self, message: Message, *, alias_map: dict[str, str] | None = None) -> str:
        speaker_id = message.sender_id or ("assistant" if message.role == "assistant" else "member")
        speaker = alias_map.get(speaker_id, speaker_id) if alias_map else speaker_id
        mentioned_users = self._extract_message_mentions(message)
        if mentioned_users:
            return f"- 发言人: {speaker} | 提及: {', '.join(mentioned_users)} | 内容: {message.content}"
        return f"- 发言人: {speaker} | 内容: {message.content}"

    def _extract_message_mentions(self, message: Message) -> list[str]:
        if not message.mentions_json:
            return []

        try:
            payload = json.loads(message.mentions_json)
        except json.JSONDecodeError:
            return []

        if not isinstance(payload, list):
            return []

        results: list[str] = []
        for item in payload:
            if not isinstance(item, dict) or item.get("is_bot"):
                continue
            name = str(item.get("name") or "").strip()
            user_id = str(item.get("user_id") or item.get("open_id") or "").strip()
            if name:
                results.append(name)
            elif user_id:
                results.append(user_id)
        return results

    def _upsert_user_aliases(self, session_id: str, mentioned_users: list[dict]) -> None:
        valid_users = []
        for item in mentioned_users:
            if not isinstance(item, dict) or item.get("is_bot"):
                continue
            display_name = str(item.get("display_name") or item.get("name") or "").strip()
            if not display_name:
                continue
            valid_users.append(
                {
                    "user_id": str(item.get("user_id") or "").strip() or None,
                    "open_id": str(item.get("open_id") or "").strip() or None,
                    "union_id": str(item.get("union_id") or "").strip() or None,
                    "display_name": display_name,
                }
            )

        if not valid_users:
            return

        with SessionLocal() as session:
            for item in valid_users:
                match_conditions = [
                    condition
                    for condition in (
                        UserAlias.user_id == item["user_id"] if item["user_id"] else None,
                        UserAlias.open_id == item["open_id"] if item["open_id"] else None,
                        UserAlias.union_id == item["union_id"] if item["union_id"] else None,
                    )
                    if condition is not None
                ]
                existing = (
                    session.execute(
                        select(UserAlias).where(
                            UserAlias.session_id == session_id,
                            or_(*match_conditions),
                        )
                    ).scalar_one_or_none()
                    if match_conditions
                    else None
                )
                if existing is not None:
                    existing.display_name = item["display_name"]
                    continue
                session.add(
                    UserAlias(
                        session_id=session_id,
                        user_id=item["user_id"],
                        open_id=item["open_id"],
                        union_id=item["union_id"],
                        display_name=item["display_name"],
                    )
                )
            session.commit()

    def _build_alias_map(self, session_id: str, identifiers: list[str]) -> dict[str, str]:
        unique_ids = [value for value in {identifier.strip() for identifier in identifiers if identifier and identifier.strip()}]
        if not unique_ids:
            return {}

        with SessionLocal() as session:
            rows = (
                session.execute(
                    select(UserAlias).where(
                        UserAlias.session_id == session_id,
                        or_(
                            UserAlias.user_id.in_(unique_ids),
                            UserAlias.open_id.in_(unique_ids),
                            UserAlias.union_id.in_(unique_ids),
                        ),
                    )
                )
                .scalars()
                .all()
            )

        alias_map: dict[str, str] = {}
        for row in rows:
            for key in (row.user_id, row.open_id, row.union_id):
                if key:
                    alias_map[key] = row.display_name
        return alias_map
