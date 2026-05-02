import unittest
from types import SimpleNamespace

from app.services.task_context_pack import TaskContextPackBuilder


class TaskContextPackBuilderTests(unittest.TestCase):
    def test_builds_used_sources_and_missing_items(self) -> None:
        detail = SimpleNamespace(
            source_type="im",
            source_ref="m1",
            trigger_message_id="m1",
            steps=[
                SimpleNamespace(step_key="request_route", status="done"),
                SimpleNamespace(step_key="generate_doc", status="done"),
            ],
            artifacts=[
                SimpleNamespace(
                    artifact_id="artifact_doc",
                    artifact_type="document",
                    title="需求文档",
                    provider="feishu",
                    status="ready",
                    url="https://feishu.example/doc",
                    version=2,
                )
            ],
            session_documents=[
                SimpleNamespace(
                    title="需求文档",
                    version=2,
                    sync_mode="updated",
                    is_current=True,
                    url="https://feishu.example/doc",
                )
            ],
            confirmations=[],
        )

        pack = TaskContextPackBuilder().build_for_task_run(detail)

        self.assertIn("已汇总", pack["summary"])
        self.assertTrue(any(item["kind"] == "im" for item in pack["used_sources"]))
        self.assertTrue(any(item["kind"] == "document" for item in pack["used_sources"]))
        missing_kinds = {item["kind"] for item in pack["missing_items"]}
        self.assertIn("canvas", missing_kinds)
        self.assertIn("slides", missing_kinds)
        self.assertTrue(any("白板" in item for item in pack["suggested_inputs"]))

    def test_complete_context_suggests_delivery_or_rehearsal(self) -> None:
        detail = SimpleNamespace(
            source_type="im",
            source_ref="m1",
            trigger_message_id="m1",
            steps=[SimpleNamespace(step_key="plan", status="done")],
            artifacts=[
                SimpleNamespace(artifact_type="document", title="文档", provider="feishu", status="ready", url="https://doc", version=1),
                SimpleNamespace(artifact_type="canvas", title="流程图", provider="local", status="ready", url="/canvas.html", version=1),
                SimpleNamespace(artifact_type="slides_package", title="PPT", provider="local", status="ready", url="/slides.html", version=1),
            ],
            session_documents=[
                SimpleNamespace(title="文档", version=1, sync_mode="created", is_current=True, url="https://doc")
            ],
            confirmations=[],
        )

        pack = TaskContextPackBuilder().build_for_task_run(detail)

        self.assertEqual(pack["missing_items"], [])
        self.assertEqual(pack["suggested_inputs"], ["当前上下文较完整，可以进入排练、修订或交付归档。"])


if __name__ == "__main__":
    unittest.main()
