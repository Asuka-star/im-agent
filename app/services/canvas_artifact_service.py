import json
import re
import uuid
from html import escape
from pathlib import Path

from app.services.artifact_skills import CanvasSkill


class CanvasArtifactService:
    NODE_PALETTE = CanvasSkill.node_palette

    def __init__(self, *, root_dir: Path | None = None) -> None:
        self.root_dir = root_dir or Path("data") / "artifacts" / "canvas"
        self.canvas_skill = CanvasSkill()

    def generate_flow(
        self,
        *,
        title: str,
        instruction: str,
        llm_result: dict,
        workspace_context: str,
        task_run_id: str | None,
        session_id: str,
    ) -> dict:
        scene = self.canvas_skill.normalize(self._build_scene(
            title=title,
            instruction=instruction,
            llm_result=llm_result,
            workspace_context=workspace_context,
        ))
        filename = self._canvas_filename(scene, task_run_id=task_run_id, session_id=session_id)
        svg_filename = self._svg_filename(filename)
        html_filename = self._html_filename(filename)
        scene["exports"] = {
            "json": f"/api/artifacts/canvas/{filename}",
            "svg": f"/api/artifacts/canvas/{svg_filename}",
            "html": f"/api/artifacts/canvas/{html_filename}",
        }
        self.root_dir.mkdir(parents=True, exist_ok=True)
        path = self.root_dir / filename
        svg_path = self.root_dir / svg_filename
        html_path = self.root_dir / html_filename
        svg = self._build_svg(scene)
        path.write_text(json.dumps(scene, ensure_ascii=False, indent=2), encoding="utf-8")
        svg_path.write_text(svg, encoding="utf-8")
        html_path.write_text(self._build_html(scene, svg), encoding="utf-8")
        return {
            "artifact_type": "canvas",
            "provider": "local",
            "status": "ready",
            "title": scene["title"],
            "url": f"/api/artifacts/canvas/{html_filename}",
            "export_url": f"/api/artifacts/canvas/{svg_filename}",
            "preview": scene,
            "version": scene["version"],
        }

    def _build_scene(
        self,
        *,
        title: str,
        instruction: str,
        llm_result: dict,
        workspace_context: str,
    ) -> dict:
        canvas = llm_result.get("canvas") if isinstance(llm_result.get("canvas"), dict) else {}
        raw_shapes = canvas.get("shapes") if isinstance(canvas.get("shapes"), list) else []
        shapes = self._normalize_shapes(raw_shapes)
        template = self._select_template(canvas, instruction=instruction, workspace_context=workspace_context)
        if not shapes:
            labels = self._flow_labels(canvas, instruction=instruction, workspace_context=workspace_context)
            shapes = self._template_shapes(template, labels)
        return {
            "canvas_id": f"canvas_{uuid.uuid4().hex[:12]}",
            "title": str(canvas.get("title") or title or "Canvas").strip() or "Canvas",
            "version": 1,
            "schema": "im-agent.canvas.v1",
            "template": template,
            "summary": self._scene_summary(template, shapes),
            "shapes": shapes,
        }

    def _normalize_shapes(self, raw_shapes: list) -> list[dict]:
        shapes: list[dict] = []
        for index, item in enumerate(raw_shapes, start=1):
            if not isinstance(item, dict):
                continue
            shape_type = str(item.get("type") or "node").strip().lower()
            if shape_type not in {"node", "rect", "frame", "sticky", "arrow"}:
                shape_type = "node"
            shape = {
                "id": str(item.get("id") or f"s{index}").strip(),
                "type": shape_type,
            }
            if shape_type == "arrow":
                shape.update(
                    {
                        "from": str(item.get("from") or "").strip(),
                        "to": str(item.get("to") or "").strip(),
                        "color": self._hex_color(item.get("color") or item.get("stroke"), "#2F7F8A"),
                        "label": str(item.get("label") or item.get("text") or "").strip(),
                    }
                )
            else:
                palette = self.NODE_PALETTE[(index - 1) % len(self.NODE_PALETTE)]
                shape.update(
                    {
                        "text": str(item.get("text") or item.get("label") or f"Step {index}").strip(),
                        "x": self._int_value(item.get("x"), 80 + (index - 1) * 220),
                        "y": self._int_value(item.get("y"), 140),
                        "w": self._int_value(item.get("w") or item.get("width"), 160),
                        "h": self._int_value(item.get("h") or item.get("height"), 64),
                        "color": self._hex_color(item.get("color") or item.get("fill"), palette["color"]),
                        "stroke": self._hex_color(item.get("stroke") or item.get("border"), palette["stroke"]),
                        "group": str(item.get("group") or palette["group"]).strip(),
                    }
                )
            shapes.append(shape)
        return shapes

    def _int_value(self, value: object, default: int) -> int:
        try:
            return int(value) if value not in (None, "") else default
        except (TypeError, ValueError):
            return default

    def _hex_color(self, value: object, default: str) -> str:
        text = str(value or "").strip()
        if re.fullmatch(r"#[0-9A-Fa-f]{6}", text):
            return text.upper()
        return default

    def _select_template(self, canvas: dict, *, instruction: str, workspace_context: str) -> str:
        raw_template = str(canvas.get("template") or canvas.get("kind") or "").strip().lower()
        aliases = {
            "risk": "risk",
            "risks": "risk",
            "risk_map": "risk",
            "module": "module",
            "modules": "module",
            "architecture": "module",
            "flow": "flow",
            "flowchart": "flow",
            "process": "flow",
        }
        if raw_template in aliases:
            return aliases[raw_template]
        text = f"{instruction}\n{workspace_context}".lower()
        if re.search(r"(风险|隐患|阻塞|延期|延迟|应对|缓解|risk|mitigation|blocker)", text, flags=re.IGNORECASE):
            return "risk"
        if re.search(r"(流程图|流程画布|流程|flowchart|process|flow)", str(instruction or ""), flags=re.IGNORECASE):
            return "flow"
        if re.search(r"(模块|架构|分工|前端|后端|设计|测试|交付|frontend|backend|module|architecture)", text, flags=re.IGNORECASE):
            return "module"
        return "flow"

    def _template_shapes(self, template: str, labels: list[str]) -> list[dict]:
        if template == "risk":
            return self._risk_shapes(labels)
        if template == "module":
            return self._module_shapes(labels)
        return self._flow_shapes(labels)

    def _scene_summary(self, template: str, shapes: list[dict]) -> dict:
        nodes = [shape for shape in shapes if isinstance(shape, dict) and shape.get("type") != "arrow"]
        arrows = [shape for shape in shapes if isinstance(shape, dict) and shape.get("type") == "arrow"]
        groups = sorted({str(shape.get("group") or "").strip() for shape in nodes if str(shape.get("group") or "").strip()})
        return {
            "template": template,
            "node_count": len(nodes),
            "arrow_count": len(arrows),
            "groups": groups,
        }

    def _flow_labels(self, canvas: dict, *, instruction: str, workspace_context: str) -> list[str]:
        raw_nodes = canvas.get("nodes") if isinstance(canvas.get("nodes"), list) else []
        labels = [str(item).strip() for item in raw_nodes if str(item).strip()]
        if labels:
            return labels[:8]

        inferred_labels = self._infer_flow_labels(instruction=instruction, workspace_context=workspace_context)
        if inferred_labels:
            return inferred_labels[:8]

        candidates = re.split(r"[\n,，;；。.!?？]+", f"{instruction}\n{workspace_context}")
        labels = []
        for item in candidates:
            text = " ".join(item.split()).strip()
            if not text or text in labels:
                continue
            labels.append(text[:36])
            if len(labels) >= 5:
                break
        return labels or ["Input", "Agent planning", "Tool execution", "Artifact delivery"]

    def _infer_flow_labels(self, *, instruction: str, workspace_context: str) -> list[str]:
        discussion_text = self._discussion_content(workspace_context)
        labels = self._flow_labels_from_discussion(discussion_text)
        if labels:
            return labels
        return self._flow_labels_from_discussion(instruction)

    def _discussion_content(self, workspace_context: str) -> str:
        lines: list[str] = []
        for raw_line in str(workspace_context or "").splitlines():
            text = raw_line.strip().lstrip("-").strip()
            if not text:
                continue
            if text.startswith("[") and text.endswith("]"):
                continue
            text = self._extract_message_content(text)
            text = self._clean_flow_candidate(text)
            if text and not self._is_canvas_meta_text(text):
                lines.append(text)
        return "\n".join(lines)

    def _extract_message_content(self, text: str) -> str:
        segments = [segment.strip() for segment in re.split(r"\s*\|\s*", text) if segment.strip()]
        for segment in segments:
            safe_match = re.match(r"^(?:\u5185\u5bb9|content)\s*[:\uff1a]\s*(.+)$", segment, flags=re.IGNORECASE)
            if safe_match:
                return safe_match.group(1).strip()
            match = re.match(r"^(?:内容|content)\s*[:：]\s*(.+)$", segment, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return text

    def _flow_labels_from_discussion(self, text: str) -> list[str]:
        labels: list[str] = []
        for sentence in re.split(r"[\n\u3002.!?\uff1f]+", str(text or "")):
            sentence = self._clean_flow_candidate(sentence)
            if not sentence or self._is_canvas_meta_text(sentence):
                continue
            for part in self._split_flow_sentence(sentence):
                label = self._normalize_flow_label(part)
                if label and label not in labels and not self._is_canvas_meta_text(label):
                    labels.append(label[:36])
                if len(labels) >= 6:
                    return labels
        return labels

    def _split_flow_sentence(self, sentence: str) -> list[str]:
        parts: list[str] = []
        for chunk in re.split(r"(?:\u7136\u540e|\u4e4b\u540e|\u5e76\u4e14|\u540c\u65f6|\u518d\u7b49|\u518d|\uff0c|,|\uff1b|;)", sentence):
            chunk = self._clean_flow_candidate(chunk)
            if chunk:
                parts.append(chunk)
        expanded: list[str] = []
        for part in parts or [sentence]:
            if "\u90a3\u4e48" in part:
                before, after = part.split("\u90a3\u4e48", 1)
                expanded.extend([before, after])
            elif "\u5c31\u53ef\u4ee5" in part:
                before, after = part.split("\u5c31\u53ef\u4ee5", 1)
                expanded.extend([before, after])
            else:
                expanded.append(part)
        return [item for item in expanded if self._clean_flow_candidate(item)]

    def _normalize_flow_label(self, text: str) -> str:
        text = self._clean_flow_candidate(text)
        if not text:
            return ""
        text = re.sub(r"^\u5f53(.+?)\u5b8c\u6210\u540e$", lambda match: f"{match.group(1)}\u5b8c\u6210", text)
        text = re.sub(r"^(.+?)\u5b8c\u6210\u540e$", lambda match: f"{match.group(1)}\u5b8c\u6210", text)
        text = re.sub(r"^(?:\u7b49|\u7b49\u5f85)(.+?)\u5b8c\u6210$", lambda match: f"{match.group(1)}\u5b8c\u6210", text)
        text = re.sub(
            r"^(?:\u6211\u4eec\u7684)?\u9879\u76ee(?:\u5c31\u53ef\u4ee5|\u53ef\u4ee5)?\u4e0a\u7ebf(?:\u4e86)?$",
            "\u9879\u76ee\u4e0a\u7ebf",
            text,
        )
        text = re.sub(r"^(.{1,12}?)(?:\u4f60)?\u6765(.+)$", r"\1\2", text)
        text = text.replace("UI", " UI ").replace("  ", " ").strip()
        return text

    def _clean_flow_candidate(self, text: str) -> str:
        text = " ".join(str(text or "").split()).strip()
        text = re.sub(r"^[-*\u2022]\s*", "", text).strip()
        return text.strip("\uff1a:，,\u3002.;\uff1b ")

    def _is_canvas_meta_text(self, text: str) -> bool:
        normalized = self._clean_flow_candidate(text)
        if not normalized:
            return True
        if normalized.startswith("[") and normalized.endswith("]"):
            return True
        if re.search(
            r"(?:\u751f\u6210|\u753b|\u521b\u5efa|\u5236\u4f5c).{0,8}(?:\u753b\u5e03|\u6d41\u7a0b\u56fe|\u6d41\u7a0b\u753b\u5e03|canvas)",
            normalized,
            flags=re.IGNORECASE,
        ):
            return True
        if normalized.lower() in {"canvas", "flowchart", "diagram"}:
            return True
        return normalized.startswith(
            (
                "\u53d1\u8a00\u4eba",
                "speaker",
                "\u534f\u4f5c\u4e0a\u4e0b\u6587",
                "\u8fd1\u671f\u7fa4\u804a\u8ba8\u8bba",
            )
        )

    def _flow_shapes(self, labels: list[str]) -> list[dict]:
        shapes: list[dict] = []
        total = len(labels)
        for index, label in enumerate(labels, start=1):
            node_id = f"n{index}"
            palette = self._flow_node_style(index, total)
            shapes.append(
                {
                    "id": node_id,
                    "type": "node",
                    "text": label,
                    "x": 80 + (index - 1) * 220,
                    "y": 140,
                    "w": 168,
                    "h": 72,
                    "color": palette["color"],
                    "stroke": palette["stroke"],
                    "group": palette["group"],
                }
            )
            if index > 1:
                shapes.append(
                    {
                        "id": f"a{index - 1}",
                        "type": "arrow",
                        "from": f"n{index - 1}",
                        "to": node_id,
                        "color": "#2F7F8A",
                    }
                )
        return shapes

    def _risk_shapes(self, labels: list[str]) -> list[dict]:
        risk_labels = (labels or ["关键任务延期", "外部接口权限不稳定", "交付材料缺少验收证据"])[:5]
        shapes: list[dict] = []
        for index, label in enumerate(risk_labels, start=1):
            y = 88 + (index - 1) * 116
            risk_id = f"r{index}"
            mitigation_id = f"m{index}"
            risk_text = label if re.search(r"(风险|隐患|阻塞|延期|延迟|risk)", label, flags=re.IGNORECASE) else f"风险：{label}"
            shapes.extend(
                [
                    {
                        "id": risk_id,
                        "type": "sticky",
                        "text": risk_text[:42],
                        "x": 80,
                        "y": y,
                        "w": 210,
                        "h": 82,
                        "color": "#FFF1D7",
                        "stroke": "#D6A04B",
                        "group": "风险",
                    },
                    {
                        "id": mitigation_id,
                        "type": "node",
                        "text": self._mitigation_for_label(label),
                        "x": 380,
                        "y": y,
                        "w": 230,
                        "h": 82,
                        "color": "#E7F3E8",
                        "stroke": "#67A77B",
                        "group": "应对",
                    },
                    {
                        "id": f"ra{index}",
                        "type": "arrow",
                        "from": risk_id,
                        "to": mitigation_id,
                        "color": "#2F7F8A",
                        "label": "缓解",
                    },
                ]
            )
        return shapes

    def _mitigation_for_label(self, label: str) -> str:
        text = str(label or "")
        if re.search(r"(权限|接口|api|API|飞书|外部)", text):
            return "应对：准备本地 artifact 兜底，并提前校验权限"
        if re.search(r"(延期|延迟|进度|排期|截止)", text):
            return "应对：拆分里程碑，设置每日同步和缓冲时间"
        if re.search(r"(验收|评委|证据|材料|演示)", text):
            return "应对：补齐验收检查、截图和 Demo 脚本"
        if re.search(r"(质量|错误|失败|不稳定)", text):
            return "应对：增加自动检查和失败降级说明"
        return "应对：明确负责人、截止时间和可验证结果"

    def _module_shapes(self, labels: list[str]) -> list[dict]:
        module_labels = self._module_labels(labels)
        shapes: list[dict] = []
        for index, label in enumerate(module_labels, start=1):
            col = (index - 1) % 3
            row = (index - 1) // 3
            node_id = f"mod{index}"
            shapes.append(
                {
                    "id": node_id,
                    "type": "frame",
                    "text": label,
                    "x": 80 + col * 240,
                    "y": 92 + row * 138,
                    "w": 190,
                    "h": 92,
                    "color": self.NODE_PALETTE[(index - 1) % len(self.NODE_PALETTE)]["color"],
                    "stroke": self.NODE_PALETTE[(index - 1) % len(self.NODE_PALETTE)]["stroke"],
                    "group": "模块",
                }
            )
        if len(shapes) > 1:
            delivery_id = "mod_delivery"
            y = 92 + ((len(module_labels) + 2) // 3) * 138
            shapes.append(
                {
                    "id": delivery_id,
                    "type": "node",
                    "text": "集成交付与验收",
                    "x": 320,
                    "y": y,
                    "w": 220,
                    "h": 82,
                    "color": "#FFF1D7",
                    "stroke": "#EF8354",
                    "group": "交付",
                }
            )
            for index in range(1, len(module_labels) + 1):
                shapes.append(
                    {
                        "id": f"ma{index}",
                        "type": "arrow",
                        "from": f"mod{index}",
                        "to": delivery_id,
                        "color": "#2F7F8A",
                    }
                )
        return shapes

    def _module_labels(self, labels: list[str]) -> list[str]:
        text = "\n".join(labels)
        candidates: list[str] = []
        modules = [
            ("前端体验", r"(前端|UI|页面|Web|web|frontend)"),
            ("后端服务", r"(后端|接口|API|api|服务|backend)"),
            ("设计与内容", r"(设计|内容|文案|视觉|素材)"),
            ("测试验收", r"(测试|验收|质量|回归|检查)"),
            ("交付归档", r"(交付|归档|打包|Demo|demo|演示)"),
        ]
        for label, pattern in modules:
            if re.search(pattern, text, flags=re.IGNORECASE):
                candidates.append(label)
        for label in labels:
            cleaned = self._clean_flow_candidate(label)
            if cleaned and cleaned not in candidates:
                candidates.append(cleaned[:24])
            if len(candidates) >= 6:
                break
        return candidates[:6] or ["前端体验", "后端服务", "测试验收", "交付归档"]

    def _flow_node_style(self, index: int, total: int) -> dict[str, str]:
        if index == 1:
            return self.NODE_PALETTE[0]
        if index == total:
            return self.NODE_PALETTE[3]
        return self.NODE_PALETTE[1]

    def _canvas_filename(self, scene: dict, *, task_run_id: str | None, session_id: str) -> str:
        title = str(scene.get("title") or "canvas").strip() or "canvas"
        stem = self._slugify_filename(task_run_id or f"{session_id}-{title}")[:96]
        return f"{stem or 'canvas'}.json"

    def _svg_filename(self, json_filename: str) -> str:
        stem = json_filename.rsplit(".", 1)[0]
        return f"{stem or 'canvas'}.svg"

    def _html_filename(self, json_filename: str) -> str:
        stem = json_filename.rsplit(".", 1)[0]
        return f"{stem or 'canvas'}.html"

    def _slugify_filename(self, value: str) -> str:
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip(".-_")
        return slug or "artifact"

    def _build_svg(self, scene: dict) -> str:
        shapes = scene.get("shapes") if isinstance(scene.get("shapes"), list) else []
        nodes = [shape for shape in shapes if isinstance(shape, dict) and shape.get("type") != "arrow"]
        arrows = [shape for shape in shapes if isinstance(shape, dict) and shape.get("type") == "arrow"]
        bounds = self._svg_bounds(nodes)
        width = bounds["width"]
        height = bounds["height"]
        offset_x = bounds["offset_x"]
        offset_y = bounds["offset_y"]
        node_by_id = {str(node.get("id") or ""): node for node in nodes}

        parts = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            (
                f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
                f'viewBox="0 0 {width} {height}" role="img">'
            ),
            f"<title>{escape(str(scene.get('title') or 'Canvas'))}</title>",
            '<rect width="100%" height="100%" fill="#F8FBFC"/>',
            '<defs><marker id="arrowhead" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto">'
            '<path d="M0,0 L10,4 L0,8 Z" fill="#2F7F8A"/></marker></defs>',
        ]
        for arrow in arrows:
            rendered = self._svg_arrow(arrow, node_by_id, offset_x=offset_x, offset_y=offset_y)
            if rendered:
                parts.append(rendered)
        for node in nodes:
            parts.append(self._svg_node(node, offset_x=offset_x, offset_y=offset_y))
        parts.append("</svg>")
        return "\n".join(parts)

    def _build_html(self, scene: dict, svg: str) -> str:
        title = escape(str(scene.get("title") or "Canvas"))
        shape_count = len(scene.get("shapes") if isinstance(scene.get("shapes"), list) else [])
        json_url = escape(str(scene.get("exports", {}).get("json") or ""))
        svg_url = escape(str(scene.get("exports", {}).get("svg") or ""))
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f6f8f9; color: #172026; }}
    header {{ padding: 22px 28px; background: #172026; color: white; }}
    header p {{ margin: 6px 0 0; color: #b8c7ce; }}
    main {{ padding: 24px; max-width: 1180px; margin: 0 auto; }}
    .canvas {{ overflow: auto; background: white; border: 1px solid #dce5e9; border-radius: 14px; padding: 18px; box-shadow: 0 14px 34px rgba(23,32,38,.12); }}
    .links {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 16px; }}
    a {{ color: #116a7b; font-weight: 700; }}
  </style>
</head>
<body>
  <header>
    <h1>{title}</h1>
    <p>{shape_count} 个节点/连线 · 自由画布预览</p>
  </header>
  <main>
    <section class="canvas">{svg}</section>
    <nav class="links">
      <a href="{json_url}">JSON 场景</a>
      <a href="{svg_url}">SVG 导出</a>
    </nav>
  </main>
</body>
</html>
"""

    def _svg_bounds(self, nodes: list[dict]) -> dict[str, int]:
        if not nodes:
            return {"width": 640, "height": 360, "offset_x": 40, "offset_y": 40}
        min_x = min(self._int_value(node.get("x"), 80) for node in nodes)
        min_y = min(self._int_value(node.get("y"), 140) for node in nodes)
        max_x = max(
            self._int_value(node.get("x"), 80) + self._int_value(node.get("w"), 168)
            for node in nodes
        )
        max_y = max(
            self._int_value(node.get("y"), 140) + self._int_value(node.get("h"), 72)
            for node in nodes
        )
        width = max(640, max_x - min_x + 80)
        height = max(360, max_y - min_y + 80)
        return {"width": width, "height": height, "offset_x": 40 - min_x, "offset_y": 40 - min_y}

    def _svg_node(self, node: dict, *, offset_x: int, offset_y: int) -> str:
        x = self._int_value(node.get("x"), 80) + offset_x
        y = self._int_value(node.get("y"), 140) + offset_y
        width = self._int_value(node.get("w"), 168)
        height = self._int_value(node.get("h"), 72)
        fill = self._hex_color(node.get("color"), "#EAF5FF")
        stroke = self._hex_color(node.get("stroke"), "#5A9FD6")
        text = escape(str(node.get("text") or node.get("id") or "Node"))
        group = escape(str(node.get("group") or "").strip())
        group_label = (
            f'<text x="{x + 12}" y="{y + 18}" fill="#60717B" font-size="11" '
            f'font-family="Arial, sans-serif">{group}</text>'
            if group
            else ""
        )
        return (
            f'<g id="{escape(str(node.get("id") or ""))}">'
            f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="8" fill="{fill}" '
            f'stroke="{stroke}" stroke-width="2"/>'
            f"{group_label}"
            f'<text x="{x + 12}" y="{y + (42 if group else 38)}" fill="#12313A" font-size="14" '
            f'font-weight="700" font-family="Arial, sans-serif">{text}</text>'
            "</g>"
        )

    def _svg_arrow(self, arrow: dict, node_by_id: dict[str, dict], *, offset_x: int, offset_y: int) -> str | None:
        source = node_by_id.get(str(arrow.get("from") or ""))
        target = node_by_id.get(str(arrow.get("to") or ""))
        if not source or not target:
            return None
        x1 = self._int_value(source.get("x"), 80) + self._int_value(source.get("w"), 168) + offset_x
        y1 = self._int_value(source.get("y"), 140) + self._int_value(source.get("h"), 72) // 2 + offset_y
        x2 = self._int_value(target.get("x"), 80) + offset_x
        y2 = self._int_value(target.get("y"), 140) + self._int_value(target.get("h"), 72) // 2 + offset_y
        color = self._hex_color(arrow.get("color"), "#2F7F8A")
        label = escape(str(arrow.get("label") or "").strip())
        mid_x = (x1 + x2) // 2
        mid_y = (y1 + y2) // 2
        label_svg = (
            f'<text x="{mid_x}" y="{mid_y - 8}" text-anchor="middle" fill="#2F4D55" font-size="12" '
            f'font-family="Arial, sans-serif">{label}</text>'
            if label
            else ""
        )
        return (
            f'<g id="{escape(str(arrow.get("id") or ""))}">'
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="2.5" '
            'marker-end="url(#arrowhead)"/>'
            f"{label_svg}"
            "</g>"
        )
