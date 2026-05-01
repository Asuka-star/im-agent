import json
import re
import zipfile
from html import escape
from pathlib import Path

from app.utils.values import coerce_positive_int


class PresentationArtifactService:
    def __init__(self, *, root_dir: Path | None = None) -> None:
        self.root_dir = root_dir or Path("data") / "artifacts" / "slides"

    def persist_package(
        self,
        package: dict,
        *,
        provider: str,
        task_run_id: str | None,
        session_id: str,
    ) -> dict:
        self.root_dir.mkdir(parents=True, exist_ok=True)
        preview = self._normalize_package(package)
        json_filename = self._slides_filename(preview, task_run_id=task_run_id, session_id=session_id)
        html_filename = self._html_filename(json_filename)
        pptx_filename = self._pptx_filename(json_filename)
        preview["exports"] = {
            "json": f"/api/artifacts/slides/{json_filename}",
            "html": f"/api/artifacts/slides/{html_filename}",
            "pptx": f"/api/artifacts/slides/{pptx_filename}",
        }

        (self.root_dir / json_filename).write_text(
            json.dumps(preview, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.root_dir / html_filename).write_text(self._render_html(preview), encoding="utf-8")
        self._write_pptx(self.root_dir / pptx_filename, preview)

        return {
            "artifact_type": "slides_package",
            "provider": provider,
            "status": "ready",
            "title": str(preview.get("theme") or "演示稿"),
            "url": f"/api/artifacts/slides/{html_filename}",
            "preview": preview,
            "version": coerce_positive_int(preview.get("version")),
        }

    def _normalize_package(self, package: dict) -> dict:
        preview = dict(package or {})
        preview.setdefault("schema", "im-agent.slides.v1")
        preview["version"] = coerce_positive_int(preview.get("version"))
        slides = preview.get("slides") if isinstance(preview.get("slides"), list) else []
        preview["slides"] = [self._normalize_slide(slide, index) for index, slide in enumerate(slides, start=1)]
        return preview

    def _normalize_slide(self, slide: object, index: int) -> dict:
        payload = slide if isinstance(slide, dict) else {}
        title = str(payload.get("title") or f"第{index}页").strip() or f"第{index}页"
        bullets = payload.get("bullets") if isinstance(payload.get("bullets"), list) else []
        notes = (
            payload.get("speaker_notes")
            or payload.get("speaker_note")
            or payload.get("notes")
            or self._fallback_speaker_notes(title, bullets)
        )
        duration = payload.get("duration_sec") or payload.get("duration_seconds") or 45
        try:
            duration_sec = max(15, min(int(duration), 180))
        except (TypeError, ValueError):
            duration_sec = 45
        return {
            **payload,
            "title": title,
            "bullets": [str(item).strip() for item in bullets if str(item).strip()][:5],
            "speaker_notes": str(notes).strip(),
            "duration_sec": duration_sec,
        }

    def _fallback_speaker_notes(self, title: str, bullets: list[object]) -> str:
        key_points = "；".join(str(item).strip() for item in bullets[:3] if str(item).strip())
        if key_points:
            return f"本页围绕“{title}”展开，重点说明：{key_points}。"
        return f"本页围绕“{title}”展开，建议用一句业务场景引入，再给出结论。"

    def _slides_filename(self, package: dict, *, task_run_id: str | None, session_id: str) -> str:
        title = str(package.get("theme") or "slides").strip() or "slides"
        stem = self._slugify_filename(task_run_id or f"{session_id}-{title}")[:96]
        return f"{stem or 'slides'}.json"

    def _html_filename(self, json_filename: str) -> str:
        stem = json_filename.rsplit(".", 1)[0]
        return f"{stem or 'slides'}.html"

    def _pptx_filename(self, json_filename: str) -> str:
        stem = json_filename.rsplit(".", 1)[0]
        return f"{stem or 'slides'}.pptx"

    def _slugify_filename(self, value: str) -> str:
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip(".-_")
        return slug or "artifact"

    def _render_html(self, package: dict) -> str:
        theme = str(package.get("theme") or "演示稿").strip()
        audience = str(package.get("audience") or "").strip()
        slides = package.get("slides") if isinstance(package.get("slides"), list) else []
        slide_html = "\n".join(self._render_slide(slide, index) for index, slide in enumerate(slides, start=1))
        audience_html = f"<p>适用场景：{escape(audience)}</p>" if audience else ""
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(theme)}</title>
  <style>
    :root {{ color-scheme: light; font-family: Inter, "Microsoft YaHei", "PingFang SC", sans-serif; }}
    body {{ margin: 0; background: #f3f7f8; color: #172026; }}
    header {{ padding: 28px 34px 18px; background: #172026; color: white; }}
    header p {{ margin: 8px 0 0; color: #c8d9df; }}
    main {{ display: grid; gap: 22px; padding: 24px; max-width: 1100px; margin: 0 auto; }}
    section {{ aspect-ratio: 16 / 9; background: linear-gradient(135deg, #172026, #274755); color: white; border-radius: 16px; padding: 42px; box-sizing: border-box; box-shadow: 0 18px 38px rgba(23, 32, 38, .18); display: flex; flex-direction: column; }}
    .index {{ font-size: 14px; letter-spacing: .04em; color: #9ed8e4; font-weight: 700; }}
    h2 {{ font-size: clamp(28px, 5vw, 46px); margin: 20px 0 22px; line-height: 1.16; }}
    ul {{ margin: 0; padding-left: 24px; font-size: clamp(17px, 2vw, 24px); line-height: 1.48; }}
    li {{ margin-bottom: 12px; }}
    aside {{ margin-top: auto; background: rgba(255,255,255,.1); border: 1px solid rgba(255,255,255,.16); border-radius: 12px; padding: 14px 16px; color: #eaf5f8; }}
    aside strong {{ color: #f6b26b; }}
  </style>
</head>
<body>
  <header>
    <h1>{escape(theme)}</h1>
    {audience_html}
  </header>
  <main>
    {slide_html}
  </main>
</body>
</html>
"""

    def _render_slide(self, slide: object, index: int) -> str:
        payload = slide if isinstance(slide, dict) else {}
        title = str(payload.get("title") or f"第{index}页").strip()
        bullets = payload.get("bullets") if isinstance(payload.get("bullets"), list) else []
        notes = str(payload.get("speaker_notes") or "").strip()
        duration_sec = payload.get("duration_sec")
        bullet_html = "\n".join(f"<li>{escape(str(item))}</li>" for item in bullets[:5]) or "<li>暂无要点</li>"
        note_html = ""
        if notes:
            note_html = f"<aside><strong>讲者备注</strong> · {escape(str(duration_sec or 45))} 秒<br>{escape(notes)}</aside>"
        return f"""<section>
  <div class="index">P{index:02d}</div>
  <h2>{escape(title)}</h2>
  <ul>{bullet_html}</ul>
  {note_html}
</section>"""

    def _write_pptx(self, path: Path, package: dict) -> None:
        slides = package.get("slides") if isinstance(package.get("slides"), list) else []
        if not slides:
            slides = [{"title": package.get("theme") or "演示稿", "bullets": ["暂无要点"]}]
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", self._pptx_content_types(len(slides)))
            archive.writestr("_rels/.rels", self._pptx_root_rels())
            archive.writestr("ppt/presentation.xml", self._pptx_presentation(len(slides)))
            archive.writestr("ppt/_rels/presentation.xml.rels", self._pptx_presentation_rels(len(slides)))
            archive.writestr("ppt/presProps.xml", self._pptx_empty("p:presentationPr"))
            archive.writestr("ppt/viewProps.xml", self._pptx_empty("p:viewPr"))
            archive.writestr("ppt/tableStyles.xml", self._pptx_table_styles())
            for index, slide in enumerate(slides, start=1):
                archive.writestr(f"ppt/slides/slide{index}.xml", self._pptx_slide(slide, index))
                archive.writestr(f"ppt/slides/_rels/slide{index}.xml.rels", self._pptx_slide_rels())

    def _pptx_content_types(self, slide_count: int) -> str:
        slide_overrides = "\n".join(
            f'<Override PartName="/ppt/slides/slide{index}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
            for index in range(1, slide_count + 1)
        )
        return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
  <Override PartName="/ppt/presProps.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presProps+xml"/>
  <Override PartName="/ppt/viewProps.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.viewProps+xml"/>
  <Override PartName="/ppt/tableStyles.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.tableStyles+xml"/>
  {slide_overrides}
</Types>"""

    def _pptx_root_rels(self) -> str:
        return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>
</Relationships>"""

    def _pptx_presentation(self, slide_count: int) -> str:
        slide_ids = "\n".join(
            f'<p:sldId id="{255 + index}" r:id="rId{index}"/>' for index in range(1, slide_count + 1)
        )
        return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
  xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
  xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:sldSz cx="12192000" cy="6858000" type="screen16x9"/>
  <p:notesSz cx="6858000" cy="9144000"/>
  <p:sldIdLst>
    {slide_ids}
  </p:sldIdLst>
</p:presentation>"""

    def _pptx_presentation_rels(self, slide_count: int) -> str:
        slide_rels = "\n".join(
            f'<Relationship Id="rId{index}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" '
            f'Target="slides/slide{index}.xml"/>'
            for index in range(1, slide_count + 1)
        )
        extra_index = slide_count + 1
        return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  {slide_rels}
  <Relationship Id="rId{extra_index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/presProps" Target="presProps.xml"/>
  <Relationship Id="rId{extra_index + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/viewProps" Target="viewProps.xml"/>
  <Relationship Id="rId{extra_index + 2}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/tableStyles" Target="tableStyles.xml"/>
</Relationships>"""

    def _pptx_slide_rels(self) -> str:
        return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>"""

    def _pptx_slide(self, slide: object, index: int) -> str:
        payload = slide if isinstance(slide, dict) else {}
        title = self._xml_text(str(payload.get("title") or f"第{index}页").strip())
        bullets = payload.get("bullets") if isinstance(payload.get("bullets"), list) else []
        bullet_xml = "\n".join(
            self._pptx_text_box(
                shape_id=30 + bullet_index,
                name=f"Bullet {bullet_index}",
                x=1050000,
                y=2050000 + (bullet_index - 1) * 620000,
                cx=9800000,
                cy=460000,
                text="• " + self._xml_text(str(item).strip()),
                font_size=2100,
                color="334155",
            )
            for bullet_index, item in enumerate(bullets[:5], start=1)
            if str(item).strip()
        )
        if not bullet_xml:
            bullet_xml = self._pptx_text_box(
                shape_id=31,
                name="Bullet 1",
                x=1050000,
                y=2050000,
                cx=9800000,
                cy=460000,
                text="• 暂无要点",
                font_size=2100,
                color="334155",
            )
        notes = self._xml_text(str(payload.get("speaker_notes") or "").strip())
        notes_xml = ""
        if notes:
            notes_xml = self._pptx_text_box(
                shape_id=80,
                name="Speaker Notes",
                x=900000,
                y=5850000,
                cx=10400000,
                cy=520000,
                text=f"讲者备注：{notes}",
                font_size=1200,
                color="64748B",
            )
        return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
  xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
  xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld>
    <p:bg><p:bgPr><a:solidFill><a:srgbClr val="F8FAFC"/></a:solidFill></p:bgPr></p:bg>
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
      {self._pptx_text_box(2, "Title", 700000, 520000, 10800000, 820000, title, 3400, "0F172A", bold=True)}
      {self._pptx_line(3, 700000, 1450000, 10800000, 0)}
      {bullet_xml}
      {notes_xml}
      {self._pptx_text_box(90, "Page", 10900000, 6200000, 600000, 260000, str(index), 1100, "94A3B8")}
    </p:spTree>
  </p:cSld>
  <p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:sld>"""

    def _pptx_text_box(
        self,
        shape_id: int,
        name: str,
        x: int,
        y: int,
        cx: int,
        cy: int,
        text: str,
        font_size: int,
        color: str,
        *,
        bold: bool = False,
    ) -> str:
        bold_attr = ' b="1"' if bold else ""
        return f"""<p:sp>
  <p:nvSpPr><p:cNvPr id="{shape_id}" name="{self._xml_text(name)}"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>
  <p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/></p:spPr>
  <p:txBody><a:bodyPr wrap="square"/><a:lstStyle/><a:p><a:r><a:rPr lang="zh-CN" sz="{font_size}"{bold_attr}><a:solidFill><a:srgbClr val="{color}"/></a:solidFill></a:rPr><a:t>{text}</a:t></a:r><a:endParaRPr lang="zh-CN" sz="{font_size}"/></a:p></p:txBody>
</p:sp>"""

    def _pptx_line(self, shape_id: int, x: int, y: int, cx: int, cy: int) -> str:
        return f"""<p:cxnSp>
  <p:nvCxnSpPr><p:cNvPr id="{shape_id}" name="Divider"/><p:cNvCxnSpPr/><p:nvPr/></p:nvCxnSpPr>
  <p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="line"><a:avLst/></a:prstGeom><a:ln w="22000"><a:solidFill><a:srgbClr val="116A7B"/></a:solidFill></a:ln></p:spPr>
</p:cxnSp>"""

    def _pptx_empty(self, tag: str) -> str:
        return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<{tag} xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
  xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
  xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>"""

    def _pptx_table_styles(self) -> str:
        return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<a:tblStyleLst xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" def="{5C22544A-7EE6-4342-B048-85BDC9FD1C3A}"/>"""

    def _xml_text(self, value: str) -> str:
        return escape(value, quote=True)
