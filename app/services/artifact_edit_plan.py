from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ArtifactEditOperation:
    """One executable mutation on a generated artifact."""

    op_type: str
    target: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ArtifactEditPlan:
    """Artifact-agnostic edit intent produced by an agent or fallback parser."""

    artifact_type: str
    mutation_required: bool
    operations: list[ArtifactEditOperation] = field(default_factory=list)
    scope: str = "unspecified"
    fallback: str = "ask_clarification"
    needs_clarification: bool = False
    question: str = ""

    @property
    def operation_types(self) -> set[str]:
        return {operation.op_type for operation in self.operations if operation.op_type}


class ArtifactEditPlanner:
    """Normalizes edit intent for docs, slides, and canvas artifacts.

    This is deliberately artifact-agnostic. LLM-supplied edit plans are preferred;
    the local parser is only a deterministic fallback for offline/error paths.
    """

    EDIT_PLAN_KEYS = ("artifact_edit_plan", "edit_plan", "mutation_plan")
    MUTATION_MARKERS = (
        "修改",
        "修订",
        "更新",
        "改",
        "整理",
        "规范",
        "格式",
        "排版",
        "美化",
        "润色",
        "压缩",
        "删除",
        "删掉",
        "清空",
        "清除",
        "移除",
        "去掉",
        "新增",
        "添加",
        "加一",
        "重命名",
        "改名",
        "调整",
        "挪",
        "移动",
        "modify",
        "revise",
        "update",
        "rewrite",
        "format",
        "polish",
        "delete",
        "remove",
        "add",
        "append",
        "rename",
        "reorder",
        "move",
        "compress",
    )
    DELETE_MARKERS = ("删除", "删掉", "清空", "清除", "移除", "去掉", "去除", "不要保留", "delete", "remove", "drop", "clear")
    ADD_MARKERS = ("新增", "添加", "追加", "补充", "加一页", "加一张", "加一个", "add", "append", "insert")
    RENAME_MARKERS = ("重命名", "改名", "更名", "改成", "改为", "rename")
    MEDIA_MARKERS = ("图片", "配图", "插图", "示意图", "截图", "image", "mockup", "diagram", "illustration")
    TABLE_MARKERS = ("表格", "表", "table", "matrix")
    LAYOUT_MARKERS = (
        "布局",
        "排版",
        "左侧",
        "右侧",
        "左边",
        "右边",
        "居中",
        "顶部",
        "底部",
        "上方",
        "下方",
        "对齐",
        "left",
        "right",
        "center",
        "top",
        "bottom",
        "align",
        "layout",
    )
    FORMAT_MARKERS = (
        "整理",
        "规范",
        "格式",
        "排版",
        "美化",
        "润色",
        "规整",
        "清理",
        "重新组织",
        "重新总结",
        "重新整理",
        "重新生成",
        "重新写",
        "总结一下",
        "优化",
        "rewrite",
        "format",
        "polish",
    )
    REORDER_MARKERS = ("调整顺序", "重排", "挪到", "移动到", "放到", "reorder", "move")
    COMPRESS_MARKERS = ("压缩", "缩成", "精简", "减少到", "compress", "shorten")

    @classmethod
    def from_llm_result(
        cls,
        llm_result: dict | None,
        *,
        artifact_type: str,
        instruction: str,
        available_targets: list[str] | None = None,
    ) -> ArtifactEditPlan:
        raw_plan = cls._raw_plan(llm_result or {})
        if isinstance(raw_plan, dict):
            plan = cls._parse_raw_plan(raw_plan, artifact_type=artifact_type)
            if plan.operations or plan.mutation_required or plan.needs_clarification:
                return plan
        return cls.from_instruction(
            artifact_type=artifact_type,
            instruction=instruction,
            available_targets=available_targets,
        )

    @classmethod
    def from_instruction(
        cls,
        *,
        artifact_type: str,
        instruction: str,
        available_targets: list[str] | None = None,
    ) -> ArtifactEditPlan:
        text = str(instruction or "").strip()
        lower = text.lower()
        targets = [target for target in (available_targets or []) if str(target).strip()]
        mutation_required = any(marker in text or marker in lower for marker in cls.MUTATION_MARKERS)
        operations: list[ArtifactEditOperation] = []

        has_delete = any(marker in text or marker in lower for marker in cls.DELETE_MARKERS)
        has_add = any(marker in text or marker in lower for marker in cls.ADD_MARKERS)
        has_rename = any(marker in text or marker in lower for marker in cls.RENAME_MARKERS)
        has_compress = any(marker in text or marker in lower for marker in cls.COMPRESS_MARKERS)
        has_reorder = any(marker in text or marker in lower for marker in cls.REORDER_MARKERS)
        has_format = any(marker in text or marker in lower for marker in cls.FORMAT_MARKERS)
        has_layout = any(marker in text or marker in lower for marker in cls.LAYOUT_MARKERS)
        explicit_targets: list[str] = []

        if has_delete:
            clause = cls.operation_clause(text, cls.DELETE_MARKERS)
            explicit_targets = cls.resolve_target_mentions(clause, targets)
            target_payload = cls._target_payload(explicit_targets, clause)
            if artifact_type == "doc":
                target_payload = cls._doc_delete_target_payload(clause, targets, target_payload)
            operations.append(
                ArtifactEditOperation(
                    op_type="delete",
                    target=target_payload,
                    reason="用户要求删除产物中的指定内容",
                )
            )
        if has_add:
            clause = cls.operation_clause(text, cls.ADD_MARKERS)
            explicit_targets = cls.resolve_target_mentions(clause, targets)
            target_payload = cls._target_payload(explicit_targets, clause)
            if cls._looks_like_media_add(clause):
                operations.append(
                    ArtifactEditOperation(
                        op_type="add_media",
                        target=target_payload,
                        payload=cls._media_payload(clause or text),
                        reason="用户要求补充图片或示意图内容",
                    )
                )
            elif cls._looks_like_table_add(clause):
                operations.append(
                    ArtifactEditOperation(
                        op_type="add_table",
                        target=target_payload,
                        payload=cls._table_payload(clause or text),
                        reason="用户要求补充表格内容",
                    )
                )
            else:
                operations.append(
                    ArtifactEditOperation(
                        op_type="append",
                        target=target_payload,
                        payload={"text": clause or text},
                        reason="用户要求补充产物内容",
                    )
                )
        if has_rename:
            clause = cls.operation_clause(text, cls.RENAME_MARKERS)
            explicit_targets = cls.resolve_target_mentions(clause, targets)
            operations.append(
                ArtifactEditOperation(
                    op_type="rename",
                    target=cls._target_payload(explicit_targets, clause),
                    reason="用户要求重命名产物内容",
                )
            )
        if has_compress:
            clause = cls.operation_clause(text, cls.COMPRESS_MARKERS)
            explicit_targets = cls.resolve_target_mentions(clause, targets)
            operations.append(
                ArtifactEditOperation(
                    op_type="compress",
                    target=cls._target_payload(explicit_targets, clause),
                    payload={"max_count": cls.extract_requested_count(clause)},
                    reason="用户要求压缩产物内容",
                )
            )
        if has_reorder:
            clause = cls.operation_clause(text, cls.REORDER_MARKERS)
            explicit_targets = cls.resolve_target_mentions(clause, targets)
            operations.append(
                ArtifactEditOperation(
                    op_type="reorder",
                    target=cls._target_payload(explicit_targets, clause),
                    reason="用户要求调整产物顺序",
                )
            )
        if has_layout:
            clause = cls.operation_clause(text, cls.LAYOUT_MARKERS)
            explicit_targets = cls.resolve_target_mentions(clause, targets)
            operations.append(
                ArtifactEditOperation(
                    op_type="update_layout",
                    target=cls._target_payload(explicit_targets, clause, default_scope="all"),
                    payload=cls._layout_payload(clause or text),
                    reason="用户要求调整产物布局或对齐方式",
                )
            )
        if has_format:
            clause = cls.operation_clause(text, cls.FORMAT_MARKERS)
            explicit_targets = cls.resolve_target_mentions(clause, targets)
            operations.append(
                ArtifactEditOperation(
                    op_type="rewrite",
                    target=cls._target_payload(explicit_targets, clause, default_scope="all"),
                    payload={"goal": clause or text},
                    reason="用户要求重写或规范产物表达",
                )
            )
        if mutation_required and not operations:
            explicit_targets = cls.resolve_target_mentions(text, targets)
            operations.append(
                ArtifactEditOperation(
                    op_type="update",
                    target=cls._target_payload(explicit_targets, text),
                    payload={"instruction": text},
                    reason="用户要求修改产物",
                )
            )

        scope = "targeted" if explicit_targets else ("all" if operations else "unspecified")
        return ArtifactEditPlan(
            artifact_type=artifact_type,
            mutation_required=mutation_required or bool(operations),
            operations=operations,
            scope=scope,
        )

    @classmethod
    def resolve_target_mentions(cls, instruction: str, available_targets: list[str]) -> list[str]:
        normalized_instruction = cls.match_key(instruction)
        if not normalized_instruction:
            return []
        exact = [
            target
            for target in available_targets
            if cls.match_key(target) and cls.match_key(target) in normalized_instruction
        ]
        if exact:
            return exact

        target_match = re.search(
            r"(?:删除|删掉|移除|去掉|去除|修改|更新|整理|重写|delete|remove|update|rewrite)\s*"
            r"(?P<target>.+?)(?:栏|章节|部分|内容|页面|页|节点|画布|一下|$)",
            instruction,
            re.IGNORECASE,
        )
        if not target_match:
            return []
        target_key = cls.match_key(target_match.group("target"))
        if not target_key:
            return []
        return [
            target
            for target in available_targets
            if target_key in cls.match_key(target) or cls.match_key(target) in target_key
        ]

    @classmethod
    def resolve_target_payload_mentions(
        cls,
        target: dict[str, Any] | None,
        available_targets: list[str],
        *,
        payload: dict[str, Any] | None = None,
        allow_all: bool = True,
    ) -> list[str]:
        """Resolve a structured edit-plan target against artifact-specific labels."""

        if not isinstance(target, dict):
            return []
        if allow_all and target.get("scope") == "all":
            return [item for item in available_targets if str(item).strip()]

        queries = target.get("queries") if isinstance(target.get("queries"), list) else []
        for key in ("query", "heading", "title", "name", "id", "label"):
            value = str(target.get(key) or "").strip()
            if value:
                queries = [*queries, value]

        payload = payload if isinstance(payload, dict) else {}
        for key in ("heading", "title", "target_heading", "target_title", "target", "label", "id"):
            value = str(payload.get(key) or "").strip()
            if value:
                queries = [*queries, value]

        query_text = " ".join(str(item) for item in queries if str(item).strip())
        return cls.resolve_target_mentions(query_text, available_targets)

    @staticmethod
    def operation_clause(instruction: str, markers: tuple[str, ...]) -> str:
        text = str(instruction or "").strip()
        if not text:
            return ""
        clauses = [
            clause.strip()
            for clause in re.split(r"(?:\n|，|,|；|;|。|然后|并且|同时|以及|\band\b|\bthen\b)", text, flags=re.IGNORECASE)
            if clause.strip()
        ]
        for clause in clauses:
            lower = clause.lower()
            if any(marker in clause or marker.lower() in lower for marker in markers):
                return clause
        return text

    @staticmethod
    def extract_requested_count(instruction: str) -> int | None:
        match = re.search(r"(\d+)\s*(?:页|张|个|slides?|nodes?)", instruction, re.IGNORECASE)
        if match:
            return max(int(match.group(1)), 1)
        chinese_digits = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7}
        for digit, value in chinese_digits.items():
            if f"{digit}页" in instruction or f"{digit}张" in instruction or f"{digit}个" in instruction:
                return value
        return None

    @staticmethod
    def match_key(value: str) -> str:
        return re.sub(r"[\s\W_]+", "", str(value or "").lower(), flags=re.UNICODE)

    @staticmethod
    def _raw_plan(llm_result: dict) -> dict | None:
        for key in ArtifactEditPlanner.EDIT_PLAN_KEYS:
            raw = llm_result.get(key)
            if isinstance(raw, dict):
                return raw
        return None

    @staticmethod
    def _parse_raw_plan(raw_plan: dict, *, artifact_type: str) -> ArtifactEditPlan:
        raw_operations = raw_plan.get("operations") or raw_plan.get("ops") or []
        operations: list[ArtifactEditOperation] = []
        if isinstance(raw_operations, list):
            for item in raw_operations:
                if not isinstance(item, dict):
                    continue
                op_type = str(item.get("type") or item.get("op") or item.get("action") or "").strip().lower()
                if not op_type:
                    continue
                target = item.get("target") if isinstance(item.get("target"), dict) else {}
                payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
                operations.append(
                    ArtifactEditOperation(
                        op_type=op_type,
                        target=target,
                        payload=payload,
                        reason=str(item.get("reason") or "").strip(),
                    )
                )
        clarification = raw_plan.get("clarification") if isinstance(raw_plan.get("clarification"), dict) else {}
        return ArtifactEditPlan(
            artifact_type=str(raw_plan.get("artifact_type") or artifact_type).strip() or artifact_type,
            mutation_required=bool(raw_plan.get("mutation_required") or operations),
            operations=operations,
            scope=str(raw_plan.get("scope") or ("targeted" if operations else "unspecified")),
            fallback=str(raw_plan.get("fallback") or "ask_clarification"),
            needs_clarification=bool(raw_plan.get("needs_clarification") or clarification.get("needed")),
            question=str(raw_plan.get("question") or clarification.get("question") or "").strip(),
        )

    @staticmethod
    def _target_payload(targets: list[str], instruction: str, *, default_scope: str = "targeted") -> dict[str, Any]:
        if targets:
            return {"kind": "heading", "queries": targets}
        return {"kind": "instruction", "query": instruction, "scope": default_scope}

    @classmethod
    def _doc_delete_target_payload(
        cls,
        instruction: str,
        available_targets: list[str],
        fallback: dict[str, Any],
    ) -> dict[str, Any]:
        text = str(instruction or "").strip()
        lower = text.lower()
        targets = cls.resolve_target_mentions(text, available_targets)
        quoted = cls._quoted_targets(text)
        anchor = targets[0] if targets else (quoted[0] if quoted else "")
        stop_at = targets[1] if len(targets) > 1 else (quoted[1] if len(quoted) > 1 else "")
        target = dict(fallback)
        if anchor:
            target["query"] = anchor
            target["queries"] = targets or [anchor]

        if "之间" in text or "between" in lower:
            target.update(
                {
                    "kind": "anchor_range",
                    "anchor": anchor,
                    "scope": "between",
                    "include_anchor": False,
                    "stop_at": stop_at,
                }
            )
            return target

        if any(marker in text for marker in ("这一组", "这组", "本组")) or (
            anchor and re.match(r"^Update\s*\([^)]+\)$", anchor, re.IGNORECASE) and any(marker in text for marker in ("栏", "以下", "下面"))
        ):
            target.update(
                {
                    "kind": "anchor_range",
                    "anchor": anchor,
                    "scope": "group",
                    "include_anchor": True,
                }
            )
            return target

        if any(
            marker in text
            for marker in (
                "下面的正文",
                "下面正文",
                "下面的内容",
                "下方正文",
                "下方内容",
                "以下正文",
                "以下内容",
                "本节正文",
                "正文内容",
            )
        ) or "body" in lower:
            target.update(
                {
                    "kind": "anchor_range",
                    "anchor": anchor,
                    "scope": "body",
                    "include_anchor": False,
                }
            )
            return target

        if any(marker in text or marker in lower for marker in ("后面", "之后", "后续", "以下", "below", "after")):
            target.update(
                {
                    "kind": "anchor_range",
                    "anchor": anchor,
                    "scope": "after",
                    "include_anchor": False,
                }
            )
            return target

        if any(marker in text or marker in lower for marker in ("前面", "之前", "以上", "before")):
            target.update(
                {
                    "kind": "anchor_range",
                    "anchor": anchor,
                    "scope": "before",
                    "include_anchor": False,
                }
            )
            return target

        if any(marker in text for marker in ("这一节", "本节", "这个章节", "该章节")):
            target.update(
                {
                    "kind": "anchor_range",
                    "anchor": anchor,
                    "scope": "section",
                    "include_anchor": True,
                }
            )
        return target

    @staticmethod
    def _quoted_targets(text: str) -> list[str]:
        targets: list[str] = []
        patterns = (
            r"[“\"]([^”\"]+)[”\"]",
            r"[「『]([^」』]+)[」』]",
            r"'([^']+)'",
        )
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                value = match.group(1).strip()
                if value and value not in targets:
                    targets.append(value)
        return targets

    @classmethod
    def _looks_like_media_add(cls, clause: str) -> bool:
        lower = clause.lower()
        return any(marker in clause or marker in lower for marker in cls.MEDIA_MARKERS)

    @classmethod
    def _looks_like_table_add(cls, clause: str) -> bool:
        lower = clause.lower()
        return any(marker in clause or marker in lower for marker in cls.TABLE_MARKERS)

    @classmethod
    def _media_payload(cls, clause: str) -> dict[str, Any]:
        caption = cls._strip_action_prefix(clause)
        return {
            "media_kind": "image",
            "source": "instruction",
            "caption": caption or "补充图片说明",
        }

    @classmethod
    def _table_payload(cls, clause: str) -> dict[str, Any]:
        title = cls._strip_action_prefix(clause)
        return {
            "title": title or "表格型汇总",
            "rows": [],
        }

    @staticmethod
    def _layout_payload(clause: str) -> dict[str, Any]:
        lower = clause.lower()
        payload: dict[str, Any] = {"instruction": clause}
        position_map = {
            "左侧": "left",
            "左边": "left",
            "left": "left",
            "右侧": "right",
            "右边": "right",
            "right": "right",
            "上方": "top",
            "顶部": "top",
            "top": "top",
            "下方": "bottom",
            "底部": "bottom",
            "bottom": "bottom",
            "居中": "center",
            "center": "center",
        }
        align_map = {
            "顶部对齐": "top",
            "顶对齐": "top",
            "top align": "top",
            "底部对齐": "bottom",
            "bottom align": "bottom",
            "左对齐": "left",
            "left align": "left",
            "右对齐": "right",
            "right align": "right",
            "居中对齐": "center",
            "center align": "center",
        }
        for marker, value in position_map.items():
            if marker in clause or marker in lower:
                payload["position"] = value
                break
        for marker, value in align_map.items():
            if marker in clause or marker in lower:
                payload["align"] = value
                break
        return payload

    @staticmethod
    def _strip_action_prefix(text: str) -> str:
        value = str(text or "").strip()
        value = re.sub(
            r"^(?:请|帮我|请帮我|新增|添加|追加|补充|插入|insert|add|append)\s*",
            "",
            value,
            flags=re.IGNORECASE,
        )
        return value.strip("：:，,；;。 ")
