import os
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import AppSetting, Episode, Memory, MemoryChunk, Message, Session, Task, TaskChangeLog, UserAlias
from app.schemas.analyze import AgentTrace, AnalyzeResponse
from app.schemas.task import TaskItem
from app.services.memory_service import MemoryService


class TeamMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.tempdir.name, "team_memory.db")
        self.engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
        self.test_session_local = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        for table in (
            AppSetting.__table__,
            Session.__table__,
            Episode.__table__,
            Message.__table__,
            Task.__table__,
            TaskChangeLog.__table__,
            Memory.__table__,
            MemoryChunk.__table__,
            UserAlias.__table__,
        ):
            table.create(bind=self.engine)

        memory_patcher = patch("app.services.memory_service.SessionLocal", self.test_session_local)
        state_patcher = patch("app.services.app_state.SessionLocal", self.test_session_local)
        self.addCleanup(memory_patcher.stop)
        self.addCleanup(state_patcher.stop)
        memory_patcher.start()
        state_patcher.start()
        self.service = MemoryService()

    def tearDown(self) -> None:
        self.engine.dispose()
        self.tempdir.cleanup()

    def test_p2p_team_context_can_read_registered_group_discussion(self) -> None:
        self.service.register_team_group_session("tenant_a", "oc_group_a")
        episode = self.service.ensure_active_episode("oc_group_a")
        self.service.save_user_message(
            session_id="oc_group_a",
            message_id="msg_group_1",
            sender_id="u1",
            content="张三负责后端接口，周五前给出联调版本。",
            episode_id=episode.id,
            embed=False,
        )
        self.service.save_round(
            session_id="oc_group_a",
            analysis=AnalyzeResponse(
                session_id="oc_group_a",
                summary="群聊明确了后端接口联调安排。",
                tasks=[
                    TaskItem(
                        title="后端接口联调",
                        owner="张三",
                        priority="medium",
                        due_date="周五",
                        status="draft",
                        notes="群聊中明确",
                    )
                ],
                risks=[],
                next_actions=["张三周五前提交联调版本"],
                agent_traces=[AgentTrace(agent="planner", summary="ok")],
            ),
            episode_id=episode.id,
            embed=False,
        )

        context = self.service.build_team_workspace_context(
            "tenant_a",
            current_session_id="ou_p2p_a",
            query_text="当前后端是谁负责",
        )
        tasks = self.service.get_team_current_tasks("tenant_a", current_session_id="ou_p2p_a")

        self.assertIn("[团队群聊上下文]", context)
        self.assertIn("张三负责后端接口", context)
        self.assertIn("后端接口联调", context)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].owner, "张三")


if __name__ == "__main__":
    unittest.main()
