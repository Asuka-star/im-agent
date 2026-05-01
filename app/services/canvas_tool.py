from __future__ import annotations

import json

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
        return (
            "【Canvas 产物】\n"
            f"标题：{artifact.get('title') or 'Canvas'}\n"
            f"节点/连线数量：{len(shapes)}\n"
            f"预览链接：{artifact.get('url') or ''}"
        )

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
            if operation.op_type == "append":
                shapes.append(self._supplement_node(shapes, str(operation.payload.get("text") or instruction)))
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
