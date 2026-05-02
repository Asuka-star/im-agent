from __future__ import annotations

from typing import Any

from app.core.config import settings


class FeishuArtifactCardBuilder:
    """Builds best-effort Feishu interactive cards for visible artifact delivery."""

    def build_artifact_card(
        self,
        *,
        title: str,
        mode: str,
        artifacts: list[dict] | None,
        summary: str | None = None,
    ) -> dict | None:
        artifact_items = self._artifact_items(artifacts or [])
        if not artifact_items:
            return None
        ready_count = sum(1 for item in artifact_items if item["status"] == "ready")
        lines = [
            f"**任务类型**：{self._mode_label(mode)}",
            f"**交付物**：{len(artifact_items)} 个，{ready_count} 个已就绪",
        ]
        if summary:
            lines.append(f"**摘要**：{self._shorten(summary, 80)}")
        elements: list[dict[str, Any]] = [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "\n".join(lines),
                },
            },
            {"tag": "hr"},
        ]
        for item in artifact_items[:4]:
            elements.append(
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**{item['label']}**：{item['title']}\n{item['detail']}",
                    },
                }
            )
        actions = [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": item["button_text"]},
                "url": item["url"],
                "type": "primary" if index == 0 else "default",
            }
            for index, item in enumerate(artifact_items[:3])
            if item.get("url")
        ]
        if actions:
            elements.append({"tag": "action", "actions": actions})
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "turquoise",
                "title": {"tag": "plain_text", "content": self._shorten(title or "AI 协作产物已生成", 48)},
            },
            "elements": elements,
        }

    def _artifact_items(self, artifacts: list[dict]) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                continue
            artifact_type = str(artifact.get("artifact_type") or "artifact")
            url = self._absolute_url(str(artifact.get("url") or "").strip())
            exports = self._exports(artifact.get("preview"))
            if not url and exports:
                url = exports[0]["url"]
            if not url:
                continue
            label = self._artifact_label(artifact_type)
            result.append(
                {
                    "artifact_type": artifact_type,
                    "label": label,
                    "title": self._shorten(str(artifact.get("title") or label), 42),
                    "status": str(artifact.get("status") or "ready"),
                    "url": url,
                    "button_text": self._button_text(artifact_type),
                    "detail": self._detail(artifact, exports),
                }
            )
        return result

    def _exports(self, preview: object) -> list[dict[str, str]]:
        if not isinstance(preview, dict):
            return []
        exports = preview.get("exports")
        if not isinstance(exports, dict):
            return []
        result = []
        for key, value in exports.items():
            url = self._absolute_url(str(value or "").strip())
            if url:
                result.append({"label": str(key).upper(), "url": url})
        return result

    def _detail(self, artifact: dict, exports: list[dict[str, str]]) -> str:
        artifact_type = str(artifact.get("artifact_type") or "")
        preview = artifact.get("preview") if isinstance(artifact.get("preview"), dict) else {}
        if artifact_type == "slides_package":
            slides = preview.get("slides") if isinstance(preview.get("slides"), list) else []
            notes = sum(
                1
                for item in slides
                if isinstance(item, dict) and str(item.get("speaker_notes") or "").strip()
            )
            return f"{len(slides)} 页，讲者备注 {notes}/{len(slides)} 页"
        if artifact_type == "canvas":
            summary = preview.get("summary") if isinstance(preview.get("summary"), dict) else {}
            node_count = str(summary.get("node_count") or "")
            arrow_count = str(summary.get("arrow_count") or "")
            if node_count or arrow_count:
                return f"{node_count or 0} 节点，{arrow_count or 0} 连线"
        if exports:
            return "导出：" + "、".join(item["label"] for item in exports[:3])
        return "可在链接中查看"

    def _absolute_url(self, value: str) -> str:
        if not value:
            return ""
        if value.startswith("http://") or value.startswith("https://"):
            return value
        base = str(settings.artifact_public_base_url or "").rstrip("/")
        if not base:
            return value
        return f"{base}/{value.lstrip('/')}"

    def _artifact_label(self, artifact_type: str) -> str:
        labels = {
            "document": "协作文档",
            "doc": "协作文档",
            "feishu_doc": "飞书文档",
            "slides": "演示稿",
            "slides_package": "演示稿",
            "canvas": "白板 / Canvas",
            "delivery_bundle": "交付包",
        }
        return labels.get(artifact_type, "协作产物")

    def _button_text(self, artifact_type: str) -> str:
        if artifact_type == "delivery_bundle":
            return "打开交付包"
        if artifact_type in {"slides", "slides_package"}:
            return "打开演示稿"
        if artifact_type == "canvas":
            return "打开白板"
        if artifact_type in {"document", "doc", "feishu_doc"}:
            return "打开文档"
        return "打开产物"

    def _mode_label(self, mode: str) -> str:
        labels = {
            "doc": "文档",
            "slides": "演示稿",
            "canvas": "白板",
            "delivery": "交付",
            "analysis": "分析",
        }
        return labels.get(mode, mode or "协作")

    def _shorten(self, value: str, limit: int) -> str:
        text = " ".join(str(value or "").split())
        if len(text) <= limit:
            return text
        return text[: max(limit - 1, 1)].rstrip() + "…"
