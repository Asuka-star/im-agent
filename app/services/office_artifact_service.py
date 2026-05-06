import re
from pathlib import Path


class OfficeArtifactService:
    def __init__(self, *, root_dir: Path | None = None) -> None:
        self.root_dir = root_dir or Path("data") / "artifacts"

    def persist_document_markdown(
        self,
        package: dict,
        *,
        sync_lines: list[str],
        task_run_id: str | None,
        session_id: str,
    ) -> dict:
        artifact_dir = self.root_dir / "doc"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        filename = self._document_filename(package, task_run_id=task_run_id, session_id=session_id)
        path = artifact_dir / filename
        path.write_text(self._render_document_markdown(package, sync_lines=sync_lines), encoding="utf-8")
        return {
            "filename": filename,
            "url": f"/api/artifacts/doc/{filename}",
        }

    def _document_filename(
        self,
        package: dict,
        *,
        task_run_id: str | None,
        session_id: str,
    ) -> str:
        title = self._filename_title(str(package.get("title") or "collab_doc").strip() or "collab_doc")
        stem = self._artifact_stem(title, suffix=task_run_id or session_id)[:96]
        return f"{stem or 'collab_doc'}.md"

    @staticmethod
    def _filename_title(title: str) -> str:
        return re.sub(r"\s*[-_]*\s*统计至.+$", "", title).strip() or title

    def _artifact_stem(self, title: str, *, suffix: str | None) -> str:
        title_stem = self._slugify_filename(title)
        suffix_stem = self._short_suffix(suffix)
        if suffix_stem and suffix_stem not in title_stem:
            return f"{title_stem}-{suffix_stem}"
        return title_stem or suffix_stem or "collab_doc"

    @staticmethod
    def _short_suffix(value: str | None) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if text.startswith("run_") and len(text) > 24:
            return text[:16]
        return text[:24]

    def _slugify_filename(self, value: str) -> str:
        slug = re.sub(r"[^\w.-]+", "-", value.strip(), flags=re.UNICODE).strip(".-_")
        return slug or "artifact"

    def _render_document_markdown(self, package: dict, *, sync_lines: list[str]) -> str:
        title = str(package.get("title") or "collab_doc").strip() or "collab_doc"
        lines = [f"# {title}", ""]
        stats_as_of = str(package.get("stats_as_of") or "").strip()
        if stats_as_of:
            lines.extend([f"> Generated at: {stats_as_of}", ""])
        sections = package.get("sections") if isinstance(package.get("sections"), list) else []
        for section in sections:
            if not isinstance(section, dict):
                continue
            heading = str(section.get("heading") or "").strip()
            paragraphs = section.get("paragraphs") if isinstance(section.get("paragraphs"), list) else []
            if heading:
                lines.extend([f"## {heading}", ""])
            for paragraph in paragraphs:
                text = str(paragraph).strip()
                if text:
                    lines.extend([text, ""])
        if sync_lines:
            lines.extend(["## Sync Status", ""])
            lines.extend(str(line).strip() for line in sync_lines if str(line).strip())
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"
