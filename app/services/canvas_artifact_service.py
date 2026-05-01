import json
import re
import uuid
from html import escape
from pathlib import Path


class CanvasArtifactService:
    NODE_PALETTE = [
        {"color": "#EAF5FF", "stroke": "#5A9FD6", "group": "Input"},
        {"color": "#F3F0FF", "stroke": "#8B6FD6", "group": "Agent"},
        {"color": "#EEF8F1", "stroke": "#67A77B", "group": "Tools"},
        {"color": "#FFF4E5", "stroke": "#D6923D", "group": "Artifact"},
    ]

    def __init__(self, *, root_dir: Path | None = None) -> None:
        self.root_dir = root_dir or Path("data") / "artifacts" / "canvas"

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
        scene = self._build_scene(
            title=title,
            instruction=instruction,
            llm_result=llm_result,
            workspace_context=workspace_context,
        )
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
        if not shapes:
            labels = self._flow_labels(canvas, instruction=instruction, workspace_context=workspace_context)
            shapes = self._flow_shapes(labels)
        return {
            "canvas_id": f"canvas_{uuid.uuid4().hex[:12]}",
            "title": str(canvas.get("title") or title or "Canvas").strip() or "Canvas",
            "version": 1,
            "schema": "im-agent.canvas.v1",
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

    def _flow_labels(self, canvas: dict, *, instruction: str, workspace_context: str) -> list[str]:
        raw_nodes = canvas.get("nodes") if isinstance(canvas.get("nodes"), list) else []
        labels = [str(item).strip() for item in raw_nodes if str(item).strip()]
        if labels:
            return labels[:8]

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
