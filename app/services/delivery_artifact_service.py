import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class DeliveryArtifactService:
    def __init__(self, *, root_dir: Path | None = None) -> None:
        self.root_dir = root_dir or Path("data") / "artifacts" / "delivery"

    def persist_bundle(
        self,
        manifest: dict[str, Any],
        *,
        task_run_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        self.root_dir.mkdir(parents=True, exist_ok=True)
        normalized = self._normalize_manifest(manifest, task_run_id=task_run_id, session_id=session_id)
        stem = self._slugify_filename(task_run_id or session_id)[:96] or "delivery"
        json_path = self.root_dir / f"{stem}.json"
        html_path = self.root_dir / f"{stem}.html"
        normalized["exports"] = {
            "json": f"/api/artifacts/delivery/{stem}.json",
            "html": f"/api/artifacts/delivery/{stem}.html",
        }
        feishu_delivery = normalized.get("feishu_delivery")
        if isinstance(feishu_delivery, dict):
            feishu_url = str(feishu_delivery.get("url") or feishu_delivery.get("document_url") or "").strip()
            if feishu_url:
                normalized["exports"]["feishu_doc"] = feishu_url
        json_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
        html_path.write_text(self._render_html(normalized), encoding="utf-8")
        return {
            "artifact_type": "delivery_bundle",
            "provider": "local",
            "status": "ready",
            "title": str(normalized.get("title") or "任务交付包"),
            "url": normalized["exports"]["html"],
            "preview": normalized,
            "version": self._coerce_version(normalized.get("version")),
        }

    def _normalize_manifest(self, manifest: dict[str, Any], *, task_run_id: str, session_id: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        items = manifest.get("artifacts") if isinstance(manifest.get("artifacts"), list) else []
        checks = manifest.get("checks") if isinstance(manifest.get("checks"), list) else []
        artifact_summaries = (
            manifest.get("artifact_summaries")
            if isinstance(manifest.get("artifact_summaries"), list)
            else []
        )
        deliverables = manifest.get("deliverables") if isinstance(manifest.get("deliverables"), list) else []
        return {
            "schema": "agent-pilot.delivery.v1",
            "version": self._coerce_version(manifest.get("version")),
            "title": str(manifest.get("title") or "任务交付包").strip() or "任务交付包",
            "task_run_id": str(manifest.get("task_run_id") or task_run_id),
            "session_id": str(manifest.get("session_id") or session_id),
            "requirement_id": str(manifest.get("requirement_id") or "").strip() or None,
            "generated_at": str(manifest.get("generated_at") or now),
            "summary": str(manifest.get("summary") or "").strip(),
            "source": manifest.get("source") if isinstance(manifest.get("source"), dict) else {},
            "checks": [item for item in checks if isinstance(item, dict)],
            "artifacts": [item for item in items if isinstance(item, dict)],
            "artifact_summaries": [item for item in artifact_summaries if isinstance(item, dict)],
            "deliverables": [item for item in deliverables if isinstance(item, dict)],
            "feishu_delivery": (
                manifest.get("feishu_delivery")
                if isinstance(manifest.get("feishu_delivery"), dict)
                else None
            ),
            "context_pack": manifest.get("context_pack") if isinstance(manifest.get("context_pack"), dict) else {},
            "highlights": [
                str(item).strip()
                for item in (manifest.get("highlights") if isinstance(manifest.get("highlights"), list) else [])
                if str(item).strip()
            ],
            "next_steps": [
                str(item).strip()
                for item in (manifest.get("next_steps") if isinstance(manifest.get("next_steps"), list) else [])
                if str(item).strip()
            ],
        }

    def _coerce_version(self, value: object) -> int:
        try:
            return max(int(value or 1), 1)
        except (TypeError, ValueError):
            return 1

    def _render_html(self, manifest: dict[str, Any]) -> str:
        if manifest.get("deliverables"):
            return self._render_link_index_html(manifest)
        title = html.escape(str(manifest.get("title") or "任务交付包"))
        summary = html.escape(str(manifest.get("summary") or ""))
        generated_at = html.escape(str(manifest.get("generated_at") or ""))
        checks_html = "\n".join(self._render_check(item) for item in manifest.get("checks", []))
        artifacts_html = "\n".join(self._render_artifact(item) for item in manifest.get("artifacts", []))
        summaries_html = "\n".join(self._render_artifact_summary(item) for item in manifest.get("artifact_summaries", []))
        highlights_html = "\n".join(f"<li>{html.escape(str(item))}</li>" for item in manifest.get("highlights", []))
        context_html = self._render_context_pack(manifest.get("context_pack") if isinstance(manifest.get("context_pack"), dict) else {})
        next_steps_html = "\n".join(f"<li>{html.escape(str(item))}</li>" for item in manifest.get("next_steps", []))
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #1e2f38; background: #f6f8f9; }}
    main {{ max-width: 960px; margin: 0 auto; padding: 32px 20px 48px; }}
    header {{ background: #172026; color: white; border-radius: 16px; padding: 24px; }}
    h1 {{ margin: 0 0 8px; font-size: 28px; }}
    h2 {{ margin-top: 28px; font-size: 20px; }}
    .meta {{ color: #b8c7ce; }}
    .grid {{ display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }}
    .card {{ background: white; border: 1px solid #dce5e9; border-radius: 12px; padding: 16px; }}
    .wide {{ grid-column: 1 / -1; }}
    .chips {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0; }}
    .chip {{ background: #edf4f6; color: #31515e; border-radius: 999px; padding: 4px 10px; font-size: 13px; }}
    .muted {{ color: #60717a; }}
    .warn-box {{ color: #8a4b13; background: #fff7e6; border-radius: 8px; padding: 8px 10px; margin-top: 10px; }}
    .ok {{ color: #116a7b; font-weight: 700; }}
    .warn {{ color: #b7791f; font-weight: 700; }}
    .miss {{ color: #c85d3a; font-weight: 700; }}
    a {{ color: #116a7b; }}
  </style>
</head>
<body>
  <main>
    <header>
      <h1>{title}</h1>
      <div class="meta">Generated at {generated_at}</div>
      <p>{summary}</p>
    </header>
    <h2>交付摘要</h2>
    <section class="card wide"><ul>{highlights_html}</ul></section>
    <h2>上下文依据</h2>
    {context_html}
    <h2>验收清单</h2>
    <section class="grid">{checks_html}</section>
    <h2>产物详情</h2>
    <section class="grid">{summaries_html}</section>
    <h2>交付物</h2>
    <section class="grid">{artifacts_html}</section>
    <h2>建议下一步</h2>
    <ul>{next_steps_html}</ul>
  </main>
</body>
</html>
"""

    def _render_link_index_html(self, manifest: dict[str, Any]) -> str:
        title = html.escape(str(manifest.get("title") or "需求交付清单"))
        summary = html.escape(str(manifest.get("summary") or ""))
        generated_at = html.escape(str(manifest.get("generated_at") or ""))
        deliverables = manifest.get("deliverables") if isinstance(manifest.get("deliverables"), list) else []
        deliverables_html = "\n".join(
            self._render_deliverable(item)
            for item in deliverables
            if isinstance(item, dict)
        )
        highlights_html = "\n".join(f"<li>{html.escape(str(item))}</li>" for item in manifest.get("highlights", []))
        next_steps_html = "\n".join(f"<li>{html.escape(str(item))}</li>" for item in manifest.get("next_steps", []))
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #1e2f38; background: #f6f8f9; }}
    main {{ max-width: 880px; margin: 0 auto; padding: 32px 20px 48px; }}
    header {{ background: #16262e; color: white; border-radius: 12px; padding: 22px 24px; }}
    h1 {{ margin: 0 0 8px; font-size: 26px; }}
    h2 {{ margin-top: 28px; font-size: 19px; }}
    .meta {{ color: #c2d1d7; }}
    .grid {{ display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); }}
    .card {{ background: white; border: 1px solid #dce5e9; border-radius: 10px; padding: 16px; }}
    .label {{ color: #60717a; font-size: 13px; }}
    .ready {{ color: #116a7b; font-weight: 700; }}
    .missing {{ color: #9a5b12; font-weight: 700; }}
    .links {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }}
    .links a {{ background: #eaf3f5; border-radius: 999px; color: #116a7b; padding: 6px 10px; text-decoration: none; }}
    .feishu {{ margin-top: 12px; border-top: 1px solid #edf2f4; padding-top: 10px; color: #31515e; font-size: 13px; }}
    .feishu a {{ color: #116a7b; }}
    .sync-chip {{ display: inline-block; margin: 4px 6px 0 0; background: #eef6f2; border-radius: 999px; padding: 3px 8px; }}
    a {{ color: #116a7b; }}
  </style>
</head>
<body>
  <main>
    <header>
      <h1>{title}</h1>
      <div class="meta">Generated at {generated_at}</div>
      <p>{summary}</p>
    </header>
    <h2>最新产物链接</h2>
    <section class="grid">{deliverables_html}</section>
    <h2>说明</h2>
    <section class="card"><ul>{highlights_html}</ul></section>
    <h2>下一步</h2>
    <section class="card"><ul>{next_steps_html}</ul></section>
  </main>
</body>
</html>
"""

    def _render_deliverable(self, item: dict[str, Any]) -> str:
        label = html.escape(str(item.get("label") or item.get("artifact_type") or "产物"))
        title = html.escape(str(item.get("title") or label))
        status = str(item.get("status") or "missing")
        detail = html.escape(str(item.get("detail") or ""))
        css = "ready" if status == "ready" else "missing"
        status_text = "已生成" if status == "ready" else "待补齐"
        links = item.get("links") if isinstance(item.get("links"), list) else []
        links_html = "".join(
            self._render_named_link(link)
            for link in links
            if isinstance(link, dict)
        )
        if not links_html:
            url = str(item.get("url") or "").strip()
            links_html = self._render_named_link({"label": "打开", "url": url}) if url else ""
        feishu_sync_html = self._render_feishu_sync(item.get("feishu_sync"))
        return (
            '<article class="card">'
            f'<div class="label">{label}</div>'
            f'<div class="{css}">{status_text}</div>'
            f'<strong>{title}</strong>'
            f'<p>{detail}</p>'
            f'<div class="links">{links_html}</div>'
            f'{feishu_sync_html}'
            '</article>'
        )

    def _render_feishu_sync(self, sync: Any) -> str:
        if not isinstance(sync, dict):
            return ""
        document_url = str(sync.get("document_url") or "").strip()
        status = html.escape(str(sync.get("status") or "linked"))
        media_items = sync.get("media_items") if isinstance(sync.get("media_items"), list) else []
        media_html = "".join(
            self._render_sync_media_item(item)
            for item in media_items
            if isinstance(item, dict)
        )
        link_html = (
            f'<a href="{html.escape(document_url)}">飞书交付文档</a>'
            if document_url
            else "飞书交付文档"
        )
        return f'<div class="feishu">飞书同步：{status} · {link_html}<div>{media_html}</div></div>'

    def _render_sync_media_item(self, item: dict[str, Any]) -> str:
        label = {
            "canvas_image": "Canvas 图片",
            "slides_pptx": "PPTX 附件",
        }.get(str(item.get("kind") or ""), str(item.get("kind") or "媒体"))
        status = str(item.get("status") or "").strip()
        token = str(item.get("file_token") or "").strip()
        detail = f"{label}: {status or 'ready'}"
        if token:
            detail = f"{detail} · token {token[:8]}"
        return f'<span class="sync-chip">{html.escape(detail)}</span>'

    def _render_named_link(self, link: dict[str, Any]) -> str:
        label = html.escape(str(link.get("label") or "打开"))
        url = str(link.get("url") or "").strip()
        if not url:
            return ""
        return f'<a href="{html.escape(url)}">{label}</a>'

    def _render_check(self, item: dict[str, Any]) -> str:
        label = html.escape(str(item.get("label") or item.get("key") or "检查项"))
        status = str(item.get("status") or "missing")
        detail = html.escape(str(item.get("detail") or ""))
        css = "ok" if status == "ready" else "warn" if status == "partial" else "miss"
        text = "已满足" if status == "ready" else "部分满足" if status == "partial" else "待补齐"
        return f'<article class="card"><div class="{css}">{text}</div><strong>{label}</strong><p>{detail}</p></article>'

    def _render_artifact(self, item: dict[str, Any]) -> str:
        title = html.escape(str(item.get("title") or item.get("artifact_type") or "产物"))
        artifact_type = html.escape(str(item.get("artifact_type") or "artifact"))
        url = str(item.get("url") or "").strip()
        version = html.escape(str(item.get("version") or 1))
        link = f'<a href="{html.escape(url)}">{html.escape(url)}</a>' if url else "<span>无链接</span>"
        return f'<article class="card"><strong>{title}</strong><p>{artifact_type} · v{version}</p>{link}</article>'

    def _render_context_pack(self, pack: dict[str, Any]) -> str:
        summary = html.escape(str(pack.get("summary") or "暂无上下文摘要"))
        used = pack.get("used_sources") if isinstance(pack.get("used_sources"), list) else []
        missing = pack.get("missing_items") if isinstance(pack.get("missing_items"), list) else []
        suggestions = pack.get("suggested_inputs") if isinstance(pack.get("suggested_inputs"), list) else []
        used_html = "".join(self._render_context_item(item) for item in used if isinstance(item, dict))
        missing_html = "".join(self._render_context_item(item) for item in missing if isinstance(item, dict))
        suggestions_html = "".join(f'<span class="chip">{html.escape(str(item))}</span>' for item in suggestions)
        return (
            '<section class="grid">'
            f'<article class="card"><strong>已使用材料</strong><p>{summary}</p>{used_html}</article>'
            f'<article class="card"><strong>建议补充</strong>{missing_html}<div class="chips">{suggestions_html}</div></article>'
            '</section>'
        )

    def _render_context_item(self, item: dict[str, Any]) -> str:
        label = html.escape(str(item.get("label") or item.get("kind") or "上下文"))
        detail = html.escape(str(item.get("detail") or ""))
        status = html.escape(str(item.get("status") or "ready"))
        url = str(item.get("url") or "").strip()
        link = f' <a href="{html.escape(url)}">打开</a>' if url else ""
        return f'<p><strong>{label}</strong> <span class="muted">{status}</span><br>{detail}{link}</p>'

    def _render_artifact_summary(self, item: dict[str, Any]) -> str:
        title = html.escape(str(item.get("title") or item.get("label") or "产物"))
        label = html.escape(str(item.get("label") or item.get("artifact_type") or "产物"))
        status = html.escape(str(item.get("status") or "ready"))
        metrics = item.get("metrics") if isinstance(item.get("metrics"), list) else []
        highlights = item.get("highlights") if isinstance(item.get("highlights"), list) else []
        warnings = item.get("warnings") if isinstance(item.get("warnings"), list) else []
        exports = item.get("exports") if isinstance(item.get("exports"), list) else []
        metrics_html = "".join(f'<span class="chip">{html.escape(str(metric))}</span>' for metric in metrics)
        highlights_html = "".join(f"<li>{html.escape(str(text))}</li>" for text in highlights)
        warnings_html = "".join(f"<li>{html.escape(str(text))}</li>" for text in warnings)
        export_links = " ".join(
            self._render_export_link(export)
            for export in exports
            if isinstance(export, dict)
        )
        warning_block = f'<div class="warn-box"><ul>{warnings_html}</ul></div>' if warnings_html else ""
        return (
            '<article class="card">'
            f'<div class="muted">{label} · {status}</div>'
            f'<strong>{title}</strong>'
            f'<div class="chips">{metrics_html}</div>'
            f'<ul>{highlights_html}</ul>'
            f'{warning_block}'
            f'<p>{export_links}</p>'
            '</article>'
        )

    def _render_export_link(self, export: dict[str, Any]) -> str:
        label = html.escape(str(export.get("label") or "导出"))
        url = str(export.get("url") or "").strip()
        if not url:
            return ""
        return f'<a href="{html.escape(url)}">{label}</a>'

    def _slugify_filename(self, value: str) -> str:
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip(".-_")
        return slug or "delivery"
