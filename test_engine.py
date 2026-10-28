"""Offline tests for planner, action batching, event streams, stops, and summaries."""
import base64
import json
import os
import re
import sys
import tempfile
import threading
import types
import unittest
from datetime import datetime
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
        result = self.texts.pop(0)
        if isinstance(result, Exception):
            raise result
        return FakeResponse(result)


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
        self.assertTrue(any("finish" in e.get("text", "") and "must be last" in e.get("text", "")
                            for e in events if e["type"] == "warning"))
        self.assertEqual(events[-1]["outcome"], "yes")

    def test_planner_vision_fields_and_config_migration(self):
        legacy = json.loads(plan_reply())
        parsed = engine.parse_plan(json.dumps(legacy))
        self.assertFalse(parsed["needs_vision"])
        self.assertFalse(parsed["screen_first"])
        explicit = dict(legacy, needs_vision=True, screen_first=True, reason="A dialog is open.")
        parsed = engine.parse_plan(json.dumps(explicit))
        self.assertTrue(parsed["needs_vision"])
        self.assertTrue(parsed["screen_first"])
        self.assertEqual(parsed["reason"], "A dialog is open.")
        self.assertEqual(engine.migrate_vision_mode({"vision": True}), "on")
        self.assertEqual(engine.migrate_vision_mode({"vision": False}), "auto")
        self.assertEqual(engine.migrate_vision_mode({}), "auto")
        self.assertEqual(engine.migrate_vision_mode({"vision": False, "vision_mode": "off"}), "off")

    @staticmethod
    def _vision_test_patches():
        from unittest.mock import patch
        return (patch("engine.grab", return_value=base64.b64encode(b"grid-image").decode("ascii")),
                patch("engine.create_screenshot_task_dir", return_value="/fake/task"),
                patch("engine.save_screenshot", side_effect=lambda _image, _folder, step:
                      f"/fake/task/step-{step:02d}.jpg"))

    def test_vision_off_never_screenshots_or_escalates(self):
        first_plan = json.loads(plan_reply())
        first_plan.update(needs_vision=True, screen_first=True, reason="Need a button.")
        client = FakeClient([json.dumps(first_plan), response("finish", 1, {"message": "done"})])
        patches = self._vision_test_patches()
        with patches[0] as grab_mock, patches[1] as dir_mock, patches[2] as save_mock:
            events = self.invoke(client, settings_extra={"vision_mode": "off"})
        grab_mock.assert_not_called()
        dir_mock.assert_not_called()
        save_mock.assert_not_called()
        self.assertFalse(any("image_url" in str(call["messages"]) for call in client.calls))
        self.assertEqual(events[-1]["vision_used"], "no")
        self.assertFalse(any(event["type"] == "vision_enabled" for event in events))

    def test_on_always_attaches_screenshot_and_reports_vision(self):
        client = FakeClient([plan_reply(), json.dumps({"screen": "A blank notepad window.", **json.loads(response("finish", 1))})])
        patches = self._vision_test_patches()
        with patches[0] as grab_mock, patches[1], patches[2] as save_mock:
            events = self.invoke(client, settings_extra={"vision_mode": "on"})
        self.assertEqual(grab_mock.call_count, 1)
        action_messages = client.calls[1]["messages"]
        self.assertTrue(any(isinstance(message.get("content"), list) and
                            any(part.get("type") == "image_url" for part in message["content"])
                            for message in action_messages))
        self.assertEqual(events[-1]["vision_used"], "yes")
        self.assertEqual(events[-1]["total_api_calls"], 2)
        self.assertTrue(any(event["type"] == "screen" and "blank notepad" in event["text"] for event in events))
        self.assertIn("one short sentence", client.calls[1]["messages"][0]["content"])
        self.assertEqual(save_mock.call_args_list[0].args[2], 1)

    def test_auto_stays_text_only_without_planner_or_action_escalation(self):
        client = FakeClient([plan_reply(), response("finish", 1, {"message": "done"})])
        patches = self._vision_test_patches()
        with patches[0] as grab_mock:
            events = self.invoke(client, settings_extra={"vision_mode": "auto"})
        grab_mock.assert_not_called()
        self.assertEqual(events[-1]["vision_used"], "no")

    def test_auto_planner_needs_vision_enables_and_remains_on(self):
        first_plan = json.loads(plan_reply())
        first_plan.update(needs_vision=True, reason="Read the visible status.")
        first_action = {"screen": "The app is open.", "thought": "Inspecting.",
                        "action": "wait", "args": {"seconds": 1}, "step": 1}
        last_action = {"screen": "The status is visible.", "thought": "Done.",
                       "action": "finish", "args": {"message": "done"}, "step": 1}
        client = FakeClient([json.dumps(first_plan), json.dumps(first_action), json.dumps(last_action)])
        patches = self._vision_test_patches()
        with patches[0] as grab_mock, patches[1], patches[2]:
            events = self.invoke(client, settings_extra={"vision_mode": "auto"})
        self.assertEqual(grab_mock.call_count, 2)
        self.assertEqual(events[-1]["vision_used"], "turned on at step 1")
        self.assertEqual(events[-1]["total_api_calls"], 3)
        self.assertEqual(len([event for event in events if event["type"] == "screen"]), 2)
        enabled = next(event for event in events if event["type"] == "vision_enabled")
        self.assertEqual(enabled["reason"], "Read the visible status.")
        for call in client.calls[1:]:
            self.assertTrue(any(isinstance(message.get("content"), list) for message in call["messages"]))

    def test_screen_first_attaches_image_and_updates_plan(self):
        first = json.loads(plan_reply())
        first.update(screen_first=True, reason="A dialog may be present.")
        second = json.loads(plan_reply())
        second["goal"] = "Dismiss the visible dialog"
        second["screen"] = "A confirmation dialog is open."
        client = FakeClient([json.dumps(first), json.dumps(second), response("finish", 1, {"message": "done"})])
        patches = self._vision_test_patches()
        with patches[0] as grab_mock, patches[1], patches[2] as save_mock:
            events = self.invoke(client, settings_extra={"vision_mode": "auto"})
        self.assertEqual(grab_mock.call_count, 1)
        screen_call = client.calls[1]["messages"]
        self.assertTrue(any(isinstance(message.get("content"), list) and
                            any(part.get("type") == "image_url" for part in message["content"])
                            for message in screen_call))
        self.assertTrue(any(call.args[2] == 0 for call in save_mock.call_args_list))
        plan_event = next(event for event in events if event["type"] == "plan")
        self.assertTrue(plan_event["based_on_screen"])
        self.assertIn("Based on the current screen", plan_event["note"])
        self.assertEqual(plan_event["goal"], "Dismiss the visible dialog")
        self.assertTrue(any(event["type"] == "screen" and "confirmation dialog" in event["text"]
                            for event in events))
        self.assertEqual(events[-1]["vision_used"], "yes")
        self.assertEqual(events[-1]["api_calls"], 3)

    def test_screen_first_off_keeps_first_plan_without_screenshot(self):
        first = json.loads(plan_reply())
        first.update(screen_first=True)
        client = FakeClient([json.dumps(first), response("finish", 1, {"message": "done"})])
        patches = self._vision_test_patches()
        with patches[0] as grab_mock:
            events = self.invoke(client, settings_extra={"vision_mode": "off"})
        grab_mock.assert_not_called()
        self.assertEqual(len(client.calls), 2)
        self.assertFalse(next(event for event in events if event["type"] == "plan")["based_on_screen"])

    def test_screen_first_screenshot_is_saved_as_step_00_jpg(self):
        first = json.loads(plan_reply())
        first["screen_first"] = True
        client = FakeClient([json.dumps(first), plan_reply(), response("finish", 1)])
        with tempfile.TemporaryDirectory() as folder, \
             patch("engine.grab", return_value=base64.b64encode(b"grid-image").decode("ascii")), \
             patch("engine.create_screenshot_task_dir", return_value=folder):
            self.invoke(client, settings_extra={"vision_mode": "auto"})
            self.assertTrue(os.path.isfile(os.path.join(folder, "step-00.jpg")))

    def test_screen_first_failed_visual_planner_retains_first_plan_with_warning(self):
        first = json.loads(plan_reply())
        first.update(screen_first=True)
        client = FakeClient([json.dumps(first), RuntimeError("planner unavailable"),
                             response("finish", 1, {"message": "done"})])
        patches = self._vision_test_patches()
        with patches[0], patches[1], patches[2]:
            events = self.invoke(client, settings_extra={"vision_mode": "auto"})
        plan_event = next(event for event in events if event["type"] == "plan")
        self.assertEqual(plan_event["goal"], first["goal"])
        self.assertFalse(plan_event["based_on_screen"])
        self.assertTrue(any(event["type"] == "warning" and "keeping the first plan" in event["text"]
                            for event in events))

    def test_need_vision_escalation_replans_from_a_screenshot(self):
        first_plan = json.loads(plan_reply())
        request = response("need_vision", 1, {"reason": "A popup needs a visual check."})
        remaining = json.dumps({"steps": [{"id": 1, "title": "Handle the popup"},
                                           {"id": 2, "title": "Verify completion"}],
                                "screen": "A popup is visible in the foreground."})
        finish = {"screen": "The popup is gone.", "thought": "Complete.", "action": "finish",
                  "args": {"message": "done"}, "step": 1}
        client = FakeClient([json.dumps(first_plan), request, remaining, json.dumps(finish)])
        patches = self._vision_test_patches()
        with patches[0] as grab_mock, patches[1], patches[2]:
            events = self.invoke(client, settings_extra={"vision_mode": "auto"})
        self.assertEqual(events[-1]["vision_used"], "turned on at step 1")
        self.assertEqual(events[-1]["replans"], 1)
        self.assertTrue(any(event["type"] == "replan" and "popup" in event["reason"] for event in events))
        self.assertTrue(any(event["type"] == "screen" and "foreground" in event["text"] for event in events))
        self.assertIn("one-sentence `screen` field", client.calls[2]["messages"][0]["content"])
        self.assertIn("foreground", str(client.calls[3]["messages"]))
        replan_call = client.calls[2]["messages"][-1]["content"]
        self.assertTrue(any(part.get("type") == "image_url" for part in replan_call))
        self.assertGreaterEqual(grab_mock.call_count, 2)

    def test_two_consecutive_failures_escalate_and_replan(self):
        client = FakeClient([plan_reply(), response("wait", 1), response("wait", 1),
                             json.dumps({"steps": [{"id": 1, "title": "Recover"}]}),
                             response("finish", 1, {"message": "recovered"})])
        results = iter([False, False])
        patches = self._vision_test_patches()
        with patches[0] as grab_mock, patches[1], patches[2]:
            events = self.invoke(client, lambda *_: next(results), settings_extra={"vision_mode": "auto"})
        self.assertEqual(events[-1]["vision_used"], "turned on at step 2")
        self.assertEqual(events[-1]["replans"], 1)
        self.assertTrue(any(event["type"] == "vision_enabled" and "Two actions in a row" in event["reason"]
                            for event in events))
        self.assertGreaterEqual(grab_mock.call_count, 2)

    def test_image_unsupported_falls_back_once_with_requested_warning(self):
        client = FakeClient([plan_reply(), RuntimeError("This model does not support image inputs"),
                             response("finish", 1, {"message": "done"})])
        patches = self._vision_test_patches()
        with patches[0] as grab_mock, patches[1], patches[2]:
            events = self.invoke(client, settings_extra={"vision_mode": "on"})
        self.assertEqual(grab_mock.call_count, 1)
        self.assertTrue(any(event["type"] == "warning" and
                            "this model cannot read images; pick a vision model or untick vision" == event["text"]
                            for event in events))
        self.assertEqual(events[-1]["vision_used"], "yes")
        self.assertEqual(events[-1]["api_calls"], 3)
        self.assertFalse(any(isinstance(message.get("content"), list) for message in client.calls[2]["messages"]))

    def test_model_replan_preserves_completed_steps_and_continues_ids(self):
        replacement = json.dumps({"steps": [{"id": 2, "title": "Revised second step"},
                                             {"id": 3, "title": "Revised final step"}]})
        client = FakeClient([plan_reply(), response("wait", 1), response("replan", 2, {"reason": "The route changed."}),
                             replacement, response("finish", 2, {"message": "done"})])
        events = self.invoke(client, lambda *_: "ok")
        replan_call = client.calls[3]["messages"][-1]["content"]
        request = json.loads(replan_call)
        self.assertEqual([step["id"] for step in request["completed_steps"]], [1])
        self.assertEqual(request["next_step_id"], 2)
        event = next(event for event in events if event["type"] == "replan")
        self.assertEqual([step["id"] for step in event["steps"]], [1, 2, 3])
        self.assertEqual(event["completed_ids"], [1])
        self.assertEqual(events[-1]["replans"], 1)
        self.assertEqual(events[-1]["api_calls"], 5)

    def test_overloaded_plan_step_automatically_replans_after_seven_actions(self):
        responses = [plan_reply()] + [response("wait", 1) for _ in range(7)] + [
            json.dumps({"steps": [{"id": 1, "title": "Continue"}]}), response("finish", 1)]
        client = FakeClient(responses)
        events = self.invoke(client, lambda *_: "ok")
        replan = next(event for event in events if event["type"] == "replan")
        self.assertIn("more than 6 actions", replan["reason"])
        self.assertEqual(events[-1]["replans"], 1)
        self.assertEqual(events[-1]["actions_executed"], 8)

    def test_replanning_stops_after_two_and_warns_once(self):
        texts = [plan_reply()]
        for _ in range(3):
            texts.extend([response("replan", 1, {"reason": "Try a new route."}),
                          json.dumps({"steps": [{"id": 1, "title": "Remaining"}]})])
        texts.append(response("finish", 1, {"message": "done"}))
        client = FakeClient(texts)
        events = self.invoke(client)
        self.assertEqual(events[-1]["replans"], 2)
        warnings = [event for event in events if event["type"] == "warning" and "Maximum of 2 re-plans" in event["text"]]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(len([event for event in events if event["type"] == "replan"]), 2)

    def test_vision_screen_memory_is_emitted_but_old_images_are_not_resent(self):
        first = {"screen": "The editor is empty.", "thought": "Type text.", "action": "wait",
                 "args": {"seconds": 1}, "step": 1}
        second = {"screen": "The editor now contains text.", "thought": "Done.", "action": "finish",
                  "args": {"message": "done"}, "step": 1}
        client = FakeClient([plan_reply(), json.dumps(first), json.dumps(second)])
        patches = self._vision_test_patches()
        with patches[0], patches[1], patches[2]:
            events = self.invoke(client, settings_extra={"vision_mode": "on"})
        screens = [event["text"] for event in events if event["type"] == "screen"]
        self.assertEqual(screens, [first["screen"], second["screen"]])
        for call in client.calls[1:]:
            last_user = next(message for message in reversed(call["messages"]) if message["role"] == "user")
            self.assertIsInstance(last_user["content"], list)
            self.assertEqual(sum(part.get("type") == "image_url" for part in last_user["content"]), 1)
        self.assertIn(first["screen"], str(client.calls[2]["messages"]))
        self.assertEqual(events[-1]["vision_used"], "yes")

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
        self.assertTrue(any(e["type"] == "warning" and "web_search" in e["text"] and "must be last" in e["text"]
                            for e in events))
        self.assertFalse(any(e["type"] == "search" for e in events))

    def test_notes_create_markdown_report_and_report_events(self):
        source = "https://example.org/research"
        client = FakeClient([plan_reply(), response("save_note", 1,
                             {"text": "A sourced and verified finding.", "source": source}),
                             response("finish", 1, {"message": "Research answer."})])
        events = []
        settings = {"model": "offline-test", "client": client, "sleep": lambda _seconds: None,
                    "open_report_notepad": False}
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


class ImprovePromptTests(unittest.TestCase):
    def test_valid_json_uses_exactly_one_text_only_call(self):
        payload = json.dumps({"improved": "Please save the document.", "changes": "Corrected grammar and clarified the wording."})
        client = FakeClient([payload])
        result = engine.improve_prompt("pls save doc", {"model": "offline", "client": client, "vision": True})
        self.assertEqual(result["improved"], "Please save the document.")
        self.assertEqual(result["changes"], "Corrected grammar and clarified the wording.")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["temperature"], 0)
        self.assertEqual(len(client.calls[0]["messages"]), 2)
        self.assertNotIn("image_url", json.dumps(client.calls[0]["messages"]))
        self.assertIn("same language", client.calls[0]["messages"][0]["content"])

    def test_invalid_json_raises_value_error(self):
        client = FakeClient(["not JSON"])
        with self.assertRaisesRegex(ValueError, "invalid JSON"):
            engine.improve_prompt("fix this", {"model": "offline", "client": client})
        self.assertEqual(len(client.calls), 1)

    def test_api_failure_propagates_without_retry(self):
        class BrokenClient:
            def __init__(self):
                self.calls = 0
                self.chat = types.SimpleNamespace(completions=self)
            def create(self, **_kwargs):
                self.calls += 1
                raise RuntimeError("offline API failure")

        client = BrokenClient()
        with self.assertRaisesRegex(RuntimeError, "offline API failure"):
            engine.improve_prompt("fix this", {"model": "offline", "client": client})
        self.assertEqual(client.calls, 1)

    def test_empty_input_does_not_call_model(self):
        client = FakeClient([])
        self.assertIsNone(engine.improve_prompt("  \n", {"model": "offline", "client": client}))
        self.assertEqual(client.calls, [])


class ReportNotepadTests(unittest.TestCase):
    def test_notepad_launch_arguments(self):
        with patch("engine.subprocess.Popen") as popen:
            warning = engine.open_report_in_notepad("C:/reports/report.md")
        popen.assert_called_once_with(["notepad.exe", "C:/reports/report.md"])
        self.assertEqual(warning, "")

    def test_notepad_failure_falls_back_and_returns_warning(self):
        with patch("engine.subprocess.Popen", side_effect=OSError("not found")), \
             patch.object(engine.os, "startfile", create=True) as startfile:
            warning = engine.open_report_in_notepad("C:/reports/report.md")
        startfile.assert_called_once_with("C:/reports/report.md")
        self.assertIn("Notepad could not open", warning)

    def test_failed_notepad_and_fallback_return_warning(self):
        with patch("engine.subprocess.Popen", side_effect=OSError("not found")), \
             patch.object(engine.os, "startfile", side_effect=OSError("no file handler"), create=True):
            warning = engine.open_report_in_notepad("report.md")
        self.assertIn("fallback also failed", warning)

    def test_auto_open_happens_after_report_saved_event(self):
        client = FakeClient([plan_reply(), response("save_note", 1,
                             {"text": "A fact.", "source": "https://example.test"}),
                             response("finish", 1, {"message": "Done."})])
        events = []
        settings = {"model": "offline", "client": client, "sleep": lambda _s: None,
                    "open_report_notepad": True}
        with tempfile.TemporaryDirectory() as report_dir, \
             patch.object(engine, "REPORTS_DIR", report_dir), \
             patch.object(engine, "open_report_in_notepad", side_effect=lambda _path:
                          self.assertEqual(events[-1]["type"], "report_saved") or "") as launcher:
            run_task("research", settings, events.append, threading.Event())
        launcher.assert_called_once()


class ScreenshotTests(unittest.TestCase):
    invoke = EngineTests.invoke

    def test_task_folder_file_names_and_safe_slug(self):
        now = datetime(2025, 4, 5, 6, 7, 8)
        with tempfile.TemporaryDirectory() as root:
            folder = engine.create_screenshot_task_dir("Ä VERY wild task!!! with symbols " + "x" * 40,
                                                       root=root, now=now)
            slug = engine.safe_task_slug("Ä VERY wild task!!! with symbols " + "x" * 40)
            self.assertEqual(os.path.basename(folder), "20250405-060708-" + slug)
            self.assertLessEqual(len(slug), 30)
            self.assertRegex(slug, r"^[a-z0-9-]+$")
            fake_image = base64.b64encode(b"fake jpeg bytes").decode("ascii")
            base_path = engine.save_screenshot(fake_image, folder, 1)
            self.assertEqual(os.path.basename(base_path), "step-01.jpg")
            with open(base_path, "rb") as image_file:
                self.assertEqual(image_file.read(), b"fake jpeg bytes")

    def test_click_marker_file_is_saved(self):
        fake_image = base64.b64encode(b"fake jpeg").decode("ascii")
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(engine, "_click_marker_image", return_value=b"fake marked jpeg"):
            marker_path = engine.save_click_marker(fake_image, folder, 7,
                                                   {"cell": "D3", "dx": 0.2, "dy": 0.7})
            self.assertEqual(os.path.basename(marker_path), "step-07-click.jpg")
            with open(marker_path, "rb") as marker_file:
                self.assertEqual(marker_file.read(), b"fake marked jpeg")

    def test_cleanup_removes_only_folders_older_than_seven_days(self):
        now = 1_800_000_000.0
        with tempfile.TemporaryDirectory() as root:
            old_folder = os.path.join(root, "old-task")
            new_folder = os.path.join(root, "new-task")
            os.makedirs(old_folder)
            os.makedirs(new_folder)
            os.utime(old_folder, (now - 8 * 86400, now - 8 * 86400))
            os.utime(new_folder, (now - 2 * 86400, now - 2 * 86400))
            removed = engine.cleanup_screenshot_folders(root=root, now=now)
            self.assertEqual(removed, [old_folder])
            self.assertFalse(os.path.exists(old_folder))
            self.assertTrue(os.path.isdir(new_folder))

    def test_vision_saves_current_screenshot_and_click_marker(self):
        client = FakeClient([plan_reply(), response("click", 1, {"cell": "H5"}),
                             response("finish", 1, {"message": "Clicked."})])
        fake_images = [base64.b64encode(b"jpeg one").decode("ascii"),
                       base64.b64encode(b"jpeg two").decode("ascii")]
        events = []
        settings = {"model": "offline", "client": client, "vision": True,
                    "sleep": lambda _s: None, "action_runner": lambda *_: "clicked",
                    "open_report_notepad": False}
        with tempfile.TemporaryDirectory() as screenshot_root, \
             patch.object(engine, "SCREENSHOTS_DIR", screenshot_root), \
             patch.object(engine, "grab", side_effect=fake_images), \
             patch.object(engine, "_click_marker_image", return_value=b"fake click marker"):
            run_task("Open a page!", settings, events.append, threading.Event())
            task_dirs = os.listdir(screenshot_root)
            self.assertEqual(len(task_dirs), 1)
            task_dir = os.path.join(screenshot_root, task_dirs[0])
            self.assertEqual(sorted(os.listdir(task_dir)),
                             ["step-01-click.jpg", "step-01.jpg", "step-02.jpg"])
            with open(os.path.join(task_dir, "step-02.jpg"), "rb") as f:
                self.assertEqual(f.read(), b"jpeg two")
            screenshot_events = [event for event in events if event["type"] == "screenshot_saved"]
            self.assertEqual(len(screenshot_events), 3)
            self.assertTrue(all(os.path.isfile(event["file_path"]) for event in screenshot_events))
        # The second model action turn has the new image only; earlier images are not replayed.
        vision_messages = client.calls[2]["messages"]
        image_urls = [part["image_url"]["url"] for message in vision_messages
                      if isinstance(message.get("content"), list) for part in message["content"]
                      if part.get("type") == "image_url"]
        self.assertEqual(image_urls, ["data:image/jpeg;base64," + fake_images[1]])

    def test_vision_off_creates_no_screenshot_folder(self):
        client = FakeClient([plan_reply(), response("finish", 1, {"message": "done"})])
        with tempfile.TemporaryDirectory() as parent:
            screenshot_root = os.path.join(parent, "screenshots")
            with patch.object(engine, "SCREENSHOTS_DIR", screenshot_root), \
                 patch.object(engine, "grab", side_effect=AssertionError("vision is off")):
                events = self.invoke(client, settings_extra={"vision": False})
            self.assertFalse(os.path.exists(screenshot_root))
            self.assertFalse(any(event["type"] == "screenshot_saved" for event in events))


if __name__ == "__main__":
    unittest.main(verbosity=2)
