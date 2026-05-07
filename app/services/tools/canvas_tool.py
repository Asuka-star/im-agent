from __future__ import annotations

import json

from app.core.config import settings
from app.services.artifact_edit_plan import ArtifactEditPlan, ArtifactEditPlanner
from app.services.canvas_artifact_service import CanvasArtifactService


class CanvasTool:
    """Generates free-canvas artifacts and concise user-facing previews."""

    def __init__(self, *, artifact_service: CanvasArtifactService) -> None:
        self.artifact_service = artifact_service

    def generate_flow_artifact(
        self,
        *,
        title: str,
        instruction: str,
        llm_result: dict,
        workspace_context: str,
        task_run_id: str | None,
        session_id: str,
    ) -> dict:
        return self.artifact_service.generate_flow(
            title=title,
            instruction=instruction,
            llm_result=llm_result,
            workspace_context=workspace_context,
            task_run_id=task_run_id,
            session_id=session_id,
        )

    def format_reply(self, artifact: dict) -> str:
        preview = artifact.get("preview") if isinstance(artifact.get("preview"), dict) else {}
        shapes = preview.get("shapes") if isinstance(preview.get("shapes"), list) else []
        preview_url = self.public_preview_url(artifact.get("url"))
        return (
            "【Canvas 产物】\n"
            f"标题：{artifact.get('title') or 'Canvas'}\n"
            f"节点/连线数量：{len(shapes)}\n"
            f"预览链接：{preview_url}"
        )

    @classmethod
    def public_preview_url(cls, value: object) -> str:
        url = str(value or "").strip()
        if not url:
            return ""
        if url.startswith(("http://", "https://")):
            return url
        if not url.startswith("/"):
            url = f"/{url}"
        base_url = str(settings.artifact_public_base_url or "").strip().rstrip("/")
        return f"{base_url}{url}" if base_url else url

    def resolve_canvas_artifact(self, artifacts: list | None, *, artifact_id: str | None = None):
        requested_id = (artifact_id or "").strip()
        for artifact in reversed(artifacts or []):
            artifact_type = self.artifact_field(artifact, "artifact_type")
            current_artifact_id = self.artifact_field(artifact, "artifact_id")
            if artifact_type != "canvas":
                continue
            if requested_id and current_artifact_id != requested_id:
                continue
            return artifact
        return None

    def artifact_field(self, artifact: object, field: str):
        if isinstance(artifact, dict):
            return artifact.get(field)
        return getattr(artifact, field, None)

    def preview_payload(self, preview_json: str | None) -> dict:
        if not preview_json:
            return {}
        try:
            payload = json.loads(preview_json)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def plan_revision(self, scene: dict, instruction: str, llm_result: dict | None = None) -> ArtifactEditPlan:
        shapes = scene.get("shapes") if isinstance(scene.get("shapes"), list) else []
        targets: list[str] = []
        for index, shape in enumerate(shapes, start=1):
            if not isinstance(shape, dict):
                continue
            shape_id = str(shape.get("id") or "").strip()
            text = str(shape.get("text") or shape.get("label") or "").strip()
            targets.extend([f"节点{index}", f"Node {index}"])
            if shape_id:
                targets.append(shape_id)
            if text:
                targets.append(text)
        return ArtifactEditPlanner.from_llm_result(
            llm_result,
            artifact_type="canvas",
            instruction=instruction,
            available_targets=targets,
        )

    def revise_scene_deterministic(
        self,
        scene: dict,
        instruction: str,
        *,
        edit_plan: ArtifactEditPlan | None = None,
    ) -> dict:
        edit_plan = edit_plan or self.plan_revision(scene, instruction)
        revised = json.loads(json.dumps(scene, ensure_ascii=False))
        revised["revision_instruction"] = instruction
        revised["artifact_edit_plan"] = {
            "artifact_type": edit_plan.artifact_type,
            "mutation_required": edit_plan.mutation_required,
            "scope": edit_plan.scope,
            "operations": [
                {"type": operation.op_type, "target": operation.target, "payload": operation.payload}
                for operation in edit_plan.operations
            ],
        }
        shapes = revised.get("shapes") if isinstance(revised.get("shapes"), list) else []
        for operation in edit_plan.operations:
            target_indices = self._operation_target_indices(operation.target, instruction, shapes)
            if operation.op_type == "delete":
                for index in sorted(target_indices, reverse=True):
                    if 0 <= index < len(shapes):
                        del shapes[index]
                continue
            if operation.op_type == "add_media":
                shapes.append(self._supplement_media_node(shapes, operation.payload, instruction))
                continue
            if operation.op_type == "add_table":
                shapes.append(self._supplement_table_node(shapes, operation.payload, instruction))
                continue
            if operation.op_type == "append":
                shapes.append(self._supplement_node(shapes, str(operation.payload.get("text") or instruction)))
                continue
            if operation.op_type == "update_layout":
                if not target_indices and self._target_is_specific(operation.target):
                    continue
                self._apply_layout(shapes, target_indices or list(range(len(shapes))), operation.payload)
                continue
            if operation.op_type in {"rewrite", "update", "rename", "reorder"}:
                if not target_indices and self._target_is_specific(operation.target):
                    continue
                for index in target_indices or range(len(shapes)):
                    if 0 <= index < len(shapes) and isinstance(shapes[index], dict):
                        current = str(shapes[index].get("text") or shapes[index].get("label") or "").strip()
                        shapes[index]["text"] = f"{current}\n修订：{instruction}".strip()
        revised["shapes"] = shapes
        return revised

    @staticmethod
    def scene_changed(before: dict, after: dict) -> bool:
        ignored = {"version", "revision_instruction", "artifact_edit_plan", "exports", "summary"}

        def comparable(payload: dict) -> dict:
            return {key: value for key, value in payload.items() if key not in ignored}

        return comparable(before) != comparable(after)

    def _operation_target_indices(self, target: dict, instruction: str, shapes: list) -> list[int]:
        label_to_index: dict[str, int] = {}
        for index, shape in enumerate(shapes):
            if not isinstance(shape, dict):
                continue
            labels = [
                f"节点{index + 1}",
                f"Node {index + 1}",
                str(shape.get("id") or ""),
                str(shape.get("text") or shape.get("label") or ""),
            ]
            for label in labels:
                key = ArtifactEditPlanner.match_key(label)
                if key:
                    label_to_index[key] = index

        if isinstance(target, dict) and target.get("scope") == "all":
            return list(range(len(shapes)))

        matched_labels = ArtifactEditPlanner.resolve_target_payload_mentions(
            target if isinstance(target, dict) else {},
            list(label_to_index.keys()),
            allow_all=False,
        )
        if matched_labels:
            return sorted({label_to_index[ArtifactEditPlanner.match_key(label)] for label in matched_labels})

        instruction_matches = ArtifactEditPlanner.resolve_target_mentions(instruction, list(label_to_index.keys()))
        return sorted({label_to_index[ArtifactEditPlanner.match_key(label)] for label in instruction_matches})

    @staticmethod
    def _target_is_specific(target: dict) -> bool:
        if not isinstance(target, dict) or target.get("scope") == "all":
            return False
        if isinstance(target.get("queries"), list) and target.get("queries"):
            return True
        return any(str(target.get(key) or "").strip() for key in ("query", "heading", "title", "name", "id", "label"))

    @staticmethod
    def _supplement_node(shapes: list, text: str) -> dict:
        node_count = len([shape for shape in shapes if isinstance(shape, dict) and shape.get("type") != "arrow"])
        return {
            "id": f"n{node_count + 1}",
            "type": "node",
            "text": text,
            "x": 80 + node_count * 220,
            "y": 260,
            "w": 168,
            "h": 72,
            "color": "#EEF8F1",
            "stroke": "#67A77B",
            "group": "Revision",
        }

    @staticmethod
    def _supplement_media_node(shapes: list, payload: dict, instruction: str) -> dict:
        node = CanvasTool._supplement_node(shapes, str(payload.get("caption") or instruction).strip() or "补充图片说明")
        node["media_kind"] = str(payload.get("media_kind") or "image").strip() or "image"
        node["source"] = str(payload.get("source") or "instruction").strip() or "instruction"
        node["group"] = "Media"
        node["color"] = "#F8F1E4"
        node["stroke"] = "#C18A3C"
        return node

    @staticmethod
    def _supplement_table_node(shapes: list, payload: dict, instruction: str) -> dict:
        title = str(payload.get("title") or instruction).strip() or "表格型汇总"
        rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
        summary = title if not rows else f"{title}\n{len(rows)} 行表格"
        node = CanvasTool._supplement_node(shapes, summary)
        node["table_rows"] = rows
        node["group"] = "Table"
        node["color"] = "#EAF2FB"
        node["stroke"] = "#5A86B8"
        node["w"] = 208
        node["h"] = 96
        return node

    @staticmethod
    def _apply_layout(shapes: list, indices: list[int], payload: dict) -> None:
        nodes = [
            shape
            for shape in shapes
            if isinstance(shape, dict) and str(shape.get("type") or "").strip().lower() != "arrow"
        ]
        if not nodes:
            return
        xs = [float(shape.get("x") or 0) for shape in nodes]
        ys = [float(shape.get("y") or 0) for shape in nodes]
        left_x = min(xs)
        right_x = max(xs)
        top_y = min(ys)
        bottom_y = max(ys)
        position = str(payload.get("position") or "").strip().lower()
        align = str(payload.get("align") or "").strip().lower()
        for offset, index in enumerate(indices):
            if not (0 <= index < len(shapes)) or not isinstance(shapes[index], dict):
                continue
            shape = shapes[index]
            if str(shape.get("type") or "").strip().lower() == "arrow":
                continue
            if position == "left":
                shape["x"] = left_x - 200
            elif position == "right":
                shape["x"] = right_x + 220
            elif position == "center":
                shape["x"] = round((left_x + right_x) / 2)
            elif position == "top":
                shape["y"] = top_y - 120
            elif position == "bottom":
                shape["y"] = bottom_y + 140

            if align == "top":
                shape["y"] = top_y + offset * 90
            elif align == "bottom":
                shape["y"] = bottom_y + offset * 90
            elif align == "left":
                shape["x"] = left_x
            elif align == "right":
                shape["x"] = right_x
            elif align == "center":
                shape["x"] = round((left_x + right_x) / 2)

            shape["layout_hint"] = {
                "position": position or None,
                "align": align or None,
                "instruction": str(payload.get("instruction") or "").strip() or None,
            }
