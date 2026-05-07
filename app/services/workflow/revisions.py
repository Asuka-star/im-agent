from __future__ import annotations

import json
import logging
import re
from types import SimpleNamespace
from typing import Any

from app.services.tools.doc_tool import DocTool
from app.services.request_router import RouteDecision
from app.schemas.feishu_event import FeishuMessageContext
from app.utils.values import coerce_positive_int

logger = logging.getLogger(__name__)


class WorkbenchRevisionWorkflow:
    """Runs workbench revision and confirmation-resume flows for FeishuWorkflowService."""

    def __init__(self, workflow: Any) -> None:
        self.workflow = workflow

    def resume_task_run_after_confirmation(
        self,
        task_run_id: str,
        *,
        confirmation_id: str,
        answer_value: str,
        answered_by: str = "user",
        override_instruction: str | None = None,
    ) -> dict | None:
        workflow = self.workflow
        detail = workflow.task_run_service.get_task_run(task_run_id)
        if detail is None:
            return None

        metadata = workflow.task_run_service.get_task_run_metadata(task_run_id)
        requirement_payload = metadata.get("requirement_confirmation")
        if isinstance(requirement_payload, dict):
            return self._resume_after_requirement_confirmation(
                task_run_id,
                detail=detail,
                metadata=metadata,
                confirmation_id=confirmation_id,
                answer_value=answer_value,
                answered_by=answered_by,
            )

        offline_payload = metadata.get("offline_document_confirmation")
        if isinstance(offline_payload, dict):
            return self._resume_after_offline_document_confirmation(
                task_run_id,
                detail=detail,
                metadata=metadata,
                confirmation_id=confirmation_id,
                answer_value=answer_value,
                answered_by=answered_by,
                override_instruction=override_instruction,
            )

        resume_payload = metadata.get("resume_after_confirmation")
        if not isinstance(resume_payload, dict):
            return None

        expected_confirmation_id = str(resume_payload.get("confirmation_id") or "").strip()
        if expected_confirmation_id and expected_confirmation_id != confirmation_id:
            return None

        instruction = str(resume_payload.get("instruction") or "").strip()
        workspace_context = str(resume_payload.get("workspace_context") or "")
        active_episode_id = resume_payload.get("active_episode_id")
        if not isinstance(active_episode_id, int):
            active_episode_id = None

        resumed_instruction = build_confirmation_resume_instruction(instruction, answer_value)
        intent = str(resume_payload.get("intent") or "status")
        target_document = None
        if intent == "doc":
            target_document = workflow._resolve_target_document_for_instruction(detail.session_id, resumed_instruction)
            if target_document is not None:
                workspace_context = workflow._join_context_blocks(
                    workspace_context,
                    DocTool.format_current_document_context(target_document),
                )
        workflow.task_run_service.upsert_step(
            task_run_id,
            step_key="user_confirmation",
            title="等待用户确认",
            step_type="confirmation",
            status="done",
            output_payload={
                "question": resume_payload.get("question"),
                "reason": resume_payload.get("reason"),
                "options": resume_payload.get("options"),
                "answer_value": answer_value,
                "answered_by": answered_by,
            },
        )
        workflow.task_run_service.update_task_run(task_run_id, stage="confirmation_replanning", status="running")

        metadata.pop("resume_after_confirmation", None)
        metadata["last_confirmation"] = {
            "confirmation_id": confirmation_id,
            "answer_value": answer_value,
            "answered_by": answered_by,
        }
        workflow.task_run_service.update_task_run(task_run_id, metadata=metadata)

        if not workflow.llm_service.is_configured():
            result = {
                "session_id": detail.session_id,
                "episode_id": active_episode_id,
                "mode": intent,
                "analysis": None,
                "reply_preview": f"已记录确认：{answer_value}。当前未配置可继续自动执行的 LLM，请稍后重新发起一次请求。",
                "reply_sent": False,
                "reply_error": None,
                "artifacts": [],
            }
            workflow.result_persistence.persist_task_run_result(task_run_id, message_text=resumed_instruction, result=result, session_id=detail.session_id)
            return result

        route_decision = RouteDecision(route=intent, source="confirmation", confidence=1.0)
        llm_result = workflow._resolve_llm_result_for_route(route_decision, workspace_context, resumed_instruction)
        workflow._apply_route_decision_to_llm_result(llm_result, route_decision)
        synthetic_message = SimpleNamespace(
            session_id=detail.session_id,
            message_id=detail.trigger_message_id or confirmation_id,
            text=resumed_instruction,
            raw_text=resumed_instruction,
            chat_id=getattr(detail, "source_ref", None) or detail.session_id,
            chat_type=getattr(detail, "source_type", None) or "group",
            is_mentioned=True,
            sender_id=answered_by,
        )
        result = workflow.execution_runner.execute_llm_request(
            synthetic_message,
            llm_result,
            workspace_context,
            active_episode_id,
            task_run_id=task_run_id,
            target_document=target_document,
        )
        workflow.result_persistence.persist_task_run_result(task_run_id, message_text=resumed_instruction, result=result, session_id=detail.session_id)
        return result

    def _resume_after_requirement_confirmation(
        self,
        task_run_id: str,
        *,
        detail,
        metadata: dict,
        confirmation_id: str,
        answer_value: str,
        answered_by: str,
    ) -> dict | None:
        workflow = self.workflow
        payload = metadata.get("requirement_confirmation")
        if not isinstance(payload, dict):
            return None
        expected_confirmation_id = str(payload.get("confirmation_id") or "").strip()
        if expected_confirmation_id and expected_confirmation_id != confirmation_id:
            return None

        candidates = payload.get("candidates") if isinstance(payload.get("candidates"), list) else []
        chosen_requirement_id = self._requirement_id_from_answer(answer_value, candidates)
        if chosen_requirement_id is None:
            created = workflow.requirement_service.create_requirement(
                title=workflow._task_run_title(str(payload.get("instruction") or detail.title), "requirement"),
                primary_session_id=detail.session_id,
                summary=str(payload.get("instruction") or "").strip() or detail.title,
                created_by=answered_by,
                source_message_id=detail.trigger_message_id,
                source_type="confirmation",
                metadata={
                    "requirement_binding": {
                        "source": "user_confirmation_create",
                        "task_run_id": task_run_id,
                    }
                },
            )
            chosen_requirement_id = created.requirement_id

        workflow.requirement_service.bind_task_run(
            task_run_id=task_run_id,
            requirement_id=chosen_requirement_id,
            session_id=detail.session_id,
            message_id=detail.trigger_message_id,
            source_type="confirmation",
        )
        workflow.task_run_service.upsert_step(
            task_run_id,
            step_key="requirement_confirmation",
            title="确认需求归属",
            step_type="confirmation",
            status="done",
            output_payload={
                "answer_value": answer_value,
                "answered_by": answered_by,
                "requirement_id": chosen_requirement_id,
            },
        )
        metadata.pop("requirement_confirmation", None)
        metadata["requirement_resolution"] = {
            "action": "bind",
            "requirement_id": chosen_requirement_id,
            "confidence": 1.0,
            "matched_by": "user_confirmation",
            "reason": f"用户确认归属：{answer_value}",
        }
        workflow.task_run_service.update_task_run(task_run_id, metadata=metadata, stage="building_context", status="running")

        instruction = str(payload.get("instruction") or detail.title).strip()
        synthetic_message = FeishuMessageContext(
            session_id=detail.session_id,
            message_id=detail.trigger_message_id or confirmation_id,
            text=instruction,
            raw_text=str(payload.get("raw_text") or instruction),
            chat_id=str(payload.get("chat_id") or getattr(detail, "source_ref", None) or detail.session_id),
            chat_type=str(payload.get("chat_type") or detail.source_type or "group"),
            message_type=str(payload.get("message_type") or "").strip() or None,
            file_key=str(payload.get("file_key") or "").strip() or None,
            file_name=str(payload.get("file_name") or "").strip() or None,
            is_mentioned=bool(payload.get("is_mentioned", True)),
            sender_id=answered_by,
        )
        if str(synthetic_message.message_type or "").strip().lower() == "file":
            result = workflow.offline_document_execution.prepare_offline_document_submission(
                synthetic_message,
                task_run_id=task_run_id,
            )
        else:
            result = workflow._handle_mentioned_request(synthetic_message, task_run_id=task_run_id)
        workflow.result_persistence.persist_task_run_result(
            task_run_id,
            message_text=instruction,
            result=result,
            session_id=detail.session_id,
        )
        return result

    def _resume_after_offline_document_confirmation(
        self,
        task_run_id: str,
        *,
        detail,
        metadata: dict,
        confirmation_id: str,
        answer_value: str,
        answered_by: str,
        override_instruction: str | None,
    ) -> dict | None:
        workflow = self.workflow
        payload = metadata.get("offline_document_confirmation")
        if not isinstance(payload, dict):
            return None
        expected_confirmation_id = str(payload.get("confirmation_id") or "").strip()
        if expected_confirmation_id and expected_confirmation_id != confirmation_id:
            return None

        package = payload.get("package") if isinstance(payload.get("package"), dict) else {}
        if not package:
            raise ValueError("Offline document package is missing from confirmation payload.")

        file_name = str(payload.get("file_name") or "").strip() or "离线文档"
        instruction = str(payload.get("instruction") or file_name).strip() or file_name
        normalized_answer = str(answer_value or "").strip()
        cleaned_override = " ".join(str(override_instruction or "").split()).strip() or None
        effective_instruction = _merge_confirmation_instruction(instruction, cleaned_override)
        available_follow_up_targets = payload.get("available_follow_up_targets")
        allowed_targets: set[str] = set()
        if isinstance(available_follow_up_targets, list):
            allowed_targets = {str(item).strip() for item in available_follow_up_targets if str(item).strip()}
        requested_follow_up_targets = self._offline_follow_up_targets_from_answer(
            normalized_answer,
            available_targets=allowed_targets,
        )
        if allowed_targets:
            requested_follow_up_targets &= allowed_targets

        workflow.task_run_service.upsert_step(
            task_run_id,
            step_key="offline_document_confirmation",
            title="确认离线文档处理方式",
            step_type="confirmation",
            status="done",
            output_payload={
                "answer_value": normalized_answer,
                "answered_by": answered_by,
                "file_name": file_name,
                "requested_follow_up_targets": sorted(requested_follow_up_targets),
                "override_instruction": cleaned_override,
            },
        )
        last_offline_record = {
            "submission_id": task_run_id,
            "task_run_id": task_run_id,
            "file_name": file_name,
            "file_extension": str(payload.get("file_extension") or "").strip() or None,
            "file_sha256": str(payload.get("file_sha256") or "").strip() or None,
            "available_follow_up_targets": sorted(allowed_targets),
            "merge_summary": payload.get("merge_summary") if isinstance(payload.get("merge_summary"), dict) else {},
            "merge_plan": payload.get("merge_plan") if isinstance(payload.get("merge_plan"), dict) else {},
            "confirmation_id": confirmation_id,
            "answer_value": normalized_answer,
            "answered_by": answered_by,
            "override_instruction": cleaned_override,
        }
        metadata.pop("offline_document_confirmation", None)
        metadata["offline_document_last_record"] = last_offline_record
        metadata["last_confirmation"] = {
            "confirmation_id": confirmation_id,
            "answer_value": normalized_answer,
            "answered_by": answered_by,
            "override_instruction": cleaned_override,
        }

        if "暂存" in normalized_answer or "参考" in normalized_answer:
            last_offline_record["status"] = "archiving"
            workflow.task_run_service.update_task_run(
                task_run_id,
                metadata=metadata,
                stage="offline_document_archiving",
                status="running",
            )
            summary_lines = ["- 已保存为离线参考材料，暂未写回飞书协作文档。"]
            artifact = workflow.offline_document_execution.parsed_package_local_artifact(
                task_run_id=task_run_id,
                session_id=detail.session_id,
                package=package,
                file_name=file_name,
                summary_lines=summary_lines,
            )
            result = {
                "session_id": detail.session_id,
                "episode_id": None,
                "mode": "doc",
                "analysis": None,
                "reply_preview": f"已收下《{file_name}》，当前先按参考材料暂存，尚未写回飞书协作文档。",
                "reply_sent": False,
                "reply_error": None,
                "artifacts": [artifact],
            }
            workflow.result_persistence.persist_task_run_result(
                task_run_id,
                message_text=effective_instruction,
                result=result,
                session_id=detail.session_id,
            )
            last_offline_record["status"] = "archived_reference"
            metadata["offline_document_last_record"] = last_offline_record
            workflow.task_run_service.update_task_run(task_run_id, metadata=metadata)
            return result

        target_document = self._offline_target_document(detail)
        last_offline_record["status"] = "applying"
        workflow.task_run_service.update_task_run(
            task_run_id,
            metadata=metadata,
            stage="offline_document_applying",
            status="running",
        )
        sync_result = workflow.doc_execution.sync_package_to_session_doc(
            package,
            session_id=detail.session_id,
            episode_id=None,
            instruction=effective_instruction,
            task_run_id=task_run_id,
            target_document=target_document,
        )
        artifact = workflow.doc_execution.build_document_artifact(
            session_id=detail.session_id,
            package=package,
            sync_result=sync_result,
            fallback_provider="offline_upload",
            task_run_id=task_run_id,
        )
        result = {
            "session_id": detail.session_id,
            "episode_id": None,
            "mode": "doc",
            "analysis": None,
            "reply_preview": workflow.doc_execution.format_doc_reply(package, sync_result.summary_lines),
            "reply_sent": False,
            "reply_error": None,
            "artifacts": [artifact],
        }
        if cleaned_override:
            result["reply_preview"] = "\n".join(
                [
                    str(result["reply_preview"] or "").strip(),
                    f"[已应用补充约束]\n- {cleaned_override}",
                ]
            ).strip()
        follow_up = self._trigger_offline_follow_up_artifacts(
            task_run_id,
            detail=detail,
            package=package,
            file_name=file_name,
            requested_targets=requested_follow_up_targets,
            override_instruction=cleaned_override,
        )
        if follow_up["lines"]:
            result["reply_preview"] = "\n\n".join(
                [
                    str(result["reply_preview"] or "").strip(),
                    "[派生产物跟随更新]",
                    "\n".join(follow_up["lines"]),
                ]
            ).strip()
        workflow.result_persistence.persist_task_run_result(
            task_run_id,
            message_text=effective_instruction,
            result=result,
            session_id=detail.session_id,
        )
        follow_up_payload = follow_up.get("payload") if isinstance(follow_up.get("payload"), dict) else {}
        if follow_up.get("status") in {"failed", "partial"}:
            last_offline_record["status"] = "merged_partial"
        else:
            last_offline_record["status"] = "merged"
        if follow_up_payload:
            last_offline_record["follow_up"] = follow_up_payload
        metadata["offline_document_last_record"] = last_offline_record
        workflow.task_run_service.update_task_run(task_run_id, metadata=metadata)
        return result

    def _trigger_offline_follow_up_artifacts(
        self,
        task_run_id: str,
        *,
        detail: Any,
        package: dict,
        file_name: str,
        requested_targets: set[str],
        override_instruction: str | None,
    ) -> dict[str, Any]:
        workflow = self.workflow
        if not requested_targets:
            payload = {
                "slides": {"status": "skipped", "reason": "not_selected"},
                "canvas": {"status": "skipped", "reason": "not_selected"},
            }
            workflow.task_run_service.upsert_step(
                task_run_id,
                step_key="offline_document_follow_up",
                title="派生产物跟随更新",
                step_type="artifact",
                status="skipped",
                output_payload=payload,
            )
            return {
                "status": "skipped",
                "lines": ["- 本次按确认结果仅更新当前文档，未联动当前 PPT / Canvas。"],
                "payload": payload,
            }
        requirement_id = str(getattr(detail, "requirement_id", "") or "").strip()
        if not requirement_id:
            workflow.task_run_service.upsert_step(
                task_run_id,
                step_key="offline_document_follow_up",
                title="派生产物跟随更新",
                step_type="artifact",
                status="skipped",
                output_payload={"reason": "requirement_missing"},
            )
            return {"status": "skipped", "lines": []}

        try:
            requirement = workflow.requirement_service.get_requirement(requirement_id)
        except Exception as exc:  # noqa: BLE001
            workflow.task_run_service.upsert_step(
                task_run_id,
                step_key="offline_document_follow_up",
                title="派生产物跟随更新",
                step_type="artifact",
                status="failed",
                error=str(exc),
            )
            return {"status": "failed", "lines": [f"- 当前产物联动准备失败：{exc}"]}

        slides_status = self._trigger_offline_slides_follow_up(
            requirement=requirement,
            package=package,
            file_name=file_name,
            selected="slides" in requested_targets,
            override_instruction=override_instruction,
        )
        canvas_status = self._trigger_offline_canvas_follow_up(
            requirement=requirement,
            package=package,
            file_name=file_name,
            selected="canvas" in requested_targets,
            override_instruction=override_instruction,
        )
        statuses = [slides_status["status"], canvas_status["status"]]
        if any(status == "failed" for status in statuses) and any(status == "ready" for status in statuses):
            step_status = "done"
        elif any(status == "failed" for status in statuses):
            step_status = "failed"
        elif any(status == "ready" for status in statuses):
            step_status = "done"
        else:
            step_status = "skipped"
        payload = {
            "slides": slides_status,
            "canvas": canvas_status,
        }
        workflow.task_run_service.upsert_step(
            task_run_id,
            step_key="offline_document_follow_up",
            title="派生产物跟随更新",
            step_type="artifact",
            status=step_status,
            output_payload=payload,
            error=None if step_status != "failed" else self._first_follow_up_error(payload),
        )
        lines = []
        for item in (slides_status, canvas_status):
            line = str(item.get("line") or "").strip()
            if line:
                lines.append(line)
        return {"status": step_status, "lines": lines, "payload": payload}

    def _trigger_offline_slides_follow_up(
        self,
        *,
        requirement: Any,
        package: dict,
        file_name: str,
        selected: bool,
        override_instruction: str | None,
    ) -> dict[str, Any]:
        workflow = self.workflow
        if not selected:
            return {"status": "skipped", "reason": "not_selected", "line": ""}
        artifact = getattr(requirement, "current_slides", None)
        artifact_id = str(getattr(artifact, "artifact_id", "") or "").strip()
        if not artifact_id:
            return {"status": "skipped", "line": "- 当前需求下没有现成 PPT，本次跳过演示稿联动更新。"}
        source_task_run_id = workflow.task_run_service.get_artifact_task_run_id(artifact_id)
        if not source_task_run_id:
            return {
                "status": "failed",
                "artifact_id": artifact_id,
                "line": "- 当前 PPT 的来源任务未找到，已跳过演示稿联动更新。",
                "error": "slides_source_task_run_missing",
            }
        try:
            record = workflow.revise_slides_from_task_run(
                source_task_run_id,
                instruction=self._offline_follow_up_slides_instruction(package, file_name, override_instruction=override_instruction),
                requested_by="offline_document",
                artifact_id=artifact_id,
            )
            return {
                "status": "ready",
                "artifact_id": artifact_id,
                "source_task_run_id": source_task_run_id,
                "task_run_id": getattr(record, "task_run_id", None) if record is not None else None,
                "line": "- 已触发当前 PPT 跟随更新。",
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("Offline document slides follow-up failed: requirement_id=%s error=%s", getattr(requirement, "requirement_id", None), exc)
            return {
                "status": "failed",
                "artifact_id": artifact_id,
                "source_task_run_id": source_task_run_id,
                "line": f"- 当前 PPT 跟随更新失败：{exc}",
                "error": str(exc),
            }

    def _trigger_offline_canvas_follow_up(
        self,
        *,
        requirement: Any,
        package: dict,
        file_name: str,
        selected: bool,
        override_instruction: str | None,
    ) -> dict[str, Any]:
        workflow = self.workflow
        if not selected:
            return {"status": "skipped", "reason": "not_selected", "line": ""}
        artifact = getattr(requirement, "current_canvas", None)
        artifact_id = str(getattr(artifact, "artifact_id", "") or "").strip()
        if not artifact_id:
            return {"status": "skipped", "line": "- 当前需求下没有现成 Canvas，本次跳过画布联动更新。"}
        source_task_run_id = workflow.task_run_service.get_artifact_task_run_id(artifact_id)
        if not source_task_run_id:
            return {
                "status": "failed",
                "artifact_id": artifact_id,
                "line": "- 当前 Canvas 的来源任务未找到，已跳过画布联动更新。",
                "error": "canvas_source_task_run_missing",
            }
        try:
            record = workflow.revise_canvas_from_task_run(
                source_task_run_id,
                instruction=self._offline_follow_up_canvas_instruction(package, file_name, override_instruction=override_instruction),
                requested_by="offline_document",
                artifact_id=artifact_id,
            )
            return {
                "status": "ready",
                "artifact_id": artifact_id,
                "source_task_run_id": source_task_run_id,
                "task_run_id": getattr(record, "task_run_id", None) if record is not None else None,
                "line": "- 已触发当前 Canvas 跟随更新。",
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("Offline document canvas follow-up failed: requirement_id=%s error=%s", getattr(requirement, "requirement_id", None), exc)
            return {
                "status": "failed",
                "artifact_id": artifact_id,
                "source_task_run_id": source_task_run_id,
                "line": f"- 当前 Canvas 跟随更新失败：{exc}",
                "error": str(exc),
            }

    @staticmethod
    def _offline_follow_up_slides_instruction(package: dict, file_name: str, *, override_instruction: str | None = None) -> str:
        summary = _offline_package_summary_text(package, max_items=3, max_length=220)
        instruction = (
            f"请根据离线文档《{file_name}》的最新内容同步更新当前演示稿，"
            f"并追加 1 页“离线更新摘要”说明这些变化：{summary}"
        )
        if override_instruction:
            instruction += f"。额外约束：{override_instruction}"
        return instruction

    @staticmethod
    def _offline_follow_up_canvas_instruction(package: dict, file_name: str, *, override_instruction: str | None = None) -> str:
        summary = _offline_package_summary_text(package, max_items=3, max_length=180)
        instruction = (
            f"请根据离线文档《{file_name}》的最新内容同步更新当前画布，"
            f"并追加 1 个节点概述这些变化：{summary}"
        )
        if override_instruction:
            instruction += f"。额外约束：{override_instruction}"
        return instruction

    @staticmethod
    def _first_follow_up_error(payload: dict[str, Any]) -> str | None:
        for key in ("slides", "canvas"):
            item = payload.get(key)
            if isinstance(item, dict) and str(item.get("error") or "").strip():
                return str(item.get("error") or "").strip()
        return None

    @staticmethod
    def _offline_follow_up_targets_from_answer(answer_value: str, *, available_targets: set[str] | None = None) -> set[str]:
        answer = str(answer_value or "").strip()
        if not answer or "暂存" in answer or "参考" in answer:
            return set()
        if "应用到当前文档" in answer:
            return set(available_targets or set())
        targets: set[str] = set()
        if "PPT" in answer:
            targets.add("slides")
        if "Canvas" in answer:
            targets.add("canvas")
        return targets

    @staticmethod
    def _requirement_id_from_answer(answer_value: str, candidates: list) -> str | None:
        answer = str(answer_value or "").strip()
        if not answer or ("新建" in answer and "需求" in answer):
            return None
        explicit_index = _requirement_answer_index(answer)
        if explicit_index is not None:
            if explicit_index < 1 or explicit_index > len(candidates):
                return None
            candidate = candidates[explicit_index - 1]
            if not isinstance(candidate, dict):
                return None
            requirement_id = str(candidate.get("requirement_id") or "").strip()
            return requirement_id or None
        for index, candidate in enumerate(candidates, start=1):
            if not isinstance(candidate, dict):
                continue
            requirement_id = str(candidate.get("requirement_id") or "").strip()
            title = str(candidate.get("title") or "").strip()
            if not requirement_id:
                continue
            if _answer_matches_candidate(answer, index=index, requirement_id=requirement_id, title=title):
                return requirement_id or None
        return None

    def revise_document_from_task_run(
        self,
        source_task_run_id: str,
        *,
        instruction: str,
        requested_by: str = "pilot_workbench",
        document_id: str | None = None,
    ):
        workflow = self.workflow
        cleaned_instruction = " ".join((instruction or "").split()).strip()
        if not cleaned_instruction:
            raise ValueError("Document revision instruction cannot be empty.")

        source_detail = workflow.task_run_service.get_task_run(source_task_run_id)
        if source_detail is None:
            return None

        session_id = source_detail.session_id
        revision_start = workflow._workbench_revision_tool().start_run(
            session_id=session_id,
            source_task_run_id=source_task_run_id,
            title=workflow._task_run_title(cleaned_instruction, "doc_revision"),
            intent="doc",
            requested_by=requested_by,
            metadata={
                "source_task_run_id": source_task_run_id,
                "revision_instruction": cleaned_instruction,
                "requested_by": requested_by,
                "document_id": (document_id or "").strip() or None,
            },
            step_title="接收文档修订指令",
            input_payload={
                "source_task_run_id": source_task_run_id,
                "instruction": cleaned_instruction,
                "requested_by": requested_by,
                "document_id": (document_id or "").strip() or None,
            },
            stage="building_context",
            requirement_id=getattr(source_detail, "requirement_id", None),
        )
        task_run = revision_start.task_run

        synthetic_message_id = revision_start.synthetic_message_id
        workflow._workbench_revision_tool().remember_user_instruction(
            session_id=session_id,
            synthetic_message_id=synthetic_message_id,
            requested_by=requested_by,
            instruction=cleaned_instruction,
            label="document",
        )
        workspace_context = workflow._workbench_revision_tool().build_workspace_context(
            session_id=session_id,
            synthetic_message_id=synthetic_message_id,
            instruction=cleaned_instruction,
            label="document",
        )
        try:
            requested_document_id = (document_id or "").strip()
            if requested_document_id:
                current_doc = workflow.session_document_service.get_document(session_id, requested_document_id)
                if current_doc is None:
                    raise ValueError(f"Document not found in session history: {requested_document_id}")
            else:
                current_doc = workflow.session_document_service.get_current_document(session_id)
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, ValueError):
                raise
            logger.warning("Failed to load current document context for revision: %s", exc)
            current_doc = None
        workspace_context = workflow._join_context_blocks(
            workspace_context,
            DocTool.format_current_document_context(current_doc),
        )
        workflow.task_run_service.upsert_step(
            task_run.task_run_id,
            step_key="workspace_context",
            title="构建文档修订上下文",
            step_type="context",
            status="done",
            output_payload={"context_length": len(workspace_context)},
        )

        llm_result: dict = {}
        if workflow.llm_service.is_configured():
            try:
                workflow.task_run_service.update_task_run(task_run.task_run_id, stage="doc_revision_planning")
                revision_instruction = DocTool.build_document_revision_instruction(
                    cleaned_instruction,
                    current_doc=current_doc,
                )
                try:
                    llm_result = workflow.llm_service.resolve_doc_request(workspace_context, revision_instruction)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Specialized doc revision request failed, falling back to workspace resolver: %s",
                        exc,
                    )
                    llm_result = workflow.llm_service.resolve_workspace_request(
                        workspace_context,
                        revision_instruction,
                    )
                llm_result["operation"] = "update"
                llm_result["object"] = "doc"
                llm_result["route"] = "doc"
                llm_result["intent"] = "doc"
                workflow.task_run_service.upsert_step(
                    task_run.task_run_id,
                    step_key="intent_resolution",
                    title="识别文档修订目标",
                    step_type="intent",
                    status="done",
                    output_payload={
                        "intent": llm_result.get("intent"),
                        "reason": llm_result.get("reason"),
                    },
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Document revision planning failed, falling back to deterministic doc sync: %s", exc)
                workflow.task_run_service.upsert_step(
                    task_run.task_run_id,
                    step_key="intent_resolution",
                    title="识别文档修订目标",
                    step_type="intent",
                    status="failed",
                    error=str(exc),
                )

        workflow.task_run_service.update_task_run(task_run.task_run_id, stage="doc_revision_processing")
        synthetic_message = SimpleNamespace(
            session_id=session_id,
            message_id=synthetic_message_id,
            text=cleaned_instruction,
            chat_id=None,
        )
        try:
            result = workflow.doc_execution.prepare_doc_execution(
                synthetic_message,
                llm_result=llm_result,
                workspace_context=workspace_context,
                active_episode_id=None,
                reason="根据工作台指令修订当前协作文档",
                task_run_id=task_run.task_run_id,
                target_document=current_doc,
            )
            result["mode"] = "doc"
            result["session_id"] = session_id
            result["episode_id"] = None
            result["reply_sent"] = False
            result["reply_error"] = None
            workflow.result_persistence.persist_task_run_result(
                task_run.task_run_id,
                message_text=cleaned_instruction,
                result=result,
                session_id=session_id,
            )
        except Exception as exc:  # noqa: BLE001
            workflow.task_run_service.update_task_run(
                task_run.task_run_id,
                stage="failed",
                status="failed",
                latest_error=str(exc),
            )
            workflow.task_run_service.upsert_step(
                task_run.task_run_id,
                step_key="response_generated",
                title="生成文档修订结果",
                step_type="workflow",
                status="failed",
                error=str(exc),
            )
            raise

        return workflow.task_run_service.get_task_run(task_run.task_run_id)

    def revise_slides_from_task_run(
        self,
        source_task_run_id: str,
        *,
        instruction: str,
        requested_by: str = "pilot_workbench",
        artifact_id: str | None = None,
    ):
        workflow = self.workflow
        cleaned_instruction = " ".join((instruction or "").split()).strip()
        if not cleaned_instruction:
            raise ValueError("Slides revision instruction cannot be empty.")

        source_detail = workflow.task_run_service.get_task_run(source_task_run_id)
        if source_detail is None:
            return None

        presentation_tool = workflow._presentation_tool()
        source_artifact = presentation_tool.resolve_slides_artifact(
            getattr(source_detail, "artifacts", []),
            artifact_id=artifact_id,
        )
        if source_artifact is None:
            raise ValueError("No slides package artifact found for this task run.")
        source_artifact_id = presentation_tool.artifact_field(source_artifact, "artifact_id")
        current_package = presentation_tool.preview_payload(
            presentation_tool.artifact_field(source_artifact, "preview_json")
        )
        if not current_package:
            raise ValueError("Slides package artifact has no structured preview to revise.")

        session_id = source_detail.session_id
        revision_start = workflow._workbench_revision_tool().start_run(
            session_id=session_id,
            source_task_run_id=source_task_run_id,
            title=workflow._task_run_title(cleaned_instruction, "slides_revision"),
            intent="slides",
            requested_by=requested_by,
            metadata={
                "source_task_run_id": source_task_run_id,
                "source_artifact_id": source_artifact_id,
                "revision_instruction": cleaned_instruction,
                "requested_by": requested_by,
            },
            step_title="接收演示稿修订指令",
            input_payload={
                "source_task_run_id": source_task_run_id,
                "source_artifact_id": source_artifact_id,
                "instruction": cleaned_instruction,
                "requested_by": requested_by,
            },
            stage="slides_revision_context",
            requirement_id=getattr(source_detail, "requirement_id", None),
        )
        task_run = revision_start.task_run

        synthetic_message_id = revision_start.synthetic_message_id
        workflow._workbench_revision_tool().remember_user_instruction(
            session_id=session_id,
            synthetic_message_id=synthetic_message_id,
            requested_by=requested_by,
            instruction=cleaned_instruction,
            label="slides",
        )
        workspace_context = workflow._workbench_revision_tool().build_workspace_context(
            session_id=session_id,
            synthetic_message_id=synthetic_message_id,
            instruction=cleaned_instruction,
            label="slides",
        )
        workspace_context = workflow._join_context_blocks(
            workspace_context,
            "[当前演示稿包]\n" + json.dumps(current_package, ensure_ascii=False)[:12000],
        )
        workflow.task_run_service.upsert_step(
            task_run.task_run_id,
            step_key="workspace_context",
            title="构建演示稿修订上下文",
            step_type="context",
            status="done",
            output_payload={"context_length": len(workspace_context)},
        )

        provider = "local"
        revised_package: dict
        edit_plan = presentation_tool.plan_revision(current_package, cleaned_instruction)
        workflow.task_run_service.update_task_run(task_run.task_run_id, stage="slides_revision_processing")
        if workflow.llm_service.is_configured():
            try:
                revised_package = workflow.llm_service.revise_presentation_package(
                    current_package,
                    workspace_context,
                    cleaned_instruction,
                )
                edit_plan = presentation_tool.plan_revision(
                    current_package,
                    cleaned_instruction,
                    revised_package,
                )
                if edit_plan.mutation_required and not presentation_tool.package_changed(current_package, revised_package):
                    revised_package = presentation_tool.revise_deterministic(
                        current_package,
                        cleaned_instruction,
                        edit_plan=edit_plan,
                    )
                provider = "llm"
                workflow.task_run_service.upsert_step(
                    task_run.task_run_id,
                    step_key="intent_resolution",
                    title="生成演示稿修订方案",
                    step_type="intent",
                    status="done",
                    output_payload={"provider": provider},
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("LLM slides revision failed, using deterministic fallback: %s", exc)
                revised_package = presentation_tool.revise_deterministic(
                    current_package,
                    cleaned_instruction,
                    edit_plan=edit_plan,
                )
                provider = "fallback"
                workflow.task_run_service.upsert_step(
                    task_run.task_run_id,
                    step_key="intent_resolution",
                    title="生成演示稿修订方案",
                    step_type="intent",
                    status="failed",
                    error=str(exc),
                )
        else:
            revised_package = presentation_tool.revise_deterministic(
                current_package,
                cleaned_instruction,
                edit_plan=edit_plan,
            )

        if edit_plan.mutation_required and not presentation_tool.package_changed(current_package, revised_package):
            error = "Slides revision produced no visible artifact changes; please clarify the target page or edit."
            workflow.task_run_service.update_task_run(task_run.task_run_id, status="failed", error=error)
            raise ValueError(error)

        base_version = coerce_positive_int(
            current_package.get("version") or presentation_tool.artifact_field(source_artifact, "version")
        )
        revised_package["version"] = max(base_version + 1, 2)
        artifact = presentation_tool.persist_artifact(
            revised_package,
            provider=provider,
            session_id=session_id,
            task_run_id=task_run.task_run_id,
        )
        reply_preview = "【演示稿修订】\n" + presentation_tool.format_reply(revised_package, artifact=artifact)
        result = {
            "session_id": session_id,
            "episode_id": None,
            "mode": "slides",
            "analysis": None,
            "reply_preview": reply_preview,
            "reply_sent": False,
            "reply_error": None,
            "artifacts": [artifact],
        }
        workflow.result_persistence.persist_task_run_result(
            task_run.task_run_id,
            message_text=cleaned_instruction,
            result=result,
            session_id=session_id,
        )
        return workflow.task_run_service.get_task_run(task_run.task_run_id)

    def revise_canvas_from_task_run(
        self,
        source_task_run_id: str,
        *,
        instruction: str,
        requested_by: str = "pilot_workbench",
        artifact_id: str | None = None,
    ):
        workflow = self.workflow
        cleaned_instruction = " ".join((instruction or "").split()).strip()
        if not cleaned_instruction:
            raise ValueError("Canvas revision instruction cannot be empty.")

        source_detail = workflow.task_run_service.get_task_run(source_task_run_id)
        if source_detail is None:
            return None

        canvas_tool = workflow._canvas_tool()
        source_artifact = canvas_tool.resolve_canvas_artifact(
            getattr(source_detail, "artifacts", []),
            artifact_id=artifact_id,
        )
        if source_artifact is None:
            raise ValueError("No canvas artifact found for this task run.")
        source_artifact_id = canvas_tool.artifact_field(source_artifact, "artifact_id")
        current_scene = canvas_tool.preview_payload(canvas_tool.artifact_field(source_artifact, "preview_json"))
        if not isinstance(current_scene.get("shapes"), list) or not current_scene.get("shapes"):
            raise ValueError("Canvas artifact has no structured preview to revise.")

        session_id = source_detail.session_id
        revision_start = workflow._workbench_revision_tool().start_run(
            session_id=session_id,
            source_task_run_id=source_task_run_id,
            title=workflow._task_run_title(cleaned_instruction, "canvas_revision"),
            intent="canvas",
            requested_by=requested_by,
            metadata={
                "source_task_run_id": source_task_run_id,
                "source_artifact_id": source_artifact_id,
                "revision_instruction": cleaned_instruction,
                "requested_by": requested_by,
            },
            step_title="接收画布修订指令",
            input_payload={
                "source_task_run_id": source_task_run_id,
                "source_artifact_id": source_artifact_id,
                "instruction": cleaned_instruction,
                "requested_by": requested_by,
            },
            stage="canvas_revision_context",
            requirement_id=getattr(source_detail, "requirement_id", None),
        )
        task_run = revision_start.task_run

        synthetic_message_id = revision_start.synthetic_message_id
        workflow._workbench_revision_tool().remember_user_instruction(
            session_id=session_id,
            synthetic_message_id=synthetic_message_id,
            requested_by=requested_by,
            instruction=cleaned_instruction,
            label="canvas",
        )
        workspace_context = workflow._workbench_revision_tool().build_workspace_context(
            session_id=session_id,
            synthetic_message_id=synthetic_message_id,
            instruction=cleaned_instruction,
            label="canvas",
        )
        workspace_context = workflow._join_context_blocks(
            workspace_context,
            "[当前画布]\n" + json.dumps(current_scene, ensure_ascii=False)[:12000],
        )
        workflow.task_run_service.upsert_step(
            task_run.task_run_id,
            step_key="workspace_context",
            title="构建画布修订上下文",
            step_type="context",
            status="done",
            output_payload={"context_length": len(workspace_context)},
        )

        workflow.task_run_service.update_task_run(task_run.task_run_id, stage="canvas_revision_processing")
        edit_plan = canvas_tool.plan_revision(current_scene, cleaned_instruction)
        revised_scene = canvas_tool.revise_scene_deterministic(
            current_scene,
            cleaned_instruction,
            edit_plan=edit_plan,
        )
        if edit_plan.mutation_required and not canvas_tool.scene_changed(current_scene, revised_scene):
            error = "Canvas revision produced no visible artifact changes; please clarify the target node or layout."
            workflow.task_run_service.update_task_run(task_run.task_run_id, status="failed", error=error)
            raise ValueError(error)

        revised_scene["version"] = max(coerce_positive_int(current_scene.get("version")) + 1, 2)
        artifact = canvas_tool.generate_flow_artifact(
            title=str(revised_scene.get("title") or canvas_tool.artifact_field(source_artifact, "title") or "Canvas"),
            instruction=cleaned_instruction,
            llm_result={"canvas": revised_scene},
            workspace_context=workspace_context,
            task_run_id=task_run.task_run_id,
            session_id=session_id,
        )
        reply_preview = "【画布修订】\n" + canvas_tool.format_reply(artifact)
        result = {
            "session_id": session_id,
            "episode_id": None,
            "mode": "canvas",
            "analysis": None,
            "reply_preview": reply_preview,
            "reply_sent": False,
            "reply_error": None,
            "artifacts": [artifact],
        }
        workflow.result_persistence.persist_task_run_result(
            task_run.task_run_id,
            message_text=cleaned_instruction,
            result=result,
            session_id=session_id,
        )
        return workflow.task_run_service.get_task_run(task_run.task_run_id)

    def _offline_target_document(self, detail: Any) -> dict | None:
        workflow = self.workflow
        requirement_id = str(getattr(detail, "requirement_id", "") or "").strip()
        if requirement_id:
            try:
                requirement = workflow.requirement_service.get_requirement(requirement_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Failed to load requirement current document for offline import: %s", exc)
                requirement = None
            payload = _current_document_payload(getattr(requirement, "current_document", None) if requirement is not None else None)
            if isinstance(payload, dict) and payload.get("document_id"):
                return payload

        documents = getattr(detail, "session_documents", []) or []
        for item in documents:
            payload = _current_document_payload(item)
            if isinstance(payload, dict) and payload.get("is_current") and payload.get("document_id"):
                return payload
        for item in documents:
            payload = _current_document_payload(item)
            if isinstance(payload, dict) and payload.get("document_id"):
                return payload
        return None


def _requirement_answer_index(answer: str) -> int | None:
    match = re.match(r"^(?:第\s*)?(\d+)\s*(?:[.、)）号个]|$)", answer)
    if match:
        return int(match.group(1))
    match = re.search(r"第\s*(\d+)\s*[号个]", answer)
    if match:
        return int(match.group(1))
    return None


def _answer_matches_candidate(answer: str, *, index: int, requirement_id: str, title: str) -> bool:
    if answer == requirement_id or answer == title:
        return True
    if answer == str(index):
        return True
    if re.match(rf"^(第\s*)?{index}\s*([.、)）号个]|$)", answer):
        return True
    if re.match(rf"^{index}\s*[.、)）]\s*", answer):
        return True
    if requirement_id and requirement_id in answer:
        return True
    return bool(title and title in answer)


def _current_document_payload(value: object) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        payload = dump(mode="json")
        return payload if isinstance(payload, dict) else None
    return None


def _offline_package_summary_text(package: dict[str, Any], *, max_items: int, max_length: int) -> str:
    sections = package.get("sections") if isinstance(package.get("sections"), list) else []
    items: list[str] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        heading = str(section.get("heading") or "").strip()
        if heading:
            items.append(heading)
        paragraphs = section.get("paragraphs") if isinstance(section.get("paragraphs"), list) else []
        for paragraph in paragraphs:
            text = str(paragraph).strip()
            if not text:
                continue
            items.append(text.replace("\n", " / "))
            if len(items) >= max_items:
                break
        if len(items) >= max_items:
            break
    if not items:
        title = str(package.get("title") or "").strip()
        return title[:max_length] if title else "补充了离线文档中的最新信息"
    summary = "；".join(items)
    return summary[:max_length]


def _merge_confirmation_instruction(instruction: str, override_instruction: str | None) -> str:
    base = instruction.strip() or "继续处理离线文档"
    if not override_instruction:
        return base
    return f"{base}\n\n[用户补充约束]\n{override_instruction}"


def build_confirmation_resume_instruction(instruction: str, answer_value: str) -> str:
    base = instruction.strip() or "继续刚才的任务"
    return f"{base}\n\n[用户刚刚确认]\n{answer_value}"
