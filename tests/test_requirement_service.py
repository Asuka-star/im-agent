import os
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Artifact, ConfirmationRequest, Message, Requirement, RequirementSource, TaskRun, TaskRunStep, UserAlias
from app.schemas.feishu_event import FeishuMessageContext
from app.services.requirement_resolver import RequirementResolver
from app.services.requirement_service import RequirementService
from app.services.task_run_service import TaskRunService
from app.services.workflow.entrypoint import WorkflowEntrypoint


class _StubSessionDocumentService:
    def list_documents(self, session_id: str) -> list[dict]:
        return [
            {
                "session_id": session_id,
                "document_id": "doc_current",
                "title": "校园活动报名系统需求文档",
                "version": 2,
                "sync_mode": "updated",
                "is_current": True,
            }
        ]


class _MappedSessionDocumentService:
    def __init__(self, documents_by_session: dict[str, list[dict]]) -> None:
        self.documents_by_session = documents_by_session

    def list_documents(self, session_id: str) -> list[dict]:
        return list(self.documents_by_session.get(session_id, []))


class _StubSessionDisplayService:
    def resolve_session_label(
        self,
        *,
        session_id: str,
        source_type: str | None = None,
        source_ref: str | None = None,
        created_by: str | None = None,
    ) -> str | None:
        return f"session {session_id}"


class _StubRequirementLLMService:
    def __init__(self, result: dict) -> None:
        self.result = result
        self.calls: list[dict] = []

    def is_configured(self) -> bool:
        return True

    def resolve_requirement_workspace(self, context: dict) -> dict:
        self.calls.append(context)
        return self.result


class RequirementServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.tempdir.name, "requirements.db")
        self.engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
        self.test_session_local = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        Requirement.__table__.create(bind=self.engine)
        RequirementSource.__table__.create(bind=self.engine)
        Message.__table__.create(bind=self.engine)
        UserAlias.__table__.create(bind=self.engine)
        TaskRun.__table__.create(bind=self.engine)
        TaskRunStep.__table__.create(bind=self.engine)
        Artifact.__table__.create(bind=self.engine)
        ConfirmationRequest.__table__.create(bind=self.engine)

        service_patcher = patch("app.services.requirement_service.SessionLocal", self.test_session_local)
        task_run_patcher = patch("app.services.task_run_service.SessionLocal", self.test_session_local)
        self.addCleanup(service_patcher.stop)
        self.addCleanup(task_run_patcher.stop)
        service_patcher.start()
        task_run_patcher.start()

        session_display_service = _StubSessionDisplayService()
        self.task_run_service = TaskRunService(
            session_display_service=session_display_service,
            session_document_service=_StubSessionDocumentService(),
        )
        self.service = RequirementService(
            task_run_service=self.task_run_service,
            session_document_service=_StubSessionDocumentService(),
            session_display_service=session_display_service,
        )

    def tearDown(self) -> None:
        self.engine.dispose()
        self.tempdir.cleanup()

    def test_create_requirement_bind_task_run_and_aggregate_detail(self) -> None:
        requirement = self.service.create_requirement(
            title="校园活动报名系统",
            primary_session_id="oc_req",
            summary="面向学生和社团负责人的活动报名与审核系统。",
            source_message_id="msg_1",
        )
        with patch("app.services.task_run_service.realtime_hub.emit_room"):
            task_run = self.task_run_service.create_task_run(
                session_id="oc_req",
                title="生成需求文档",
                source_type="group",
                requirement_id=requirement.requirement_id,
            )
        self.service.bind_task_run(
            task_run_id=task_run.task_run_id,
            requirement_id=requirement.requirement_id,
            session_id="oc_req",
            message_id="msg_2",
            source_type="im",
        )
        self.task_run_service.create_artifact(
            task_run.task_run_id,
            artifact_type="slides_package",
            title="报名系统汇报 PPT",
            provider="local",
            preview={"slides": []},
        )
        self.task_run_service.create_artifact(
            task_run.task_run_id,
            artifact_type="document",
            title="requirement document",
            provider="feishu_doc",
            preview={"sync": {"document_id": "doc_current"}},
        )
        self.service.update_current_artifacts_from_task_run(task_run.task_run_id)

        detail = self.service.get_requirement(requirement.requirement_id)

        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail.requirement_id, requirement.requirement_id)
        self.assertEqual(len(detail.task_runs), 1)
        self.assertEqual(detail.task_runs[0].requirement_id, requirement.requirement_id)
        self.assertEqual(detail.current_document.document_id, "doc_current")
        self.assertIsNotNone(detail.current_slides)
        assert detail.current_slides is not None
        self.assertEqual(detail.current_slides.artifact_type, "slides_package")
        self.assertEqual(detail.timeline[0].item_id, task_run.task_run_id)
        self.assertTrue(any(source.session_id == "oc_req" and source.message_id == "msg_2" for source in detail.sources))

    def test_bind_task_run_records_source_even_without_explicit_message(self) -> None:
        requirement = self.service.create_requirement(
            title="跨会话演示稿需求",
            primary_session_id="oc_primary",
            summary="需要把另一条会话里的任务归入这个需求。",
        )
        with patch("app.services.task_run_service.realtime_hub.emit_room"):
            task_run = self.task_run_service.create_task_run(
                session_id="oc_other",
                title="补充演示稿素材",
                source_type="p2p",
            )
        self.task_run_service.create_artifact(
            task_run.task_run_id,
            artifact_type="slides_package",
            title="补充素材演示稿",
            provider="local",
            preview={"slides": []},
        )

        self.service.bind_task_run(
            task_run_id=task_run.task_run_id,
            requirement_id=requirement.requirement_id,
            source_type="manual_reassign",
        )
        self.service.bind_task_run(
            task_run_id=task_run.task_run_id,
            requirement_id=requirement.requirement_id,
            source_type="manual_reassign",
        )

        detail = self.service.get_requirement(requirement.requirement_id)

        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail.current_slides_artifact_id, detail.current_slides.artifact_id)
        matching_sources = [
            source
            for source in detail.sources
            if source.session_id == "oc_other" and source.source_type == "manual_reassign"
        ]
        self.assertEqual(len(matching_sources), 1)

    def test_current_document_can_be_resolved_from_bound_p2p_session(self) -> None:
        document_service = _MappedSessionDocumentService(
            {
                "oc_primary": [],
                "ou_personal": [
                    {
                        "session_id": "ou_personal",
                        "document_id": "doc_p2p",
                        "title": "个人单聊生成的需求方案",
                        "version": 1,
                        "sync_mode": "created",
                    }
                ],
            }
        )
        self.service.session_document_service = document_service
        requirement = self.service.create_requirement(
            title="校园活动报名系统",
            primary_session_id="oc_primary",
            summary="群聊里创建，个人单聊里继续生成文档。",
        )
        with patch("app.services.task_run_service.realtime_hub.emit_room"):
            task_run = self.task_run_service.create_task_run(
                session_id="ou_personal",
                title="生成正式需求方案文档",
                source_type="p2p",
            )
        self.service.bind_task_run(
            task_run_id=task_run.task_run_id,
            requirement_id=requirement.requirement_id,
            source_type="im",
        )
        self.task_run_service.create_artifact(
            task_run.task_run_id,
            artifact_type="document",
            title="个人单聊生成的需求方案",
            provider="feishu_doc",
            preview={"sync": {"document_id": "doc_p2p"}},
        )
        self.service.update_current_artifacts_from_task_run(task_run.task_run_id)

        detail = self.service.get_requirement(requirement.requirement_id)

        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail.current_document_id, "doc_p2p")
        self.assertIsNotNone(detail.current_document)
        assert detail.current_document is not None
        self.assertEqual(detail.current_document.session_id, "ou_personal")
        self.assertEqual(detail.current_document.document_id, "doc_p2p")

    def test_requirement_ids_for_document_includes_pointers_and_artifacts(self) -> None:
        pointer_requirement = self.service.create_requirement(
            title="Pointer Requirement",
            primary_session_id="oc_pointer",
            summary="Uses a current document pointer.",
        )
        artifact_requirement = self.service.create_requirement(
            title="Artifact Requirement",
            primary_session_id="oc_artifact",
            summary="Has a document artifact.",
        )
        with self.test_session_local() as session:
            row = session.query(Requirement).filter_by(requirement_id=pointer_requirement.requirement_id).one_or_none()
            assert row is not None
            row.current_document_id = "doc_shared"
            session.commit()
        with patch("app.services.task_run_service.realtime_hub.emit_room"):
            task_run = self.task_run_service.create_task_run(
                session_id="oc_artifact",
                title="Generate requirement doc",
                source_type="group",
                requirement_id=artifact_requirement.requirement_id,
            )
        self.task_run_service.create_artifact(
            task_run.task_run_id,
            artifact_type="document",
            title="Artifact doc",
            provider="feishu_doc",
            preview={"sync": {"document_id": "doc_shared"}},
        )

        ids = self.service.requirement_ids_for_document("doc_shared")

        self.assertEqual(
            ids,
            [pointer_requirement.requirement_id, artifact_requirement.requirement_id],
        )

    def test_passive_requirement_is_visible_without_task_runs(self) -> None:
        with self.test_session_local() as session:
            session.add(UserAlias(session_id="oc_passive_visible", user_id="user_1", display_name="李同学"))
            session.add(
                Message(
                    message_id="msg_passive_1",
                    session_id="oc_passive_visible",
                    role="user",
                    sender_id="user_1",
                    content="我们先讨论第一个需求：校园活动报名与审核系统。",
                )
            )
            session.add(
                Message(
                    message_id="msg_passive_2",
                    session_id="oc_passive_visible",
                    role="user",
                    sender_id="user_1",
                    content="目标用户包括学生、社团负责人和学院老师。",
                )
            )
            session.commit()
        requirement = self.service.create_requirement(
            title="校园活动报名与审核系统",
            primary_session_id="oc_passive_visible",
            summary="从群聊讨论中识别出的需求。",
            source_message_id="msg_passive_1",
            source_type="im_passive_group",
        )
        self.service.record_source(
            requirement_id=requirement.requirement_id,
            session_id="oc_passive_visible",
            message_id="msg_passive_2",
            source_type="im_passive_group",
        )

        listed = self.service.list_requirements(query="session oc_passive_visible")
        detail = self.service.get_requirement(requirement.requirement_id)

        self.assertEqual([item.requirement_id for item in listed], [requirement.requirement_id])
        self.assertEqual(listed[0].primary_session_label, "session oc_passive_visible")
        self.assertEqual(listed[0].task_run_count, 0)
        self.assertEqual(listed[0].source_count, 2)
        self.assertEqual(listed[0].latest_source_type, "im_passive_group")
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail.task_runs, [])
        self.assertIsNone(detail.current_document)
        self.assertEqual(len(detail.sources), 2)
        self.assertEqual(detail.sources[0].session_type, "group")
        self.assertEqual(detail.sources[0].session_label, "session oc_passive_visible")
        self.assertEqual(detail.sources[0].sender_label, "李同学")
        self.assertEqual(detail.sources[0].message_text, "我们先讨论第一个需求：校园活动报名与审核系统。")

    def test_requirement_list_ignores_legacy_status_column(self) -> None:
        archived = self.service.create_requirement(
            title="历史需求也应可见",
            primary_session_id="oc_requirement_status",
            summary="需求工作区不再按状态过滤。",
        )
        current = self.service.create_requirement(
            title="当前讨论需求",
            primary_session_id="oc_requirement_status",
            summary="同一会话里的另一个需求。",
        )
        with self.test_session_local() as session:
            row = session.query(Requirement).filter_by(requirement_id=archived.requirement_id).one()
            row.status = "archived"
            session.commit()

        listed = self.service.list_requirements(session_id="oc_requirement_status")

        self.assertEqual(
            {item.requirement_id for item in listed},
            {archived.requirement_id, current.requirement_id},
        )

    def test_bind_task_run_touches_requirement_even_without_artifact_changes(self) -> None:
        requirement = self.service.create_requirement(
            title="仅绑定任务的需求",
            primary_session_id="oc_touch",
            summary="没有产物变化时也要让需求列表更新时间前移。",
        )
        with self.test_session_local() as session:
            row = session.query(Requirement).filter_by(requirement_id=requirement.requirement_id).one()
            row.updated_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
            session.commit()
        with patch("app.services.task_run_service.realtime_hub.emit_room"):
            task_run = self.task_run_service.create_task_run(
                session_id="oc_touch",
                title="只记录讨论结论",
                source_type="group",
            )

        self.service.bind_task_run(
            task_run_id=task_run.task_run_id,
            requirement_id=requirement.requirement_id,
            source_type="manual_reassign",
        )

        detail = self.service.get_requirement(requirement.requirement_id)

        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertIsNotNone(detail.updated_at)
        assert detail.updated_at is not None
        self.assertGreater(detail.updated_at.replace(tzinfo=timezone.utc), datetime(2024, 1, 1, tzinfo=timezone.utc))

    def test_resolver_creates_requirement_when_no_active_lifecycle_exists(self) -> None:
        resolver = RequirementResolver(self.service)

        result = resolver.resolve(
            type(
                "Message",
                (),
                {
                    "session_id": "oc_new",
                    "text": "帮我整理校园活动报名系统需求文档",
                    "sender_id": "user_1",
                    "message_id": "msg_new",
                },
            )()
        )

        self.assertEqual(result.action, "create")
        self.assertIsNotNone(result.requirement_id)
        requirements = self.service.list_requirements(session_id="oc_new")
        self.assertEqual(len(requirements), 1)
        self.assertEqual(requirements[0].requirement_id, result.requirement_id)

    def test_resolver_uses_llm_as_primary_requirement_judge_for_binding(self) -> None:
        self.service.create_requirement(
            title="校园活动报名系统",
            primary_session_id="oc_llm",
            summary="面向学生的活动报名主线。",
        )
        target = self.service.create_requirement(
            title="社团负责人审核后台",
            primary_session_id="oc_llm",
            summary="面向社团负责人的审核、驳回和名单确认。",
        )
        llm = _StubRequirementLLMService({
            "action": "bind",
            "requirement_id": target.requirement_id,
            "confidence": 0.88,
            "matched_by": "semantic",
            "reason": "用户提到评审版和负责人审核语境，语义上属于审核后台。",
            "candidates": [
                {
                    "requirement_id": target.requirement_id,
                    "score": 0.88,
                    "reason": "负责人审核语义匹配。",
                }
            ],
        })
        resolver = RequirementResolver(self.service, llm_service=llm)

        result = resolver.resolve(
            type(
                "Message",
                (),
                {
                    "session_id": "oc_llm",
                    "text": "按昨天评审说的，继续改那个面向负责人的版本",
                    "sender_id": "user_1",
                    "message_id": "msg_llm_bind",
                },
            )()
        )

        self.assertEqual(result.action, "bind")
        self.assertEqual(result.requirement_id, target.requirement_id)
        self.assertEqual(result.matched_by, "semantic")
        self.assertEqual(len(llm.calls), 1)
        self.assertIn(
            target.requirement_id,
            [item["requirement_id"] for item in llm.calls[0]["active_requirements"]],
        )

    def test_resolver_uses_llm_as_primary_requirement_judge_for_creation(self) -> None:
        self.service.create_requirement(
            title="校园活动报名系统",
            primary_session_id="oc_old",
            summary="已有需求。",
        )
        llm = _StubRequirementLLMService({
            "action": "create",
            "requirement_id": None,
            "confidence": 0.86,
            "matched_by": "new_request",
            "reason": "用户明确提出不同方向的新方案。",
            "new_requirement": {
                "title": "社团积分兑换系统",
                "summary": "围绕社团积分兑换和奖品核销的新需求。",
            },
            "candidates": [],
        })
        resolver = RequirementResolver(self.service, llm_service=llm)

        result = resolver.resolve(
            type(
                "Message",
                (),
                {
                    "session_id": "oc_new_llm",
                    "text": "我们换个方向，做一个社团积分兑换系统方案",
                    "sender_id": "user_2",
                    "message_id": "msg_llm_create",
                },
            )()
        )

        self.assertEqual(result.action, "create")
        self.assertIsNotNone(result.requirement_id)
        created = self.service.get_requirement(result.requirement_id)
        self.assertIsNotNone(created)
        assert created is not None
        self.assertEqual(created.title, "社团积分兑换系统")
        self.assertEqual(created.summary, "围绕社团积分兑换和奖品核销的新需求。")
        self.assertEqual(len(llm.calls), 1)

    def test_resolver_clarifies_when_llm_bind_confidence_is_low(self) -> None:
        target = self.service.create_requirement(
            title="校园活动报名系统",
            primary_session_id="oc_low",
            summary="活动报名。",
        )
        llm = _StubRequirementLLMService({
            "action": "bind",
            "requirement_id": target.requirement_id,
            "confidence": 0.52,
            "matched_by": "semantic",
            "reason": "可能相关但不确定。",
            "candidates": [
                {"requirement_id": target.requirement_id, "score": 0.52, "reason": "弱相关。"}
            ],
        })
        resolver = RequirementResolver(self.service, llm_service=llm)

        result = resolver.resolve(
            type(
                "Message",
                (),
                {
                    "session_id": "oc_low",
                    "text": "继续改那个方案",
                    "sender_id": "user_3",
                    "message_id": "msg_llm_low",
                },
            )()
        )

        self.assertEqual(result.action, "clarify")
        self.assertEqual(result.candidates[0].requirement_id, target.requirement_id)

    def test_resolver_does_not_rule_bind_after_low_confidence_llm_bind(self) -> None:
        target = self.service.create_requirement(
            title="校园活动报名系统",
            primary_session_id="oc_low_single",
            summary="活动报名和审核方案。",
        )
        llm = _StubRequirementLLMService({
            "action": "bind",
            "requirement_id": target.requirement_id,
            "confidence": 0.41,
            "matched_by": "semantic",
            "reason": "语义可能相关，但不确定是否仍是同一个需求。",
            "candidates": [],
        })
        resolver = RequirementResolver(self.service, llm_service=llm)

        result = resolver.resolve(
            type(
                "Message",
                (),
                {
                    "session_id": "oc_low_single",
                    "text": "继续修改这个方案",
                    "sender_id": "user_4",
                    "message_id": "msg_llm_low_single",
                },
            )()
        )

        self.assertEqual(result.action, "clarify")
        self.assertNotEqual(result.action, "bind")
        self.assertEqual(result.candidates[0].requirement_id, target.requirement_id)

    def test_resolver_supplements_llm_clarification_with_recent_requirements(self) -> None:
        primary = self.service.create_requirement(
            title="校园活动报名系统",
            primary_session_id="oc_recent",
            summary="活动报名和审核。",
        )
        secondary = self.service.create_requirement(
            title="社团积分兑换系统",
            primary_session_id="oc_recent",
            summary="积分兑换和奖品核销。",
        )
        other_session = self.service.create_requirement(
            title="跨会话演示稿需求",
            primary_session_id="oc_other_recent",
            summary="另一个会话最近推进的演示稿。",
        )
        llm = _StubRequirementLLMService({
            "action": "clarify",
            "requirement_id": None,
            "confidence": 0.46,
            "matched_by": "ambiguous",
            "reason": "用户说继续改方案，但无法唯一定位。",
            "candidates": [
                {"requirement_id": primary.requirement_id, "score": 0.46, "reason": "弱相关。"}
            ],
        })
        resolver = RequirementResolver(self.service, llm_service=llm)

        result = resolver.resolve(
            type(
                "Message",
                (),
                {
                    "session_id": "oc_recent",
                    "text": "继续改一下这个方案",
                    "sender_id": "user_5",
                    "message_id": "msg_recent",
                },
            )()
        )

        candidate_ids = [item.requirement_id for item in result.candidates]
        self.assertEqual(result.action, "clarify")
        self.assertIn(primary.requirement_id, candidate_ids)
        self.assertIn(secondary.requirement_id, candidate_ids)
        self.assertIn(other_session.requirement_id, candidate_ids)

    def test_passive_resolver_does_not_create_vague_new_requirement(self) -> None:
        llm = _StubRequirementLLMService({
            "action": "create",
            "requirement_id": None,
            "confidence": 0.55,
            "matched_by": "new_request",
            "reason": "用户提到新需求，但没有说明是否是既有需求下的子需求。",
            "new_requirement": {"title": "新的需求", "summary": "信息不足。"},
            "candidates": [],
        })
        resolver = RequirementResolver(self.service, llm_service=llm)

        result = resolver.resolve(
            type(
                "Message",
                (),
                {
                    "session_id": "oc_passive_vague",
                    "text": "我们现在有一个新的需求",
                    "sender_id": "user_6",
                    "message_id": "msg_passive_vague",
                    "source_type": "im_passive_group",
                },
            )()
        )

        self.assertEqual(result.action, "clarify")
        self.assertEqual(self.service.list_requirements(session_id="oc_passive_vague"), [])
        self.assertTrue(llm.calls[0]["signals"]["passive_group_observation"])

    def test_passive_resolver_creates_clear_new_discussion_requirement(self) -> None:
        llm = _StubRequirementLLMService({
            "action": "create",
            "requirement_id": None,
            "confidence": 0.86,
            "matched_by": "new_request",
            "reason": "群聊明确开启校园活动报名系统需求讨论，且没有已有候选匹配。",
            "new_requirement": {
                "title": "校园活动报名系统",
                "summary": "围绕学生报名、负责人审核和名单导出的新需求讨论。",
            },
            "candidates": [],
        })
        resolver = RequirementResolver(self.service, llm_service=llm)

        result = resolver.resolve(
            type(
                "Message",
                (),
                {
                    "session_id": "oc_passive_create",
                    "text": "我们现在来讨论校园活动报名系统需求",
                    "sender_id": "user_6",
                    "message_id": "msg_passive_create",
                    "source_type": "im_passive_group",
                },
            )()
        )

        requirements = self.service.list_requirements(session_id="oc_passive_create")
        self.assertEqual(result.action, "create")
        self.assertEqual(len(requirements), 1)
        self.assertEqual(requirements[0].title, "校园活动报名系统")
        detail = self.service.get_requirement(result.requirement_id)
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertTrue(
            any(
                source.message_id == "msg_passive_create"
                and source.source_type == "im_passive_group"
                for source in detail.sources
            )
        )

    def test_passive_source_marks_requirement_related_to_later_session_context(self) -> None:
        requirement = self.service.create_requirement(
            title="跨会话报名系统需求",
            primary_session_id="oc_origin",
            summary="原始群聊里创建的报名系统需求。",
        )
        self.service.record_source(
            requirement_id=requirement.requirement_id,
            session_id="oc_passive_related",
            message_id="msg_topic",
            source_type="im_passive_group",
        )
        llm = _StubRequirementLLMService({
            "action": "skip",
            "confidence": 0.9,
            "matched_by": "not_requirement",
            "reason": "只检查上下文。",
            "candidates": [],
        })
        resolver = RequirementResolver(self.service, llm_service=llm)

        resolver.resolve(
            type(
                "Message",
                (),
                {
                    "session_id": "oc_passive_related",
                    "text": "继续讨论一下刚才的方案",
                    "sender_id": "user_7",
                    "message_id": "msg_later",
                },
            )()
        )

        active = llm.calls[0]["active_requirements"]
        matched = next(item for item in active if item["requirement_id"] == requirement.requirement_id)
        self.assertTrue(matched["related_to_current_session"])
        self.assertEqual(llm.calls[0]["signals"]["session_active_requirement_count"], 1)

    def test_group_message_without_mention_observes_requirement_without_task_run(self) -> None:
        requirement = self.service.create_requirement(
            title="校园活动报名系统",
            primary_session_id="oc_passive_entry",
            summary="讨论学生报名、负责人审核和名单导出。",
        )
        llm = _StubRequirementLLMService({
            "action": "bind",
            "requirement_id": requirement.requirement_id,
            "confidence": 0.88,
            "matched_by": "semantic",
            "reason": "群聊明确说现在讨论校园活动报名系统需求。",
            "candidates": [
                {
                    "requirement_id": requirement.requirement_id,
                    "score": 0.88,
                    "reason": "主题明确。",
                }
            ],
        })
        workflow = SimpleNamespace(
            memory_service=Mock(),
            task_run_service=Mock(),
            requirement_service=self.service,
            requirement_resolver=RequirementResolver(self.service, llm_service=llm),
            _team_id_for_message=Mock(return_value="default"),
            _ensure_sender_alias=Mock(return_value=None),
        )
        workflow.memory_service.ensure_active_episode.return_value = SimpleNamespace(id=10)
        workflow.memory_service.get_message_lifecycle_info.return_value = {}
        workflow.memory_service.get_user_message_content.return_value = None
        workflow.task_run_service.create_task_run.side_effect = AssertionError("passive group message should not create task run")
        entrypoint = WorkflowEntrypoint(workflow)

        result = entrypoint.handle_message(
            FeishuMessageContext(
                event_id="evt_passive",
                message_id="msg_passive_topic",
                chat_id="oc_passive_entry",
                chat_type="group",
                message_type="text",
                session_id="oc_passive_entry",
                sender_id="user_8",
                text="我们现在来讨论校园活动报名系统需求",
                raw_text="我们现在来讨论校园活动报名系统需求",
                is_mentioned=False,
            )
        )

        detail = self.service.get_requirement(requirement.requirement_id)
        self.assertEqual(result["mode"], "buffer")
        self.assertEqual(result["requirement_resolution"]["action"], "bind")
        workflow.task_run_service.create_task_run.assert_not_called()
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertTrue(
            any(
                source.session_id == "oc_passive_entry"
                and source.message_id == "msg_passive_topic"
                and source.source_type == "im_passive_group"
                for source in detail.sources
            )
        )

    def test_resolver_creates_requirement_for_new_session_when_other_active_exists(self) -> None:
        self.service.create_requirement(
            title="旧会话需求",
            primary_session_id="oc_existing",
            summary="别的会话已经存在的需求。",
        )
        resolver = RequirementResolver(self.service)

        result = resolver.resolve(
            type(
                "Message",
                (),
                {
                    "session_id": "oc_new_session",
                    "text": "帮我整理校园活动报名系统需求文档",
                    "sender_id": "user_2",
                    "message_id": "msg_new_session",
                },
            )()
        )

        self.assertEqual(result.action, "create")
        self.assertIsNotNone(result.requirement_id)
        requirements = self.service.list_requirements(session_id="oc_new_session")
        self.assertEqual(len(requirements), 1)
        self.assertEqual(requirements[0].requirement_id, result.requirement_id)

    def test_resolver_clarifies_when_multiple_requirements_match(self) -> None:
        self.service.create_requirement(
            title="校园活动报名系统",
            primary_session_id="oc_multi",
            summary="学生报名、负责人审核和名单生成。",
        )
        self.service.create_requirement(
            title="校园活动审核后台",
            primary_session_id="oc_multi",
            summary="学院老师审核活动信息和发布规则。",
        )
        resolver = RequirementResolver(self.service)

        result = resolver.resolve(
            type(
                "Message",
                (),
                {
                    "session_id": "oc_multi",
                    "text": "继续修改这个活动系统方案",
                    "sender_id": "user_1",
                    "message_id": "msg_multi",
                },
            )()
        )

        self.assertEqual(result.action, "clarify")
        self.assertGreaterEqual(len(result.candidates), 2)


if __name__ == "__main__":
    unittest.main()
