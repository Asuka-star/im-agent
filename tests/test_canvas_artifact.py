import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.services.canvas_artifact_service import CanvasArtifactService
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
            self.assertEqual(artifact["url"], "/api/artifacts/canvas/run_canvas.json")
            self.assertEqual(artifact["export_url"], "/api/artifacts/canvas/run_canvas.svg")
            self.assertTrue((Path(tmpdir) / "run_canvas.json").is_file())
            self.assertTrue((Path(tmpdir) / "run_canvas.svg").is_file())
            self.assertEqual(artifact["preview"]["exports"]["svg"], "/api/artifacts/canvas/run_canvas.svg")
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


if __name__ == "__main__":
    unittest.main()
