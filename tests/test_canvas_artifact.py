import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.services.canvas_artifact_service import CanvasArtifactService
from app.services.tools.canvas_tool import CanvasTool
from app.services.feishu_workflow import FeishuWorkflowService


class CanvasArtifactTests(unittest.TestCase):
    def test_canvas_service_persists_flow_scene(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = CanvasArtifactService(root_dir=Path(tmpdir))
            artifact = service.generate_flow(
                title="Architecture",
                instruction="login, planner, delivery",
                llm_result={},
                workspace_context="",
                task_run_id="run_canvas",
                session_id="s1",
            )

            self.assertEqual(artifact["artifact_type"], "canvas")
            self.assertEqual(artifact["url"], "/api/artifacts/canvas/run_canvas.html")
            self.assertEqual(artifact["export_url"], "/api/artifacts/canvas/run_canvas.svg")
            self.assertTrue((Path(tmpdir) / "run_canvas.json").is_file())
            self.assertTrue((Path(tmpdir) / "run_canvas.svg").is_file())
            self.assertTrue((Path(tmpdir) / "run_canvas.html").is_file())
            self.assertEqual(artifact["preview"]["exports"]["json"], "/api/artifacts/canvas/run_canvas.json")
            self.assertEqual(artifact["preview"]["exports"]["svg"], "/api/artifacts/canvas/run_canvas.svg")
            self.assertEqual(artifact["preview"]["exports"]["html"], "/api/artifacts/canvas/run_canvas.html")
            self.assertTrue(artifact["preview"]["shapes"])
            first_node = next(shape for shape in artifact["preview"]["shapes"] if shape["type"] == "node")
            self.assertEqual(first_node["color"], "#EAF5FF")
            self.assertEqual(first_node["stroke"], "#5A9FD6")
            self.assertEqual(first_node["group"], "Input")
            nodes = [shape for shape in artifact["preview"]["shapes"] if shape["type"] == "node"]
            self.assertEqual(nodes[-1]["group"], "Artifact")
            svg = (Path(tmpdir) / "run_canvas.svg").read_text(encoding="utf-8")
            self.assertIn("<svg", svg)
            self.assertIn("login", svg)
            html = (Path(tmpdir) / "run_canvas.html").read_text(encoding="utf-8")
            self.assertIn("自由画布预览", html)
            self.assertNotIn("鑷", html)
            self.assertIn("<svg", html)

    def test_canvas_service_derives_flow_nodes_from_discussion_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = CanvasArtifactService(root_dir=Path(tmpdir))
            context = (
                "[\u534f\u4f5c\u4e0a\u4e0b\u6587]\n"
                "[\u8fd1\u671f\u7fa4\u804a\u8ba8\u8bba]\n"
                "- \u53d1\u8a00\u4eba Zeleous | \u5185\u5bb9: "
                "\u5f53\u524d\u7aef\u4e0eUI\u8bbe\u8ba1\u5b8c\u6210\u540e\uff0c"
                "\u518d\u7b49\u540e\u7aef\u5b8c\u6210\uff0c"
                "\u90a3\u4e48\u6211\u4eec\u7684\u9879\u76ee\u5c31\u53ef\u4ee5\u4e0a\u7ebf\u4e86"
            )
            artifact = service.generate_flow(
                title="\u6d41\u7a0b\u753b\u5e03",
                instruction="\u6839\u636e\u8ba8\u8bba\u6765\u751f\u6210\u4e00\u4e2a\u6d41\u7a0b\u753b\u5e03",
                llm_result={},
                workspace_context=context,
                task_run_id="run_canvas",
                session_id="s1",
            )

        labels = [shape["text"] for shape in artifact["preview"]["shapes"] if shape["type"] == "node"]
        self.assertEqual(
            labels,
            [
                "\u524d\u7aef\u4e0e UI \u8bbe\u8ba1\u5b8c\u6210",
                "\u540e\u7aef\u5b8c\u6210",
                "\u9879\u76ee\u4e0a\u7ebf",
            ],
        )
        self.assertNotIn("\u534f\u4f5c\u4e0a\u4e0b\u6587", " ".join(labels))
        self.assertNotIn("\u53d1\u8a00\u4eba", " ".join(labels))

    def test_canvas_tool_formats_public_preview_url(self) -> None:
        tool = CanvasTool(artifact_service=CanvasArtifactService())
        reply = tool.format_reply(
            {
                "title": "Canvas",
                "url": "/api/artifacts/canvas/run_canvas.html",
                "preview": {"shapes": [{"type": "node"}]},
            }
        )

        self.assertIn("http://science.topviewclub.cn/api/artifacts/canvas/run_canvas.html", reply)

    def test_canvas_service_normalizes_shape_style_and_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = CanvasArtifactService(root_dir=Path(tmpdir))
            artifact = service.generate_flow(
                title="Architecture",
                instruction="draw architecture",
                llm_result={
                    "canvas": {
                        "shapes": [
                            {
                                "id": "n1",
                                "type": "node",
                                "text": "Start",
                                "color": "#ffeeaa",
                                "stroke": "#112233",
                                "group": "Intake",
                            },
                            {
                                "id": "a1",
                                "type": "arrow",
                                "from": "n1",
                                "to": "n2",
                                "color": "#334455",
                            },
                        ]
                    }
                },
                workspace_context="",
                task_run_id="run_canvas",
                session_id="s1",
            )

        node = artifact["preview"]["shapes"][0]
        arrow = artifact["preview"]["shapes"][1]
        self.assertEqual(node["color"], "#FFEEAA")
        self.assertEqual(node["stroke"], "#112233")
        self.assertEqual(node["group"], "Intake")
        self.assertEqual(arrow["color"], "#334455")

    def test_canvas_service_tolerates_non_numeric_shape_coordinates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = CanvasArtifactService(root_dir=Path(tmpdir))
            artifact = service.generate_flow(
                title="Architecture",
                instruction="draw architecture",
                llm_result={
                    "canvas": {
                        "shapes": [
                            {"id": "n1", "type": "node", "text": "Start", "x": "left", "y": "top"}
                        ]
                    }
                },
                workspace_context="",
                task_run_id="run_canvas",
                session_id="s1",
            )

        shape = artifact["preview"]["shapes"][0]
        self.assertEqual(shape["x"], 80)
        self.assertEqual(shape["y"], 140)

    def test_workflow_canvas_protocol_executes_canvas_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workflow = FeishuWorkflowService()
            workflow.canvas_artifact_service = CanvasArtifactService(root_dir=Path(tmpdir))
            message = SimpleNamespace(
                session_id="s1",
                message_id="m1",
                text="draw an architecture diagram",
                chat_id=None,
            )

            result = workflow._execute_llm_request(
                message,
                {
                    "operation": "create",
                    "object": "canvas",
                    "route": "canvas",
                    "canvas": {"title": "Architecture", "nodes": ["IM", "Planner", "Artifact"]},
                },
                workspace_context="",
                active_episode_id=None,
                task_run_id=None,
            )

        artifact = next(item for item in result["artifacts"] if item["artifact_type"] == "canvas")
        self.assertEqual(result["mode"], "canvas")
        self.assertEqual(artifact["title"], "Architecture")
        self.assertEqual(artifact["preview"]["schema"], "im-agent.canvas.v1")

    def test_workflow_fallback_canvas_request_creates_canvas_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workflow = FeishuWorkflowService()
            workflow.canvas_artifact_service = CanvasArtifactService(root_dir=Path(tmpdir))
            message = SimpleNamespace(
                session_id="s1",
                message_id="m1",
                text="帮我画一个流程图",
                chat_id=None,
                chat_type="group",
            )

            result = workflow._handle_fallback_request(
                message,
                active_episode_id=None,
                task_run_id=None,
            )

        artifact = next(item for item in result["artifacts"] if item["artifact_type"] == "canvas")
        self.assertEqual(result["mode"], "canvas")
        self.assertEqual(artifact["preview"]["schema"], "im-agent.canvas.v1")


if __name__ == "__main__":
    unittest.main()
