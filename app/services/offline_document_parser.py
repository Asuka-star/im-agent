from __future__ import annotations

import io
import posixpath
import re
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from app.services.document_section_utils import normalize_section_paragraphs


_DOCX_NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
}
_REL_NS = {"rel": "http://schemas.openxmlformats.org/package/2006/relationships"}


@dataclass(slots=True)
class OfflineDocumentParseResult:
    file_name: str
    file_extension: str
    content_type: str | None
    package: dict
    section_count: int
    paragraph_count: int
    headings: list[str]


class OfflineDocumentParser:
    """Parse offline docx/md/txt files into the structured document package used by doc sync."""

    SUPPORTED_EXTENSIONS = {".docx", ".md", ".txt"}

    def parse_bytes(
        self,
        *,
        file_name: str,
        content: bytes,
        content_type: str | None = None,
    ) -> OfflineDocumentParseResult:
        extension = Path(file_name or "").suffix.lower()
        if extension not in self.SUPPORTED_EXTENSIONS:
            raise ValueError("Only docx / md / txt offline documents are supported.")

        if extension == ".docx":
            package = self._parse_docx(file_name=file_name, content=content)
        elif extension == ".md":
            package = self._parse_markdown(file_name=file_name, content=content)
        else:
            package = self._parse_text(file_name=file_name, content=content)

        sections = package.get("sections") if isinstance(package.get("sections"), list) else []
        headings = [str(section.get("heading") or "").strip() for section in sections if isinstance(section, dict)]
        paragraph_count = sum(
            len(section.get("paragraphs") or [])
            for section in sections
            if isinstance(section, dict) and isinstance(section.get("paragraphs"), list)
        )
        return OfflineDocumentParseResult(
            file_name=file_name,
            file_extension=extension,
            content_type=content_type,
            package=package,
            section_count=len(sections),
            paragraph_count=paragraph_count,
            headings=[heading for heading in headings if heading],
        )

    def _parse_docx(self, *, file_name: str, content: bytes) -> dict:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            document_xml = archive.read("word/document.xml")
            image_assets_by_rid, assets = self._docx_image_assets(archive)
        root = ET.fromstring(document_xml)
        body = root.find("w:body", _DOCX_NS)
        blocks: list[tuple[str, object]] = []
        if body is not None:
            for child in list(body):
                tag = self._local_name(child.tag)
                if tag == "p":
                    blocks.extend(self._docx_paragraph_blocks(child, image_assets_by_rid))
                elif tag == "tbl":
                    rows = self._docx_table_rows(child)
                    if rows:
                        blocks.append(("table", rows))
        return self._blocks_to_package(file_name=file_name, blocks=blocks, assets=assets)

    def _parse_markdown(self, *, file_name: str, content: bytes) -> dict:
        text = content.decode("utf-8", errors="ignore").replace("\r\n", "\n").replace("\r", "\n")
        blocks: list[tuple[str, object]] = []
        paragraph_buffer: list[str] = []
        table_buffer: list[str] = []

        def flush_paragraph() -> None:
            if not paragraph_buffer:
                return
            joined = " ".join(item.strip() for item in paragraph_buffer if item.strip()).strip()
            if joined:
                blocks.append(("paragraph", joined))
            paragraph_buffer.clear()

        def flush_table() -> None:
            if not table_buffer:
                return
            rows = self._markdown_table_rows(table_buffer)
            if rows:
                blocks.append(("table", rows))
            table_buffer.clear()

        lines = text.split("\n")
        for index, raw_line in enumerate(lines):
            line = raw_line.rstrip()
            stripped = line.strip()
            next_line = lines[index + 1].strip() if index + 1 < len(lines) else ""
            if not stripped:
                flush_table()
                flush_paragraph()
                continue
            heading_match = re.match(r"^(#{1,3})\s+(.+?)\s*$", stripped)
            if heading_match:
                flush_table()
                flush_paragraph()
                blocks.append(("heading", heading_match.group(2).strip()))
                continue
            image_match = re.match(r"^!\[(?P<alt>[^\]]*)\]\((?P<source>[^)]+)\)\s*$", stripped)
            if image_match:
                flush_table()
                flush_paragraph()
                blocks.append(
                    (
                        "image",
                        {
                            "type": "image",
                            "caption": self._clean_text(image_match.group("alt")),
                            "source": image_match.group("source").strip(),
                        },
                    )
                )
                continue
            if "|" in stripped and re.fullmatch(r"\|?\s*[:-]+(?:\s*\|\s*[:-]+)+\s*\|?", next_line):
                flush_paragraph()
                table_buffer.append(stripped)
                continue
            if "|" in stripped and re.search(r"\|\s*[-:]+", stripped):
                flush_paragraph()
                table_buffer.append(stripped)
                continue
            if table_buffer and "|" in stripped:
                table_buffer.append(stripped)
                continue
            flush_table()
            paragraph_buffer.append(stripped)

        flush_table()
        flush_paragraph()
        return self._blocks_to_package(file_name=file_name, blocks=blocks, assets=[])

    def _parse_text(self, *, file_name: str, content: bytes) -> dict:
        text = content.decode("utf-8", errors="ignore").replace("\r\n", "\n").replace("\r", "\n")
        paragraphs = [item.strip() for item in re.split(r"\n\s*\n+", text) if item.strip()]
        blocks = [("paragraph", paragraph) for paragraph in paragraphs]
        return self._blocks_to_package(file_name=file_name, blocks=blocks, assets=[])

    def _blocks_to_package(
        self,
        *,
        file_name: str,
        blocks: list[tuple[str, object]],
        assets: list[dict[str, object]],
    ) -> dict:
        title = self._title_from_filename(file_name)
        sections: list[dict[str, object]] = []
        current_heading = "\u79bb\u7ebf\u5bfc\u5165\u5185\u5bb9"
        current_paragraphs: list[object] = []

        def flush_section() -> None:
            nonlocal current_paragraphs, current_heading
            cleaned = normalize_section_paragraphs(current_paragraphs)
            if cleaned:
                sections.append({"heading": current_heading, "paragraphs": cleaned})
            current_paragraphs = []

        for block_type, value in blocks:
            if block_type == "heading":
                heading = self._clean_text(value)
                if not heading:
                    continue
                flush_section()
                current_heading = heading
                continue
            if block_type == "paragraph":
                text = self._clean_text(value)
                if text:
                    current_paragraphs.append(text)
                continue
            if block_type == "table":
                rows = self._normalize_table_rows(value)
                if rows:
                    current_paragraphs.append({"type": "table", "rows": rows})
                continue
            if block_type == "image" and isinstance(value, dict):
                image = self._normalize_image_paragraph(value)
                if image:
                    current_paragraphs.append(image)
        flush_section()

        if not sections:
            sections = [
                {
                    "heading": "\u79bb\u7ebf\u5bfc\u5165\u5185\u5bb9",
                    "paragraphs": ["No syncable content was extracted from this file."],
                }
            ]

        package = {
            "title": title,
            "sections": sections,
            "source": {
                "kind": "offline_document",
                "file_name": file_name,
            },
        }
        if assets:
            package["assets"] = assets
        return package

    def _docx_paragraph_blocks(
        self,
        paragraph: ET.Element,
        image_assets_by_rid: dict[str, dict[str, object]],
    ) -> list[tuple[str, object]]:
        blocks: list[tuple[str, object]] = []
        text = self._docx_paragraph_text(paragraph)
        heading_level = self._docx_heading_level(paragraph)
        if text:
            blocks.append(("heading" if heading_level else "paragraph", text))
        for image in self._docx_paragraph_images(paragraph, image_assets_by_rid):
            blocks.append(("image", image))
        return blocks

    def _docx_paragraph_images(
        self,
        paragraph: ET.Element,
        image_assets_by_rid: dict[str, dict[str, object]],
    ) -> list[dict[str, object]]:
        images: list[dict[str, object]] = []
        for drawing in paragraph.findall(".//w:drawing", _DOCX_NS):
            blip = drawing.find(".//a:blip", _DOCX_NS)
            if blip is None:
                continue
            relationship_id = str(
                blip.attrib.get(f"{{{_DOCX_NS['r']}}}embed")
                or blip.attrib.get(f"{{{_DOCX_NS['r']}}}link")
                or ""
            ).strip()
            if not relationship_id:
                continue
            asset = image_assets_by_rid.get(relationship_id)
            if not asset:
                continue
            image: dict[str, object] = {
                "type": "image",
                "asset_id": asset.get("asset_id"),
                "file_name": asset.get("file_name"),
                "local_path": asset.get("local_path"),
                "source": f"offline_asset:{asset.get('asset_id')}",
            }
            caption = self._docx_image_caption(drawing)
            if caption:
                image["caption"] = caption
            width, height = self._docx_image_dimensions(drawing)
            if width is not None:
                image["width"] = width
            if height is not None:
                image["height"] = height
            normalized = self._normalize_image_paragraph(image)
            if normalized:
                images.append(normalized)
        return images

    def _docx_image_assets(self, archive: zipfile.ZipFile) -> tuple[dict[str, dict[str, object]], list[dict[str, object]]]:
        try:
            relationships_xml = archive.read("word/_rels/document.xml.rels")
        except KeyError:
            return {}, []
        rel_root = ET.fromstring(relationships_xml)
        assets_by_rid: dict[str, dict[str, object]] = {}
        assets: list[dict[str, object]] = []
        temp_dir: Path | None = None
        for relationship in rel_root.findall("rel:Relationship", _REL_NS):
            relationship_id = str(relationship.attrib.get("Id") or "").strip()
            target = str(relationship.attrib.get("Target") or "").strip()
            if not relationship_id or not target:
                continue
            archive_path = posixpath.normpath(posixpath.join("word", target))
            if not archive_path.startswith("word/media/"):
                continue
            try:
                payload = archive.read(archive_path)
            except KeyError:
                continue
            if not payload:
                continue
            if temp_dir is None:
                temp_dir = Path(tempfile.mkdtemp(prefix="im_agent_offline_docx_"))
            file_name = Path(archive_path).name or f"{relationship_id}.bin"
            suffix = Path(file_name).suffix or ".bin"
            asset_id = f"img_{len(assets) + 1}"
            local_path = temp_dir / f"{asset_id}{suffix}"
            local_path.write_bytes(payload)
            asset = {
                "asset_id": asset_id,
                "kind": "image",
                "file_name": file_name,
                "local_path": str(local_path),
                "relationship_id": relationship_id,
                "source_path": archive_path,
            }
            assets_by_rid[relationship_id] = asset
            assets.append(asset)
        return assets_by_rid, assets

    def _docx_paragraph_text(self, paragraph: ET.Element) -> str:
        fragments = [node.text or "" for node in paragraph.findall(".//w:t", _DOCX_NS)]
        return self._clean_text("".join(fragments))

    def _docx_heading_level(self, paragraph: ET.Element) -> int:
        style = paragraph.find("./w:pPr/w:pStyle", _DOCX_NS)
        style_value = str(style.attrib.get(f"{{{_DOCX_NS['w']}}}val", "") if style is not None else "").strip().lower()
        if not style_value:
            return 0
        if "heading1" in style_value or style_value in {"title", "heading", "1"}:
            return 1
        if "heading2" in style_value or style_value == "2":
            return 2
        return 0

    def _docx_table_rows(self, table: ET.Element) -> list[list[str]]:
        rows: list[list[str]] = []
        for row in table.findall("./w:tr", _DOCX_NS):
            cells: list[str] = []
            for cell in row.findall("./w:tc", _DOCX_NS):
                texts = [self._docx_paragraph_text(paragraph) for paragraph in cell.findall("./w:p", _DOCX_NS)]
                cell_text = " ".join(item for item in texts if item).strip()
                cells.append(cell_text)
            if any(cells):
                rows.append(cells)
        return self._normalize_table_rows(rows)

    def _docx_image_caption(self, drawing: ET.Element) -> str:
        doc_pr = drawing.find(".//wp:docPr", _DOCX_NS)
        if doc_pr is None:
            return ""
        for key in ("descr", "title", "name"):
            value = self._clean_text(doc_pr.attrib.get(key, ""))
            if value:
                return value
        return ""

    def _docx_image_dimensions(self, drawing: ET.Element) -> tuple[int | None, int | None]:
        extent = drawing.find(".//wp:extent", _DOCX_NS)
        if extent is None:
            return None, None
        return self._emu_to_px(extent.attrib.get("cx")), self._emu_to_px(extent.attrib.get("cy"))

    def _markdown_table_rows(self, lines: list[str]) -> list[list[str]]:
        if len(lines) < 2:
            return []
        body_lines = [line for index, line in enumerate(lines) if index != 1]
        rows = [self._split_markdown_table_row(line) for line in body_lines]
        return self._normalize_table_rows(rows)

    @staticmethod
    def _split_markdown_table_row(line: str) -> list[str]:
        text = line.strip()
        if text.startswith("|"):
            text = text[1:]
        if text.endswith("|"):
            text = text[:-1]
        return [cell.strip() for cell in text.split("|")]

    def _normalize_table_rows(self, rows: object) -> list[list[str]]:
        normalized: list[list[str]] = []
        max_columns = 0
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, list):
                continue
            cells = [self._clean_text(cell) for cell in row]
            if not any(cells):
                continue
            normalized.append(cells)
            max_columns = max(max_columns, len(cells))
        if not normalized or max_columns <= 0:
            return []
        return [cells + [""] * (max_columns - len(cells)) for cells in normalized]

    def _normalize_image_paragraph(self, payload: dict[str, object]) -> dict[str, object] | None:
        source = self._clean_text(payload.get("source") or payload.get("local_path") or "")
        asset_id = self._clean_text(payload.get("asset_id") or "")
        local_path = self._clean_text(payload.get("local_path") or "")
        token = self._clean_text(payload.get("token") or "")
        caption = self._clean_text(payload.get("caption") or payload.get("alt") or "")
        file_name = self._clean_text(payload.get("file_name") or "")
        if not any((source, asset_id, local_path, token, caption, file_name)):
            return None
        image: dict[str, object] = {"type": "image"}
        if token:
            image["token"] = token
        if asset_id:
            image["asset_id"] = asset_id
        if local_path:
            image["local_path"] = local_path
        if source:
            image["source"] = source
        if caption:
            image["caption"] = caption
        if file_name:
            image["file_name"] = file_name
        width = self._safe_positive_int(payload.get("width"))
        height = self._safe_positive_int(payload.get("height"))
        if width is not None:
            image["width"] = width
        if height is not None:
            image["height"] = height
        return image

    @staticmethod
    def _local_name(tag: str) -> str:
        return tag.split("}", 1)[-1] if "}" in tag else tag

    @staticmethod
    def _clean_text(value: object) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    @staticmethod
    def _emu_to_px(value: object) -> int | None:
        try:
            emu = int(str(value or "").strip())
        except (TypeError, ValueError):
            return None
        return max(int(round(emu / 9525)), 1) if emu > 0 else None

    @staticmethod
    def _safe_positive_int(value: object) -> int | None:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    @staticmethod
    def _title_from_filename(file_name: str) -> str:
        stem = Path(file_name or "").stem.strip()
        if not stem:
            return "\u79bb\u7ebf\u5bfc\u5165\u6587\u6863"
        return re.sub(r"[_-]+", " ", stem).strip()[:120] or "\u79bb\u7ebf\u5bfc\u5165\u6587\u6863"
