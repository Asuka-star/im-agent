import unittest
from pathlib import Path
from types import SimpleNamespace

from app.services.feishu_artifact_integrator import FeishuArtifactIntegrator, settings


class FakeDocAPI:
    def __init__(self, *, configured: bool = True) -> None:
        self.configured = configured
        self.created: list[dict] = []
        self.appended: list[dict] = []
        self.replaced: list[dict] = []

    def is_configured(self) -> bool:
        return self.configured

    def create_document_from_sections(self, title: str, sections: list[dict]) -> dict:
        self.created.append({"title": title, "sections": sections})
        return {
            "document_id": "doc_created",
            "url": "https://feishu.example/doc_created",
            "title": title,
            "section_block_index": [{"heading": "交付总览", "block_ids": ["b1"]}],
        }

    def append_sections_to_document(self, document_id: str, title: str, sections: list[dict]) -> dict:
        self.appended.append({"document_id": document_id, "title": title, "sections": sections})
        return {
            "document_id": document_id,
            "url": f"https://feishu.example/{document_id}",
            "title": title,
        }

    def replace_document_sections(
        self,
        document_id: str,
        title: str,
        sections: list[dict],
        *,
        target_headings: list[str] | None = None,
        append_headings: list[str] | None = None,
        **_: dict,
    ) -> dict:
        self.replaced.append(
            {
                "document_id": document_id,
                "title": title,
                "sections": sections,
                "target_headings": list(target_headings or []),
                "append_headings": list(append_headings or []),
            }
        )
        return {
            "document_id": document_id,
            "url": f"https://feishu.example/{document_id}",
            "title": title,
            "section_snapshot": sections,
            "section_block_index": [
                {"heading": str(section.get("heading") or ""), "block_ids": [f"block_{index}"]}
                for index, section in enumerate(sections, start=1)
            ],
        }


class FakeSessionDocumentService:
    def __init__(self) -> None:
        self.saved: list[dict] = []
        self.current: dict | None = None

    def get_current_document(self, session_id: str) -> dict | None:
        return self.current

    def save_current_document(self, session_id: str, **kwargs) -> dict:
        payload = {"session_id": session_id, **kwargs}
        self.saved.append(payload)
        self.current = payload
        return payload


class FakeMediaAPI:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.uploads = []
        self.file_uploads = []
        self.import_uploads = []

    def upload_docx_image(self, *, document_id: str, file_path, file_name: str | None = None) -> dict:
        self.uploads.append({"document_id": document_id, "file_path": Path(file_path), "file_name": file_name})
        if self.fail:
            raise RuntimeError("upload failed")
        return {"file_token": "img_token_1"}

    def upload_docx_file(self, *, document_id: str, file_path, file_name: str | None = None) -> dict:
        self.file_uploads.append({"document_id": document_id, "file_path": Path(file_path), "file_name": file_name})
        if self.fail:
            raise RuntimeError("upload failed")
        return {"file_token": "file_token_1"}

    def upload_import_file(self, *, file_path, file_name: str | None = None, file_extension: str, obj_type: str) -> dict:
        self.import_uploads.append(
            {
                "file_path": Path(file_path),
                "file_name": file_name,
                "file_extension": file_extension,
                "obj_type": obj_type,
            }
        )
        if self.fail:
            raise RuntimeError("upload failed")
        return {"file_token": "import_file_token_1"}


class FakeImportAPI:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.created: list[dict] = []
        self.waited: list[dict] = []

    def create_import_task(
        self,
        *,
        file_token: str,
        file_extension: str,
        import_type: str,
        file_name: str | None = None,
        folder_token: str | None = None,
    ) -> dict:
        self.created.append(
            {
                "file_token": file_token,
                "file_extension": file_extension,
                "import_type": import_type,
                "file_name": file_name,
                "folder_token": folder_token,
            }
        )
        if self.fail:
            raise RuntimeError("import failed")
        return {"ticket": "ticket_1", "status": "pending"}

    def wait_for_import(
        self,
        ticket: str,
        *,
        timeout_seconds: float | None = None,
        poll_seconds: float | None = None,
    ) -> dict:
        self.waited.append({"ticket": ticket, "timeout_seconds": timeout_seconds, "poll_seconds": poll_seconds})
        if self.fail:
            raise RuntimeError("import failed")
        return {
            "ticket": ticket,
            "status": "ready",
            "token": "cloud_token_1",
            "url": "https://feishu.example/cloud_slides",
            "type": "slides",
        }


class FakeRequirementService:
    def __init__(self, current_document=None) -> None:
        self.current_document = current_document

    def get_requirement(self, requirement_id: str):
        return SimpleNamespace(current_document=self.current_document)


class FeishuArtifactIntegratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original = {
            "feishu_artifact_sync_enabled": settings.feishu_artifact_sync_enabled,
            "feishu_artifact_sync_on_delivery": settings.feishu_artifact_sync_on_delivery,
            "feishu_artifact_canvas_image_enabled": settings.feishu_artifact_canvas_image_enabled,
            "feishu_artifact_slides_upload_enabled": settings.feishu_artifact_slides_upload_enabled,
            "feishu_artifact_slides_import_enabled": settings.feishu_artifact_slides_import_enabled,
            "feishu_artifact_slides_import_type": settings.feishu_artifact_slides_import_type,
            "feishu_artifact_slides_import_poll_seconds": settings.feishu_artifact_slides_import_poll_seconds,
            "feishu_artifact_slides_import_timeout_seconds": settings.feishu_artifact_slides_import_timeout_seconds,
            "artifact_public_base_url": settings.artifact_public_base_url,
        }
        settings.feishu_artifact_sync_enabled = True
        settings.feishu_artifact_sync_on_delivery = True
        settings.feishu_artifact_canvas_image_enabled = False
        settings.feishu_artifact_slides_upload_enabled = False
        settings.feishu_artifact_slides_import_enabled = False
        settings.feishu_artifact_slides_import_type = "slides"
        settings.feishu_artifact_slides_import_poll_seconds = 0
        settings.feishu_artifact_slides_import_timeout_seconds = 1
        settings.artifact_public_base_url = "https://public.example"

    def tearDown(self) -> None:
        for key, value in self.original.items():
            setattr(settings, key, value)

    def test_sync_delivery_manifest_creates_doc_with_canvas_and_ppt_links(self) -> None:
        doc_api = FakeDocAPI()
        session_documents = FakeSessionDocumentService()
        integrator = FeishuArtifactIntegrator(
            doc_api=doc_api,
            session_document_service=session_documents,
            requirement_service=FakeRequirementService(),
        )
        detail = SimpleNamespace(
            task_run_id="run_1",
            session_id="session_1",
            requirement_id="req_1",
            title="评审交付",
            session_documents=[],
        )
        manifest = _manifest()

        result = integrator.sync_delivery_manifest(detail, manifest)

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["document_id"], "doc_created")
        self.assertEqual(result["url"], "https://feishu.example/doc_created")
        self.assertEqual(len(doc_api.created), 1)
        sections = doc_api.created[0]["sections"]
        section_text = "\n".join(
            str(paragraph)
            for section in sections
            for paragraph in section.get("paragraphs", [])
        )
        self.assertIn("https://public.example/api/artifacts/canvas/run_1.html", section_text)
        self.assertIn("https://public.example/api/artifacts/slides/run_1.pptx", section_text)
        self.assertEqual(result["artifact_syncs"]["slides"]["status"], "linked")
        self.assertEqual(result["artifact_syncs"]["canvas"]["status"], "linked")
        self.assertEqual(session_documents.saved[0]["sync_mode"], "delivery_created")

    def test_sync_delivery_manifest_appends_to_existing_requirement_doc(self) -> None:
        doc_api = FakeDocAPI()
        session_documents = FakeSessionDocumentService()
        integrator = FeishuArtifactIntegrator(
            doc_api=doc_api,
            session_document_service=session_documents,
            requirement_service=FakeRequirementService(
                current_document={
                    "document_id": "doc_existing",
                    "url": "https://feishu.example/doc_existing",
                    "version": 3,
                    "section_snapshot": [{"heading": "原需求文档", "paragraphs": ["保留我"]}],
                    "section_block_index": [{"heading": "原需求文档", "block_ids": ["old_block"]}],
                }
            ),
        )
        detail = SimpleNamespace(
            task_run_id="run_1",
            session_id="session_1",
            requirement_id="req_1",
            title="评审交付",
            session_documents=[],
        )

        result = integrator.sync_delivery_manifest(detail, _manifest())

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["document_id"], "doc_existing")
        self.assertEqual(len(doc_api.appended), 1)
        self.assertEqual(session_documents.saved[0]["sync_mode"], "delivery_updated")
        self.assertEqual(session_documents.saved[0]["version"], 4)
        self.assertEqual(session_documents.saved[0]["section_snapshot"][0]["heading"], "原需求文档")
        self.assertEqual(session_documents.saved[0]["section_block_index"][0]["heading"], "原需求文档")
        self.assertEqual(result["scope"], "requirement")
        self.assertEqual(result["requirement_id"], "req_1")

    def test_requirement_scoped_sync_does_not_fallback_to_other_session_current_doc(self) -> None:
        doc_api = FakeDocAPI()
        session_documents = FakeSessionDocumentService()
        session_documents.current = {
            "document_id": "doc_other_requirement",
            "url": "https://feishu.example/doc_other_requirement",
            "title": "其他需求的文档",
            "version": 8,
            "task_run_id": "run_other",
        }
        integrator = FeishuArtifactIntegrator(
            doc_api=doc_api,
            session_document_service=session_documents,
            requirement_service=FakeRequirementService(current_document=None),
        )
        detail = SimpleNamespace(
            task_run_id="run_1",
            session_id="session_1",
            requirement_id="req_1",
            title="评审交付",
            session_documents=[],
        )

        result = integrator.sync_delivery_manifest(detail, _manifest())

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["document_id"], "doc_created")
        self.assertEqual(len(doc_api.created), 1)
        self.assertFalse(doc_api.appended)
        self.assertEqual(session_documents.saved[0]["sync_mode"], "delivery_created")

    def test_sync_delivery_manifest_skips_when_disabled(self) -> None:
        settings.feishu_artifact_sync_enabled = False
        integrator = FeishuArtifactIntegrator(
            doc_api=FakeDocAPI(),
            session_document_service=FakeSessionDocumentService(),
        )

        result = integrator.sync_delivery_manifest(SimpleNamespace(session_id="session_1"), _manifest())

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "feishu_artifact_sync_disabled")

    def test_sync_delivery_manifest_uploads_canvas_image_when_enabled(self) -> None:
        settings.feishu_artifact_canvas_image_enabled = True
        svg_path = Path("data") / "artifacts" / "canvas" / "run_1.svg"
        png_path = Path("data") / "artifacts" / "canvas" / "run_1.png"
        svg_path.parent.mkdir(parents=True, exist_ok=True)
        svg_path.write_text("<svg></svg>", encoding="utf-8")
        png_path.write_bytes(b"png")
        doc_api = FakeDocAPI()
        media_api = FakeMediaAPI()
        try:
            integrator = FeishuArtifactIntegrator(
                doc_api=doc_api,
                media_api=media_api,
                session_document_service=FakeSessionDocumentService(),
                requirement_service=FakeRequirementService(),
            )
            detail = SimpleNamespace(
                task_run_id="run_1",
                session_id="session_1",
                requirement_id="req_1",
                title="评审交付",
                session_documents=[],
            )

            result = integrator.sync_delivery_manifest(detail, _manifest())
        finally:
            svg_path.unlink(missing_ok=True)
            png_path.unlink(missing_ok=True)

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["media_items"][0]["kind"], "canvas_image")
        self.assertEqual(result["media_items"][0]["file_token"], "img_token_1")
        self.assertEqual(result["canvas_sync"]["status"], "ready")
        self.assertEqual(result["canvas_sync"]["media_items"][0]["file_token"], "img_token_1")
        self.assertEqual(media_api.uploads[0]["document_id"], "doc_created")
        self.assertEqual(media_api.uploads[0]["file_name"], "run_1.png")
        image_append = doc_api.appended[-1]["sections"][0]["paragraphs"][0]
        self.assertEqual(image_append["type"], "image")
        self.assertEqual(image_append["token"], "img_token_1")
        saved_snapshot = integrator.session_document_service.saved[0]["section_snapshot"]
        self.assertEqual(saved_snapshot[-1]["heading"], "Canvas 图片预览")

    def test_sync_delivery_manifest_keeps_doc_ready_when_canvas_image_upload_fails(self) -> None:
        settings.feishu_artifact_canvas_image_enabled = True
        svg_path = Path("data") / "artifacts" / "canvas" / "run_1.svg"
        png_path = Path("data") / "artifacts" / "canvas" / "run_1.png"
        svg_path.parent.mkdir(parents=True, exist_ok=True)
        svg_path.write_text("<svg></svg>", encoding="utf-8")
        png_path.write_bytes(b"png")
        try:
            integrator = FeishuArtifactIntegrator(
                doc_api=FakeDocAPI(),
                media_api=FakeMediaAPI(fail=True),
                session_document_service=FakeSessionDocumentService(),
                requirement_service=FakeRequirementService(),
            )

            result = integrator.sync_delivery_manifest(
                SimpleNamespace(
                    task_run_id="run_1",
                    session_id="session_1",
                    requirement_id="req_1",
                    title="评审交付",
                    session_documents=[],
                ),
                _manifest(),
            )
        finally:
            svg_path.unlink(missing_ok=True)
            png_path.unlink(missing_ok=True)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["document_id"], "doc_created")
        self.assertTrue(result["warnings"])
        self.assertTrue(any(item["kind"] == "canvas_link" for item in result["canvas_sync"]["items"]))

    def test_sync_delivery_manifest_falls_back_to_canvas_links_when_png_is_missing(self) -> None:
        settings.feishu_artifact_canvas_image_enabled = True
        svg_path = Path("data") / "artifacts" / "canvas" / "run_1.svg"
        svg_path.parent.mkdir(parents=True, exist_ok=True)
        svg_path.write_text("<svg></svg>", encoding="utf-8")
        doc_api = FakeDocAPI()
        media_api = FakeMediaAPI()
        try:
            integrator = FeishuArtifactIntegrator(
                doc_api=doc_api,
                media_api=media_api,
                session_document_service=FakeSessionDocumentService(),
                requirement_service=FakeRequirementService(),
            )

            result = integrator.sync_delivery_manifest(
                SimpleNamespace(
                    task_run_id="run_1",
                    session_id="session_1",
                    requirement_id="req_1",
                    title="评审交付",
                    session_documents=[],
                ),
                _manifest(),
            )
        finally:
            svg_path.unlink(missing_ok=True)

        self.assertEqual(result["status"], "partial")
        self.assertFalse(media_api.uploads)
        self.assertTrue(any(item["kind"] == "canvas_link" for item in result["canvas_sync"]["items"]))
        self.assertEqual(doc_api.appended[-1]["sections"][0]["heading"], "Canvas 预览链接")

    def test_sync_delivery_manifest_uploads_slides_pptx_when_enabled(self) -> None:
        settings.feishu_artifact_slides_upload_enabled = True
        pptx_path = Path("data") / "artifacts" / "slides" / "run_1.pptx"
        pptx_path.parent.mkdir(parents=True, exist_ok=True)
        pptx_path.write_bytes(b"pptx")
        doc_api = FakeDocAPI()
        media_api = FakeMediaAPI()
        try:
            integrator = FeishuArtifactIntegrator(
                doc_api=doc_api,
                media_api=media_api,
                session_document_service=FakeSessionDocumentService(),
                requirement_service=FakeRequirementService(),
            )
            detail = SimpleNamespace(
                task_run_id="run_1",
                session_id="session_1",
                requirement_id="req_1",
                title="评审交付",
                session_documents=[],
            )

            result = integrator.sync_delivery_manifest(detail, _manifest())
        finally:
            pptx_path.unlink(missing_ok=True)

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["media_items"][0]["kind"], "slides_pptx")
        self.assertEqual(result["media_items"][0]["file_token"], "file_token_1")
        self.assertEqual(result["slides_sync"]["status"], "ready")
        self.assertEqual(result["slides_sync"]["media_items"][0]["file_token"], "file_token_1")
        self.assertEqual(media_api.file_uploads[0]["document_id"], "doc_created")
        self.assertEqual(media_api.file_uploads[0]["file_name"], "run_1.pptx")
        pptx_append = doc_api.appended[-1]["sections"][0]
        self.assertEqual(pptx_append["heading"], "PPTX 飞书附件")
        self.assertIn("File token: file_token_1", pptx_append["paragraphs"])
        saved_snapshot = integrator.session_document_service.saved[0]["section_snapshot"]
        self.assertEqual(saved_snapshot[-1]["heading"], "PPTX 飞书附件")

    def test_sync_delivery_manifest_imports_slides_to_cloud_doc_when_enabled(self) -> None:
        settings.feishu_artifact_slides_upload_enabled = True
        settings.feishu_artifact_slides_import_enabled = True
        pptx_path = Path("data") / "artifacts" / "slides" / "run_1.pptx"
        pptx_path.parent.mkdir(parents=True, exist_ok=True)
        pptx_path.write_bytes(b"pptx")
        doc_api = FakeDocAPI()
        media_api = FakeMediaAPI()
        import_api = FakeImportAPI()
        try:
            integrator = FeishuArtifactIntegrator(
                doc_api=doc_api,
                media_api=media_api,
                import_api=import_api,
                session_document_service=FakeSessionDocumentService(),
                requirement_service=FakeRequirementService(),
            )
            detail = SimpleNamespace(
                task_run_id="run_1",
                session_id="session_1",
                requirement_id="req_1",
                title="评审交付",
                session_documents=[],
            )

            result = integrator.sync_delivery_manifest(detail, _manifest())
        finally:
            pptx_path.unlink(missing_ok=True)

        self.assertEqual(result["status"], "ready")
        self.assertEqual(media_api.import_uploads[0]["file_extension"], "pptx")
        self.assertEqual(media_api.import_uploads[0]["obj_type"], "slides")
        self.assertEqual(import_api.created[0]["file_token"], "import_file_token_1")
        self.assertEqual(import_api.created[0]["import_type"], "slides")
        self.assertEqual(import_api.waited[0]["ticket"], "ticket_1")
        import_items = [item for item in result["media_items"] if item["kind"] == "slides_import"]
        self.assertEqual(import_items[0]["cloud_token"], "cloud_token_1")
        self.assertEqual(import_items[0]["cloud_url"], "https://feishu.example/cloud_slides")
        self.assertEqual(result["slides_sync"]["status"], "ready")
        self.assertTrue(any(item["kind"] == "slides_import" for item in result["slides_sync"]["items"]))
        ppt_sections = doc_api.appended[-1]["sections"]
        self.assertEqual(ppt_sections[-1]["heading"], "PPT 飞书云文档")
        saved_snapshot = integrator.session_document_service.saved[0]["section_snapshot"]
        self.assertEqual(saved_snapshot[-1]["heading"], "PPT 飞书云文档")

    def test_slides_cloud_import_can_run_without_docx_attachment_upload(self) -> None:
        settings.feishu_artifact_slides_upload_enabled = False
        settings.feishu_artifact_slides_import_enabled = True
        pptx_path = Path("data") / "artifacts" / "slides" / "run_1.pptx"
        pptx_path.parent.mkdir(parents=True, exist_ok=True)
        pptx_path.write_bytes(b"pptx")
        media_api = FakeMediaAPI()
        import_api = FakeImportAPI()
        try:
            integrator = FeishuArtifactIntegrator(
                doc_api=FakeDocAPI(),
                media_api=media_api,
                import_api=import_api,
                session_document_service=FakeSessionDocumentService(),
                requirement_service=FakeRequirementService(),
            )

            result = integrator.sync_delivery_manifest(
                SimpleNamespace(
                    task_run_id="run_1",
                    session_id="session_1",
                    requirement_id="req_1",
                    title="评审交付",
                    session_documents=[],
                ),
                _manifest(),
            )
        finally:
            pptx_path.unlink(missing_ok=True)

        self.assertEqual(result["status"], "ready")
        self.assertFalse(media_api.file_uploads)
        self.assertEqual(media_api.import_uploads[0]["file_name"], "run_1.pptx")
        self.assertEqual(result["slides_sync"]["media_items"][0]["kind"], "slides_import")

    def test_sync_delivery_manifest_keeps_doc_ready_when_slides_import_fails(self) -> None:
        settings.feishu_artifact_slides_upload_enabled = True
        settings.feishu_artifact_slides_import_enabled = True
        pptx_path = Path("data") / "artifacts" / "slides" / "run_1.pptx"
        pptx_path.parent.mkdir(parents=True, exist_ok=True)
        pptx_path.write_bytes(b"pptx")
        try:
            integrator = FeishuArtifactIntegrator(
                doc_api=FakeDocAPI(),
                media_api=FakeMediaAPI(),
                import_api=FakeImportAPI(fail=True),
                session_document_service=FakeSessionDocumentService(),
                requirement_service=FakeRequirementService(),
            )

            result = integrator.sync_delivery_manifest(
                SimpleNamespace(
                    task_run_id="run_1",
                    session_id="session_1",
                    requirement_id="req_1",
                    title="评审交付",
                    session_documents=[],
                ),
                _manifest(),
            )
        finally:
            pptx_path.unlink(missing_ok=True)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["document_id"], "doc_created")
        self.assertTrue(any("slides_import_failed" in warning for warning in result["warnings"]))
        self.assertTrue(any(item["kind"] == "slides_pptx" for item in result["slides_sync"]["items"]))
        failed_import_items = [item for item in result["media_items"] if item["kind"] == "slides_import"]
        self.assertEqual(failed_import_items[0]["status"], "failed")

    def test_sync_delivery_manifest_keeps_doc_ready_when_slides_upload_fails(self) -> None:
        settings.feishu_artifact_slides_upload_enabled = True
        pptx_path = Path("data") / "artifacts" / "slides" / "run_1.pptx"
        pptx_path.parent.mkdir(parents=True, exist_ok=True)
        pptx_path.write_bytes(b"pptx")
        try:
            integrator = FeishuArtifactIntegrator(
                doc_api=FakeDocAPI(),
                media_api=FakeMediaAPI(fail=True),
                session_document_service=FakeSessionDocumentService(),
                requirement_service=FakeRequirementService(),
            )

            result = integrator.sync_delivery_manifest(
                SimpleNamespace(
                    task_run_id="run_1",
                    session_id="session_1",
                    requirement_id="req_1",
                    title="评审交付",
                    session_documents=[],
                ),
                _manifest(),
            )
        finally:
            pptx_path.unlink(missing_ok=True)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["document_id"], "doc_created")
        self.assertTrue(any("slides_pptx_upload_failed" in warning for warning in result["warnings"]))

    def test_slides_upload_requires_pptx_link_label(self) -> None:
        settings.feishu_artifact_slides_upload_enabled = True
        html_path = Path("data") / "artifacts" / "slides" / "run_1.html"
        html_path.parent.mkdir(parents=True, exist_ok=True)
        html_path.write_text("<html></html>", encoding="utf-8")
        media_api = FakeMediaAPI()
        manifest = _manifest()
        slides = manifest["deliverables"][0]
        slides["links"] = [{"label": "HTML", "url": "/api/artifacts/slides/run_1.html"}]
        try:
            integrator = FeishuArtifactIntegrator(
                doc_api=FakeDocAPI(),
                media_api=media_api,
                session_document_service=FakeSessionDocumentService(),
                requirement_service=FakeRequirementService(),
            )

            result = integrator.sync_delivery_manifest(
                SimpleNamespace(
                    task_run_id="run_1",
                    session_id="session_1",
                    requirement_id="req_1",
                    title="评审交付",
                    session_documents=[],
                ),
                manifest,
            )
        finally:
            html_path.unlink(missing_ok=True)

        self.assertEqual(result["status"], "partial")
        self.assertFalse(media_api.file_uploads)
        self.assertIn("slides_pptx_file_not_found", result["warnings"])

    def test_sync_current_task_run_artifacts_replaces_stable_sections(self) -> None:
        doc_api = FakeDocAPI()
        session_documents = FakeSessionDocumentService()
        integrator = FeishuArtifactIntegrator(
            doc_api=doc_api,
            session_document_service=session_documents,
            requirement_service=FakeRequirementService(
                current_document={
                    "document_id": "doc_existing",
                    "url": "https://feishu.example/doc_existing",
                    "title": "当前协作文档",
                    "version": 5,
                    "section_snapshot": [{"heading": "保留区域", "paragraphs": ["保留内容"]}],
                    "section_block_index": [{"heading": "保留区域", "block_ids": ["old_block"]}],
                }
            ),
        )

        result = integrator.sync_current_task_run_artifacts(
            SimpleNamespace(
                task_run_id="run_1",
                session_id="session_1",
                requirement_id="req_1",
                title="工作台修订",
                artifacts=[
                    {
                        "artifact_id": "artifact_slides_1",
                        "artifact_type": "slides_package",
                        "title": "答辩 PPT",
                        "status": "ready",
                        "url": "/api/artifacts/slides/run_1.html",
                        "preview_json": '{"exports":{"html":"/api/artifacts/slides/run_1.html","pptx":"/api/artifacts/slides/run_1.pptx","pdf":"/api/artifacts/slides/run_1.pdf"}}',
                    },
                    {
                        "artifact_id": "artifact_canvas_1",
                        "artifact_type": "canvas",
                        "title": "流程画布",
                        "status": "ready",
                        "url": "/api/artifacts/canvas/run_1.html",
                        "preview_json": '{"exports":{"html":"/api/artifacts/canvas/run_1.html","svg":"/api/artifacts/canvas/run_1.svg","json":"/api/artifacts/canvas/run_1.json"}}',
                    },
                ],
            )
        )

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["document_id"], "doc_existing")
        self.assertEqual(len(doc_api.replaced), 1)
        self.assertFalse(doc_api.appended)
        self.assertEqual(doc_api.replaced[0]["target_headings"], ["当前 PPT", "当前 Canvas"])
        section_text = "\n".join(
            str(paragraph)
            for section in doc_api.replaced[0]["sections"]
            for paragraph in section.get("paragraphs", [])
        )
        self.assertIn("https://public.example/api/artifacts/slides/run_1.pptx", section_text)
        self.assertIn("https://public.example/api/artifacts/canvas/run_1.html", section_text)
        self.assertEqual(session_documents.saved[0]["sync_mode"], "current_artifacts_updated")
        self.assertEqual(session_documents.saved[0]["version"], 6)
        self.assertEqual(result["slides_sync"]["sync_mode"], "current_artifacts_updated")
        self.assertEqual(result["canvas_sync"]["sync_mode"], "current_artifacts_updated")

    def test_sync_current_task_run_artifacts_skips_without_target_document(self) -> None:
        integrator = FeishuArtifactIntegrator(
            doc_api=FakeDocAPI(),
            session_document_service=FakeSessionDocumentService(),
            requirement_service=FakeRequirementService(current_document=None),
        )

        result = integrator.sync_current_task_run_artifacts(
            SimpleNamespace(
                task_run_id="run_1",
                session_id="session_1",
                requirement_id="req_1",
                title="工作台修订",
                artifacts=[
                    {
                        "artifact_id": "artifact_canvas_1",
                        "artifact_type": "canvas",
                        "title": "流程画布",
                        "status": "ready",
                        "url": "/api/artifacts/canvas/run_1.html",
                        "preview_json": '{"exports":{"html":"/api/artifacts/canvas/run_1.html","svg":"/api/artifacts/canvas/run_1.svg","json":"/api/artifacts/canvas/run_1.json"}}',
                    }
                ],
            )
        )

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "current_document_missing")

    def test_sync_current_task_run_artifacts_embeds_canvas_image_into_current_section(self) -> None:
        settings.feishu_artifact_canvas_image_enabled = True
        svg_path = Path("data") / "artifacts" / "canvas" / "run_1.svg"
        png_path = Path("data") / "artifacts" / "canvas" / "run_1.png"
        svg_path.parent.mkdir(parents=True, exist_ok=True)
        svg_path.write_text("<svg></svg>", encoding="utf-8")
        png_path.write_bytes(b"png")
        doc_api = FakeDocAPI()
        media_api = FakeMediaAPI()
        try:
            integrator = FeishuArtifactIntegrator(
                doc_api=doc_api,
                media_api=media_api,
                session_document_service=FakeSessionDocumentService(),
                requirement_service=FakeRequirementService(
                    current_document={
                        "document_id": "doc_existing",
                        "url": "https://feishu.example/doc_existing",
                        "title": "当前协作文档",
                        "version": 2,
                    }
                ),
            )

            result = integrator.sync_current_task_run_artifacts(
                SimpleNamespace(
                    task_run_id="run_1",
                    session_id="session_1",
                    requirement_id="req_1",
                    title="画布修订",
                    artifacts=[
                        {
                            "artifact_id": "artifact_canvas_1",
                            "artifact_type": "canvas",
                            "title": "流程画布",
                            "status": "ready",
                            "url": "/api/artifacts/canvas/run_1.html",
                            "preview_json": '{"exports":{"html":"/api/artifacts/canvas/run_1.html","svg":"/api/artifacts/canvas/run_1.svg","json":"/api/artifacts/canvas/run_1.json"}}',
                        }
                    ],
                )
            )
        finally:
            svg_path.unlink(missing_ok=True)
            png_path.unlink(missing_ok=True)

        self.assertEqual(result["status"], "ready")
        self.assertEqual(media_api.uploads[0]["file_name"], "run_1.png")
        canvas_section = doc_api.replaced[0]["sections"][0]
        image_block = next(item for item in canvas_section["paragraphs"] if isinstance(item, dict))
        self.assertEqual(image_block["type"], "image")
        self.assertEqual(image_block["token"], "img_token_1")

    def test_sync_requirement_current_artifacts_uses_requirement_pointers(self) -> None:
        doc_api = FakeDocAPI()
        session_documents = FakeSessionDocumentService()
        integrator = FeishuArtifactIntegrator(
            doc_api=doc_api,
            session_document_service=session_documents,
            requirement_service=FakeRequirementService(),
        )

        result = integrator.sync_requirement_current_artifacts(
            SimpleNamespace(
                requirement_id="req_1",
                primary_session_id="session_1",
                title="Current Requirement",
                current_document={
                    "session_id": "session_1",
                    "document_id": "doc_manual",
                    "url": "https://feishu.example/doc_manual",
                    "title": "Manual Target Doc",
                    "version": 4,
                    "section_snapshot": [{"heading": "Keep", "paragraphs": ["Existing content"]}],
                    "section_block_index": [{"heading": "Keep", "block_ids": ["keep_1"]}],
                },
                current_slides={
                    "artifact_id": "artifact_slides_manual",
                    "artifact_type": "slides_package",
                    "title": "Manual Slides",
                    "status": "ready",
                    "url": "/api/artifacts/slides/run_1.html",
                    "preview_json": '{"exports":{"html":"/api/artifacts/slides/run_1.html","pptx":"/api/artifacts/slides/run_1.pptx"}}',
                },
                current_canvas={
                    "artifact_id": "artifact_canvas_manual",
                    "artifact_type": "canvas",
                    "title": "Manual Canvas",
                    "status": "ready",
                    "url": "/api/artifacts/canvas/run_1.html",
                    "preview_json": '{"exports":{"html":"/api/artifacts/canvas/run_1.html","svg":"/api/artifacts/canvas/run_1.svg","json":"/api/artifacts/canvas/run_1.json"}}',
                },
            )
        )

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["document_id"], "doc_manual")
        self.assertEqual(len(doc_api.replaced), 1)
        self.assertEqual(doc_api.replaced[0]["target_headings"], ["当前 PPT", "当前 Canvas"])
        self.assertEqual(session_documents.saved[0]["session_id"], "session_1")
        self.assertEqual(session_documents.saved[0]["version"], 5)
        self.assertEqual(result["slides_sync"]["sync_mode"], "current_artifacts_updated")
        self.assertEqual(result["canvas_sync"]["sync_mode"], "current_artifacts_updated")


def _manifest() -> dict:
    return {
        "title": "需求交付清单 - 评审交付",
        "task_run_id": "run_1",
        "session_id": "session_1",
        "requirement_id": "req_1",
        "summary": "已汇总当前需求下最新产物链接。",
        "deliverables": [
            {
                "key": "slides",
                "label": "答辩 PPT",
                "artifact_type": "slides_package",
                "title": "评审演示稿",
                "status": "ready",
                "url": "/api/artifacts/slides/run_1.html",
                "links": [
                    {"label": "PPTX", "url": "/api/artifacts/slides/run_1.pptx"},
                    {"label": "PDF", "url": "/api/artifacts/slides/run_1.pdf"},
                ],
            },
            {
                "key": "canvas",
                "label": "Canvas / 流程图",
                "artifact_type": "canvas",
                "title": "流程画布",
                "status": "ready",
                "url": "/api/artifacts/canvas/run_1.html",
                "links": [
                    {"label": "SVG", "url": "/api/artifacts/canvas/run_1.svg"},
                    {"label": "JSON", "url": "/api/artifacts/canvas/run_1.json"},
                ],
            },
        ],
        "checks": [
            {"key": "slides", "label": "PPT", "status": "ready", "detail": "已生成"},
            {"key": "canvas", "label": "Canvas", "status": "ready", "detail": "已生成"},
        ],
        "next_steps": ["把交付文档贴回 IM。"],
    }


if __name__ == "__main__":
    unittest.main()
