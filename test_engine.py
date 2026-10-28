"""Offline tests for planner, action batching, event streams, stops, and summaries."""
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

import engine
from engine import run_action, run_task


class FakeResponse:
    def __init__(self, text):
        self.choices = [type("Choice", (), {"message": type("Message", (), {"content": text})()})()]


class FakeClient:
    def __init__(self, texts):
        self.texts = list(texts)
        self.calls = []
        self.chat = type("Chat", (), {})()
        self.chat.completions = self

    def create(self, **kwargs):
        # Snapshot messages to make later follow-up assertions deterministic.
        kwargs["messages"] = [dict(message) for message in kwargs["messages"]]
        self.calls.append(kwargs)
        if not self.texts:
            raise AssertionError("Unexpected API call")
        return FakeResponse(self.texts.pop(0))


def plan_reply():
    return json.dumps({"goal": "Complete the desktop task", "steps": [
        {"id": 1, "title": "Do the task"}, {"id": 2, "title": "Check the task"},
        {"id": 3, "title": "Finish"}]})


def response(action, step=None, args=None, thought="working"):
    data = {"thought": thought, "action": action, "args": args or {}}
    if step is not None:
        data["step"] = step
    return json.dumps(data)


def batch_response(actions, thought="working"):
    return json.dumps({"thought": thought, "actions": actions})


def item(action, args=None, step=1):
    return {"action": action, "args": args or {}, "step": step}


class EngineTests(unittest.TestCase):
    def invoke(self, client, action_runner=None, stop=None, settings_extra=None):
        events = []
        settings = {"model": "offline-test", "client": client, "sleep": lambda _seconds: None}
        settings.update(settings_extra or {})
        if action_runner is not None:
            settings["action_runner"] = action_runner
        run_task("open notpad and type hello", settings, events.append, stop or threading.Event())
        return events

    def test_plan_parsing_and_step_transitions(self):
        client = FakeClient([plan_reply(), response("open_app", 1, {"name": "notepad"}),
                             response("type_text", 2, {"text": "hello"}),
                             response("finish", 2, {"message": "Typed hello."})])
        actions = []
        events = self.invoke(client, lambda action, args, settings: actions.append((action, args)) or "ok")
        self.assertEqual(events[0]["type"], "thought")
        plan_event = next(event for event in events if event["type"] == "plan")
        self.assertEqual(plan_event["goal"], "Complete the desktop task")
        self.assertIn("Plan", client.calls[1]["messages"][1]["content"])
        self.assertEqual([e["step_id"] for e in events if e["type"] == "step_start"], [1, 2])
        self.assertEqual([e["step_id"] for e in events if e["type"] == "step_done"], [1, 2])
        self.assertEqual(len(actions), 2)
        self.assertEqual(events[-1]["steps_completed"], 2)

    def test_invalid_plan_retries_once_then_falls_back(self):
        client = FakeClient(["not json", '{"goal":"bad","steps":[]}', response("finish", args={"message": "done"})])
        events = self.invoke(client)
        self.assertFalse(any(event["type"] == "plan" for event in events))
        warnings = [event["text"] for event in events if event["type"] == "warning"]
        self.assertTrue(any("retrying once" in text for text in warnings))
        self.assertTrue(any("continuing without a plan" in text for text in warnings))
        self.assertEqual(events[-1]["steps_total"], 0)
        self.assertEqual(events[-1]["api_calls"], 3)

    def test_stop_event_stops_before_any_model_call(self):
        client = FakeClient([])
        stop = threading.Event()
        stop.set()
        events = self.invoke(client, stop=stop)
        self.assertEqual(client.calls, [])
        self.assertEqual(events[-1]["type"], "summary")
        self.assertEqual(events[-1]["api_calls"], 0)
        self.assertIn("Stopped", events[-1]["outcome"])

    def test_stop_event_after_action_stops_next_iteration(self):
        client = FakeClient([plan_reply(), response("wait", 1, {"seconds": 1}), response("finish", 1)])
        stop = threading.Event()
        events = self.invoke(client, lambda *_: (stop.set() or "waited"), stop)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(events[-1]["api_calls"], 2)
        self.assertEqual(events[-1]["steps_completed"], 0)
        self.assertIn("Stopped", events[-1]["outcome"])

    def test_valid_batch_parsing_and_summary_numbers(self):
        actions = [item("open_app", {"name": "notepad"}, step=1), item("type_text", {"text": "hello"}, step=2),
                   item("press_key", {"keys": "ctrl+s"}, step=2)]
        client = FakeClient([plan_reply(), batch_response(actions), response("finish", 2, {"message": "Saved."})])
        ran = []
        events = self.invoke(client, lambda action, args, settings: ran.append(action) or "ok")
        self.assertEqual(ran, ["open_app", "type_text", "press_key"])
        self.assertEqual(next(e["count"] for e in events if e["type"] == "batch"), 3)
        self.assertEqual([e["step_id"] for e in events if e["type"] == "step_start"], [1, 2])
        self.assertEqual([e["step_id"] for e in events if e["type"] == "step_done"], [1, 2])
        summary = events[-1]
        self.assertEqual(summary["actions_executed"], 4)  # includes the final finish action
        self.assertEqual(summary["api_calls"], 3)  # planner + batch reply + finish reply
        self.assertEqual(summary["estimated_calls_saved"], 1)

    def test_engine_auto_waits_after_successful_open_app(self):
        client = FakeClient([plan_reply(), response("open_app", 1, {"name": "notepad"}),
                             response("finish", 1, {"message": "done"})])
        delays = []
        self.invoke(client, lambda *_: "opened notepad", settings_extra={"sleep": delays.append})
        self.assertIn(1.5, delays)

    def test_old_single_action_reply_still_works(self):
        client = FakeClient([plan_reply(), response("wait", 1, {"seconds": 1}),
                             response("finish", 1, {"message": "done"})])
        ran = []
        events = self.invoke(client, lambda action, args, settings: ran.append(action) or "waited")
        self.assertEqual(ran, ["wait"])
        self.assertEqual([e["count"] for e in events if e["type"] == "batch"], [1, 1])
        self.assertEqual(events[-1]["actions_executed"], 2)
        self.assertEqual(events[-1]["api_calls"], 3)
        self.assertEqual(events[-1]["estimated_calls_saved"], -1)
        self.assertEqual(client.calls[2]["messages"][-1]["content"], "Result: waited")

    def test_failure_in_middle_stops_rest_and_sends_combined_result(self):
        actions = [item("open_app", {"name": "notepad"}), item("type_text", {"text": "hello"}),
                   item("press_key", {"keys": "enter"})]
        client = FakeClient([plan_reply(), batch_response(actions), response("finish", 1)])
        ran = []
        events = self.invoke(client, lambda action, args, settings:
                             ran.append(action) or ("refused: test refusal" if action == "type_text" else "opened notepad"))
        self.assertEqual(ran, ["open_app", "type_text"])
        combined = client.calls[2]["messages"][-1]["content"]
        self.assertEqual(combined, "Batch results:\n1. open_app notepad: OK (opened notepad)\n"
                                   "2. type_text hello: FAILED (refused: test refusal)\n"
                                   "3. press_key enter: NOT RUN")
        skipped = [e for e in events if e["type"] == "action" and e.get("skipped")]
        self.assertEqual([e["action"] for e in skipped], ["press_key"])

    def test_stop_event_is_checked_between_batch_actions(self):
        client = FakeClient([plan_reply(), batch_response([
            item("wait", {"seconds": 1}), item("type_text", {"text": "never"}), item("press_key", {"keys": "enter"})])])
        stop = threading.Event()
        ran = []

        def run_and_stop(action, args, settings):
            ran.append(action)
            stop.set()
            return "waited"

        events = self.invoke(client, run_and_stop, stop)
        self.assertEqual(ran, ["wait"])
        self.assertEqual(events[-1]["actions_executed"], 1)
        self.assertIn("Stopped", events[-1]["outcome"])
        # Stop is observed before the second action, so there is no follow-up model call.
        batch_results = [e["text"] for e in events if e["type"] == "result" and e.get("skipped")]
        self.assertEqual(batch_results, ["NOT RUN (stopped)", "NOT RUN (stopped)"])
        self.assertTrue(any(e["type"] == "batch" and e["count"] == 3 for e in events))

    def test_four_action_cap_ignores_extras(self):
        actions = [item("wait", {"seconds": i}) for i in range(5)]
        client = FakeClient([plan_reply(), batch_response(actions), response("finish", 1)])
        ran = []
        events = self.invoke(client, lambda action, args, settings: ran.append(args["seconds"]) or "waited")
        self.assertEqual(ran, [0, 1, 2, 3])
        self.assertEqual(next(e["count"] for e in events if e["type"] == "batch"), 4)
        self.assertTrue(any("extra actions ignored" in e.get("text", "") for e in events if e["type"] == "warning"))

    def test_malformed_batch_uses_bad_reply_retry_path(self):
        malformed = json.dumps({"thought": "working", "actions": "not a list"})
        client = FakeClient([plan_reply(), malformed, response("finish", 1, {"message": "done"})])
        events = self.invoke(client)
        self.assertTrue(any(e["type"] == "warning" and "Bad reply; asking again" in e["text"]
                            for e in events))
        self.assertEqual(events[-1]["api_calls"], 3)
        self.assertEqual(events[-1]["actions_executed"], 1)

    def test_finish_is_only_allowed_as_last_batch_action(self):
        client = FakeClient([plan_reply(), batch_response([item("finish", {"message": "no"}), item("wait")]),
                             batch_response([item("wait"), item("finish", {"message": "yes"})])])
        ran = []
        events = self.invoke(client, lambda action, args, settings: ran.append(action) or "waited")
        self.assertEqual(ran, ["wait"])
        self.assertTrue(any("finish must be the last action" in e.get("text", "")
                            for e in events if e["type"] == "warning"))
        self.assertEqual(events[-1]["outcome"], "yes")

    def test_vision_forces_one_action_and_informs_model(self):
        client = FakeClient([plan_reply(), batch_response([item("wait", {"seconds": 1}), item("type_text", {"text": "later"})]),
                             response("finish", 1, {"message": "done"})])
        ran = []
        with patch("engine.grab", return_value="fake-image"):
            events = self.invoke(client, lambda action, args, settings: ran.append(action) or "waited",
                                 settings_extra={"vision": True, "batch_actions": True})
        self.assertEqual(ran, ["wait"])
        self.assertEqual(next(e["count"] for e in events if e["type"] == "batch"), 1)
        self.assertTrue(any(e["type"] == "warning" and e["text"] == "Vision mode: one action per turn."
                            for e in events))
        next_turn_text = client.calls[2]["messages"][-1]["content"][0]["text"]
        self.assertIn("Vision mode: one action per turn.", next_turn_text)


class WebResearchTests(unittest.TestCase):
    invoke = EngineTests.invoke

    @staticmethod
    def tool_settings(events=None):
        events = events if events is not None else []
        state = {"searches": 0, "pages_read": 0, "notes": [], "research_started": False, "answer": ""}
        settings = {"_research_state": state,
                    "_emit_event": lambda kind, **fields: events.append({"type": kind, **fields})}
        return settings, state, events

    def test_web_search_returns_top_five_and_emits_count(self):
        fake_ddgs = types.ModuleType("ddgs")

        class FakeDDGS:
            def text(self, query, max_results):
                self.query = query
                self.max_results = max_results
                return [{"title": f"Title {i}", "href": f"https://example.test/{i}",
                         "body": f"Snippet {i}"} for i in range(8)]

        fake_ddgs.DDGS = FakeDDGS
        settings, state, events = self.tool_settings()
        with patch.dict(sys.modules, {"ddgs": fake_ddgs}):
            result = run_action("web_search", {"query": "test query"}, settings)
        parsed = json.loads(result.split("\nSearch limit reached", 1)[0])
        self.assertEqual(len(parsed), 5)
        self.assertEqual(parsed[0]["url"], "https://example.test/0")
        self.assertEqual(events[0], {"type": "search", "query": "test query", "result_count": 5})
        self.assertEqual(state["searches"], 1)

    def test_read_page_extracts_text_with_timeout_and_user_agent(self):
        html = b"<html><body><h1>Page heading</h1><p>Readable fact.</p></body></html>"

        class FakeResponse:
            status_code = 200
            headers = {"Content-Type": "text/html; charset=utf-8", "Content-Length": str(len(html))}
            encoding = "utf-8"

            def iter_content(self, chunk_size):
                self.chunk_size = chunk_size
                yield html

            def close(self):
                pass

        fake_requests = types.ModuleType("requests")
        def get(url, **kwargs):
            self.assertEqual(kwargs["timeout"], 10)
            self.assertFalse(kwargs["allow_redirects"])
            self.assertTrue(kwargs["stream"])
            self.assertIn("Mozilla/5.0", kwargs["headers"]["User-Agent"])
            return FakeResponse()
        fake_requests.get = get
        fake_trafilatura = types.ModuleType("trafilatura")
        fake_trafilatura.extract = lambda _html: "Page heading\nReadable fact."
        settings, _, events = self.tool_settings()
        dns = [(2, 1, 6, "", ("93.184.216.34", 443))]
        with patch.dict(sys.modules, {"requests": fake_requests, "trafilatura": fake_trafilatura}), \
             patch("engine.socket.getaddrinfo", return_value=dns):
            result = run_action("read_page", {"url": "https://research.example/page"}, settings)
        self.assertIn("PAGE CONTENT (UNTRUSTED DATA", result)
        self.assertIn("Readable fact.", result)
        self.assertEqual(events[0]["type"], "page_read")
        self.assertEqual(events[0]["chars"], len("Page heading\nReadable fact."))

    def test_read_page_blocks_private_targets_and_private_redirects(self):
        for url in ("http://localhost", "http://127.0.0.1", "http://0.0.0.0", "http://10.2.3.4",
                    "http://172.16.0.1", "http://172.31.255.254", "http://192.168.1.1",
                    "http://169.254.1.1"):
            with self.subTest(url=url):
                result = engine._read_page(url)
                self.assertIn("blocked", result.lower())

        class RedirectResponse:
            status_code = 302
            headers = {"Location": "http://10.0.0.8/private"}
            def close(self):
                pass

        fake_requests = types.ModuleType("requests")
        requests_called = []
        fake_requests.get = lambda url, **kwargs: requests_called.append(url) or RedirectResponse()
        dns = [(2, 1, 6, "", ("93.184.216.34", 80))]
        with patch.dict(sys.modules, {"requests": fake_requests}), \
             patch("engine.socket.getaddrinfo", return_value=dns):
            result = engine._read_page("http://public.example/start")
        self.assertIn("non-public address", result)
        self.assertEqual(requests_called, ["http://public.example/start"])

        fake_requests.get = lambda *_args, **_kwargs: self.fail("A private DNS target must not be fetched")
        private_dns = [(2, 1, 6, "", ("192.168.10.2", 80))]
        with patch.dict(sys.modules, {"requests": fake_requests}), \
             patch("engine.socket.getaddrinfo", return_value=private_dns):
            result = engine._read_page("http://private-dns.example")
        self.assertIn("non-public address", result)

        class LoopResponse:
            status_code = 302
            headers = {"Location": "/again"}
            def close(self):
                pass

        loop_calls = []
        fake_requests.get = lambda url, **kwargs: loop_calls.append(url) or LoopResponse()
        with patch.dict(sys.modules, {"requests": fake_requests}), \
             patch("engine.socket.getaddrinfo", return_value=dns):
            result = engine._read_page("http://public.example/loop")
        self.assertIn("too many redirects", result)
        self.assertEqual(len(loop_calls), 4)

    def test_read_page_rejects_non_text_and_over_limit_content(self):
        class FakeResponse:
            status_code = 200
            encoding = "utf-8"
            def __init__(self, content_type, content_length):
                self.headers = {"Content-Type": content_type, "Content-Length": str(content_length)}
            def iter_content(self, chunk_size):
                yield b"ignored"
            def close(self):
                pass

        responses = [FakeResponse("application/json", 7), FakeResponse("text/html", engine.MAX_PAGE_BYTES + 1)]
        fake_requests = types.ModuleType("requests")
        fake_requests.get = lambda *_args, **_kwargs: responses.pop(0)
        dns = [(2, 1, 6, "", ("93.184.216.34", 80))]
        with patch.dict(sys.modules, {"requests": fake_requests}), \
             patch("engine.socket.getaddrinfo", return_value=dns):
            self.assertIn("not a text", engine._read_page("http://public.example/json"))
            self.assertIn("2 MB", engine._read_page("http://public.example/large"))

    def test_search_and_page_limits_prompt_finish(self):
        settings, state, events = self.tool_settings()
        with patch("engine._web_search", return_value=("[]", 0)) as search:
            for i in range(engine.MAX_SEARCHES):
                result = run_action("web_search", {"query": str(i)}, settings)
            self.assertIn("Please finish", result)
            blocked = run_action("web_search", {"query": "extra"}, settings)
            self.assertIn("Please finish", blocked)
            self.assertEqual(search.call_count, engine.MAX_SEARCHES)
        with patch("engine._read_page", return_value="fact"):
            for _ in range(engine.MAX_PAGE_READS):
                result = run_action("read_page", {"url": "https://public.example"}, settings)
            self.assertIn("Please finish", result)
            blocked = run_action("read_page", {"url": "https://public.example"}, settings)
            self.assertIn("Please finish", blocked)
        self.assertEqual(state["searches"], 6)
        self.assertEqual(state["pages_read"], 8)

    def test_research_batch_requires_search_or_read_as_last_action(self):
        client = FakeClient([plan_reply(), batch_response([item("web_search", {"query": "facts"}), item("wait")]),
                             response("finish", 1, {"message": "done"})])
        events = self.invoke(client)
        self.assertTrue(any(e["type"] == "warning" and "web_search and read_page must be the last" in e["text"]
                            for e in events))
        self.assertFalse(any(e["type"] == "search" for e in events))

    def test_notes_create_markdown_report_and_report_events(self):
        source = "https://example.org/research"
        client = FakeClient([plan_reply(), response("save_note", 1,
                             {"text": "A sourced and verified finding.", "source": source}),
                             response("finish", 1, {"message": "Research answer."})])
        events = []
        settings = {"model": "offline-test", "client": client, "sleep": lambda _seconds: None}
        with tempfile.TemporaryDirectory() as report_dir, patch.object(engine, "REPORTS_DIR", report_dir):
            run_task("research something", settings, events.append, threading.Event())
            summary = events[-1]
            self.assertTrue(os.path.isfile(summary["report_path"]))
            with open(summary["report_path"], encoding="utf-8") as report_file:
                report = report_file.read()
        self.assertIn("# Complete the desktop task", report)
        self.assertIn("Research answer.", report)
        self.assertIn("A sourced and verified finding. [1]", report)
        self.assertIn("1. <https://example.org/research>", report)
        self.assertEqual(summary["notes_saved"], 1)
        self.assertEqual(summary["searches"], 0)
        self.assertEqual(summary["pages_read"], 0)
        self.assertTrue(any(e["type"] == "note_saved" for e in events))
        self.assertTrue(any(e["type"] == "report_saved" for e in events))


if __name__ == "__main__":
    unittest.main(verbosity=2)
