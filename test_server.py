"""Offline tests for the local server; every engine action/model call is fake."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import server


PORT = 8765


class FakeEngine:
    MODEL_PRESETS = ("auto", "auto:fast", "auto:smart", "auto:agent-fast", "auto:agent-vision")

    def __init__(self, root):
        self.APP_DIR = str(root)
        self.REPORTS_DIR = str(root / "reports")
        self.SCREENSHOTS_DIR = str(root / "screenshots")
        self.SKILLS_DIR = str(root / "skills")
        self.started = threading.Event()
        self.ask_seen = threading.Event()
        self.answer_seen = threading.Event()
        self.answer = None
        self.model_fetches = []
        self.skills = {}

    @staticmethod
    def migrate_model_config(config):
        text = str(config.get("text_model") or config.get("model") or "auto")
        return text, str(config.get("vision_model") or text)

    @staticmethod
    def migrate_vision_mode(config):
        value = config.get("vision_mode")
        if value in {"off", "auto", "on"}:
            return value
        return "on" if config.get("vision") else "off"

    def fetch_model_options(self, base, api_key=""):
        self.model_fetches.append((base, api_key))
        return self.MODEL_PRESETS + ("fake-model",), ""

    def run_task(self, task, settings, emit, stop_event):
        self.started.set()
        if task == "ask test":
            self.ask_seen.set()
            self.answer = emit({"type": "ask", "kind": "question", "text": "Proceed?",
                                "options": ["yes", "no"]})
            self.answer_seen.set()
        else:
            emit({"type": "plan", "time": "2026-01-01T00:00:00Z", "goal": task,
                  "steps": [{"id": 1, "title": "Fake step"}]})
            while not stop_event.wait(0.01):
                time.sleep(0.01)
        emit({"type": "summary", "goal": task, "steps_completed": 1,
              "steps_total": 1, "outcome": "fake done"})

    def list_skills(self, _skills_dir=None):
        return list(self.skills.values())

    def load_skill(self, name, _skills_dir=None):
        if name not in self.skills:
            raise FileNotFoundError(name)
        return self.skills[name]

    def delete_skill(self, name, _skills_dir=None):
        return self.skills.pop(name, None) is not None

    def save_skill(self, skill, name, _skills_dir=None, overwrite=False):
        if name in self.skills and not overwrite:
            raise FileExistsError(name)
        value = dict(skill, name=name)
        self.skills[name] = value
        return value, False

    def extract_skill(self, candidate, settings):
        return {"description": "fake skill", "steps": ["fake step"], "variables": [],
                "trace": [], "hints": []}

    def improve_prompt(self, text, settings):
        return {"improved": text, "changes": "unchanged fake"}

    def open_report_in_notepad(self, path):
        return ""


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.web = self.root / "web"
        self.web.mkdir()
        (self.web / "index.html").write_text("<h1>fake</h1>", encoding="utf-8")
        (self.web / "app.js").write_text("// test", encoding="utf-8")
        self.screens = self.root / "screenshots"
        self.screens.mkdir()
        (self.screens / "screen.jpg").write_bytes(b"fake-image")
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"base": "https://models.example/v1", "key": "secret-key-1234",
                                           "model": "legacy-model", "browser": "chrome",
                                           "vision_mode": "auto", "batch_actions": True}), encoding="utf-8")
        self.fake = FakeEngine(self.root)
        self.app = server.create_app(port=PORT, config_path=self.config, engine_module=self.fake,
                                     web_dir=self.web, screenshots_dir=self.screens,
                                     workspace_dir=self.root, window_mode="browser")
        self.client_context = TestClient(self.app)
        self.client = self.client_context.__enter__()
        self.token = self.app.state.session_token

    def tearDown(self):
        if self.app.state.running:
            headers = self.headers()
            self.client.post("/api/stop", headers=headers, json={})
            deadline = time.monotonic() + 2
            while self.app.state.running and time.monotonic() < deadline:
                time.sleep(0.01)
        self.client_context.__exit__(None, None, None)
        self.temp.cleanup()

    def headers(self, **extra):
        return {"Host": f"127.0.0.1:{PORT}", "X-Token": self.token, **extra}

    def wait_until(self, predicate, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return bool(predicate())

    def test_auth_host_origin_and_health(self):
        response = self.client.get("/api/health", headers={"Host": f"127.0.0.1:{PORT}"})
        self.assertEqual(response.status_code, 401)
        response = self.client.get("/api/health", headers={"Host": "attacker.example"})
        self.assertEqual(response.status_code, 403)
        response = self.client.get("/api/health", headers=self.headers())
        self.assertEqual(response.status_code, 200)
        health = response.json()
        self.assertGreaterEqual(health["uptime_seconds"], 0)
        self.assertTrue(health["python_version"])
        self.assertEqual(health["window_mode"], "browser")
        self.assertTrue(health["settings_loaded"])
        response = self.client.post("/api/stop", headers=self.headers(Origin="http://evil.example"), json={})
        self.assertEqual(response.status_code, 403)

    def test_websocket_token_host_and_origin(self):
        with self.assertRaises(WebSocketDisconnect) as missing:
            with self.client.websocket_connect("/ws", headers={"Host": f"127.0.0.1:{PORT}"}):
                pass
        self.assertEqual(missing.exception.code, 4401)
        with self.assertRaises(WebSocketDisconnect) as wrong_host:
            with self.client.websocket_connect("/ws?t=" + self.token,
                                               headers={"Host": "evil.example"}):
                pass
        self.assertEqual(wrong_host.exception.code, 4403)
        with self.assertRaises(WebSocketDisconnect) as wrong_origin:
            with self.client.websocket_connect("/ws?t=" + self.token,
                                               headers={"Host": f"127.0.0.1:{PORT}",
                                                        "Origin": "http://evil.example"}):
                pass
        self.assertEqual(wrong_origin.exception.code, 4403)
        with self.client.websocket_connect("/ws?t=" + self.token,
                                           headers={"Host": f"127.0.0.1:{PORT}",
                                                    "Origin": f"http://127.0.0.1:{PORT}"}) as socket:
            event = socket.receive_json()
            self.assertEqual(event["type"], "connected")
            self.assertEqual(event["replayed"], 0)
            self.assertEqual(event["seq"], 1)

    def test_run_broadcasts_replays_history_rejects_second_run_and_stops(self):
        headers = self.headers(Origin=f"http://127.0.0.1:{PORT}")
        with self.client.websocket_connect("/ws?t=" + self.token,
                                           headers={"Host": f"127.0.0.1:{PORT}"}) as live_socket:
            self.assertEqual(live_socket.receive_json()["type"], "connected")
            response = self.client.post("/api/run", headers=headers, json={"task": "fake task"})
            self.assertEqual(response.status_code, 202)
            self.assertTrue(self.fake.started.wait(1))
            live_events = [live_socket.receive_json(), live_socket.receive_json()]
            self.assertEqual([event["type"] for event in live_events], ["run_started", "plan"])
            second = self.client.post("/api/run", headers=headers, json={"task": "second"})
            self.assertEqual(second.status_code, 409)
        with self.client.websocket_connect("/ws?t=" + self.token,
                                           headers={"Host": f"127.0.0.1:{PORT}"}) as replay_socket:
            replayed = [replay_socket.receive_json() for _ in range(4)]
        self.assertEqual([event["type"] for event in replayed],
                         ["connected", "run_started", "plan", "connected"])
        self.assertEqual([event["seq"] for event in replayed], sorted(event["seq"] for event in replayed))
        self.assertEqual(self.client.post("/api/stop", headers=headers, json={}).status_code, 200)
        self.assertTrue(self.wait_until(lambda: not self.app.state.running))
        self.assertEqual(self.client.get("/api/status", headers=self.headers()).json()["running"], False)
        types = [item["type"] for item in self.app.state.event_history]
        self.assertIn("summary", types)
        self.assertIn("run_finished", types)

    def test_answer_unblocks_waiting_ask(self):
        headers = self.headers(Origin=f"http://127.0.0.1:{PORT}")
        self.assertEqual(self.client.post("/api/run", headers=headers,
                                          json={"task": "ask test"}).status_code, 202)
        self.assertTrue(self.fake.ask_seen.wait(1))
        self.assertTrue(self.wait_until(lambda: bool(self.app.state.pending_asks)))
        ask_id = next(iter(self.app.state.pending_asks))
        ask_events = [item for item in self.app.state.event_history if item["type"] == "ask"]
        self.assertEqual(ask_events[-1]["id"], ask_id)
        answer = self.client.post("/api/answer", headers=headers,
                                  json={"id": ask_id, "answer": "yes"})
        self.assertEqual(answer.status_code, 200)
        self.assertTrue(self.fake.answer_seen.wait(1))
        self.assertEqual(self.fake.answer, "yes")
        self.assertTrue(self.wait_until(lambda: not self.app.state.running))

    def test_settings_mask_keep_existing_and_save(self):
        response = self.client.get("/api/settings", headers=self.headers())
        self.assertEqual(response.status_code, 200)
        original = response.json()
        self.assertEqual(original["text_model"], "legacy-model")
        self.assertEqual(original["vision_model"], "legacy-model")
        self.assertTrue(original["has_key"])
        self.assertNotIn("secret-key-1234", response.text)
        self.assertTrue(original["key"].endswith("1234"))
        response = self.client.post("/api/settings", headers=self.headers(), json={
            "base": "https://new.example/v1", "key": original["key"],
            "text_model": "text-v2", "vision_model": "vision-v2", "vision_mode": "on",
            "browser": "brave", "batch_actions": False, "open_report_notepad": False})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["text_model"], "text-v2")
        self.assertEqual(response.json()["vision_model"], "vision-v2")
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(saved["key"], "secret-key-1234")
        self.assertEqual(saved["text_model"], "text-v2")
        self.assertEqual(saved["vision_mode"], "on")
        self.assertFalse(saved["batch_actions"])
        response = self.client.post("/api/settings", headers=self.headers(), json={"key": ""})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8"))["key"], "secret-key-1234")

    def test_stop_cancels_waiting_ask(self):
        headers = self.headers(Origin=f"http://127.0.0.1:{PORT}")
        self.assertEqual(self.client.post("/api/run", headers=headers,
                                          json={"task": "ask test"}).status_code, 202)
        self.assertTrue(self.fake.ask_seen.wait(1))
        self.assertTrue(self.wait_until(lambda: bool(self.app.state.pending_asks)))
        self.assertEqual(self.client.post("/api/stop", headers=headers, json={}).status_code, 200)
        self.assertTrue(self.fake.answer_seen.wait(1))
        self.assertEqual(self.fake.answer, {"cancelled": True})
        self.assertTrue(self.wait_until(lambda: not self.app.state.running))

    def test_models_reuse_engine_model_list_logic(self):
        response = self.client.get("/api/models", headers=self.headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["models"][-1], "fake-model")
        self.assertEqual(self.fake.model_fetches[-1], ("https://models.example/v1", "secret-key-1234"))

    def test_screenshot_and_static_path_traversal_are_rejected(self):
        url = "/api/screenshot?path=screen.jpg&t=" + self.token
        self.assertEqual(self.client.get(url, headers={"Host": f"127.0.0.1:{PORT}"}).status_code, 200)
        traversal = "/api/screenshot?path=..%2Fconfig.json&t=" + self.token
        self.assertEqual(self.client.get(traversal, headers={"Host": f"127.0.0.1:{PORT}"}).status_code, 403)
        static = self.client.get("/..%2Fconfig.json", headers={"Host": f"127.0.0.1:{PORT}"})
        self.assertIn(static.status_code, (403, 404))
        self.assertEqual(self.client.get("/", headers={"Host": f"127.0.0.1:{PORT}"}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
