"""Automation engine: model orchestration and desktop actions, with no UI imports."""
import base64
import difflib
import io
import json
import os
import re
import subprocess
import time
import webbrowser
from datetime import datetime, timezone
from urllib.parse import quote_plus

MAX_STEPS = 15
MAX_STEPS_VISION = 25
COLS, ROWS = 16, 9

APP_ALIASES = {
    "calculator": "calc", "paint": "mspaint", "edge": "msedge", "word": "winword",
    "file explorer": "explorer", "files": "explorer", "vscode": "code",
    "command prompt": "cmd", "terminal": "cmd", "task manager": "taskmgr",
}
KNOWN_APPS = ["notepad", "chrome", "brave", "msedge", "firefox", "calc", "explorer", "mspaint",
              "winword", "excel", "powerpnt", "spotify", "code", "cmd", "taskmgr", "whatsapp", "discord"]

SYSTEM = """You control a Windows PC using the action format specified below.
The user often makes spelling mistakes. Infer what they meant (e.g. "notpad" = notepad,
"yutube" = youtube) and use correct spelling when typing or searching, unless the odd
spelling is clearly intended.

Reply with ONLY a JSON object, no markdown, no extra text:
{"thought": "short reasoning", "action": "<name>", "args": {...}, "step": <plan step id>}

Actions:
- open_app      args: {"name": "notepad" | "chrome" | "calc" | "explorer" ...}
- type_text     args: {"text": "..."}              types into the focused window
- press_key     args: {"keys": "enter"} or {"keys": "ctrl+s"}
- open_url      args: {"url": "https://..."}
- youtube_search args: {"query": "..."}            opens YouTube results directly
- open_path     args: {"path": "C:/folder or file"}
- play_song     args: {"name": "part of song file name"}   searches the Music folder
- wait          args: {"seconds": 2}
- finish        args: {"message": "what was done"}

Rules: the engine automatically waits about 1.5 seconds after a successful open_app. Do exactly
what was asked, nothing more. Do the typing step BEFORE opening a browser (a browser steals focus).
Call finish when the task is complete."""

BATCH_ADDON = """

BATCH ACTIONS (non-vision mode only): You may return either the existing single-action JSON object,
or one JSON object with up to 4 actions:
{"thought":"short reasoning","actions":[{"action":"open_app","args":{"name":"notepad"},"step":1},{"action":"type_text","args":{"text":"hello"},"step":2}]}
Batch only a predictable sequence that needs no screen check between actions (for example open_app,
wait, type_text, press_key ctrl+s). Include the plan step id separately on every action when a plan
is present. The engine automatically waits about 1.5 seconds after a successful open_app, so do not
add a wait solely for app startup. `finish` may only be the last action. Maximum four actions."""

VISION_ADDON = """

VISION IS ON. Each turn you get a screenshot of the whole screen with a red grid.
Columns are letters A-P (left to right), rows are numbers 1-9 (top to bottom); each cell
is labeled in its top-left corner, e.g. "F5".
Extra actions:
- click         args: {"cell": "F5", "dx": 0.5, "dy": 0.5}   dx/dy (0 to 1) = position inside the cell, default centre
- double_click  same args as click
- right_click   same args as click
- scroll        args: {"direction": "down" | "up", "amount": 3}
Always look at the latest screenshot to check whether your last action worked. If it did not,
try something different instead of repeating it. Prefer open_url/youtube_search/open_app
over clicking when they can do the job. To click a small target, pick the cell that
contains its centre and set dx/dy precisely."""

PLANNER_SYSTEM = """You are a task planner for a Windows desktop automation agent. Infer intended meaning and correct spelling mistakes in the goal. Return ONLY valid JSON, with no markdown or other text, in exactly this shape:
{"goal":"...","steps":[{"id":1,"title":"..."}]}
Make 3 to 8 short, actionable steps. Use consecutive integer ids starting at 1."""


def _now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def parse_json(text):
    """Parse a JSON object, tolerating surrounding prose as in the legacy agent."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON found")
    value = json.loads(m.group(0))
    if not isinstance(value, dict):
        raise ValueError("JSON response must be an object")
    return value


def parse_plan(text):
    """Validate the planner's response and return a normalized plan."""
    # Planner output is explicitly JSON-only: reject surrounding text/markdown.
    value = json.loads(text.strip())
    if not isinstance(value, dict) or not isinstance(value.get("goal"), str) or not value["goal"].strip():
        raise ValueError("plan must include a non-empty goal")
    steps = value.get("steps")
    if not isinstance(steps, list) or not 3 <= len(steps) <= 8:
        raise ValueError("plan must contain 3 to 8 steps")
    normalized = []
    ids = set()
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError("each plan step must be an object")
        step_id = step.get("id")
        title = step.get("title")
        if isinstance(step_id, bool) or not isinstance(step_id, int) or not isinstance(title, str) or not title.strip():
            raise ValueError("each plan step needs an integer id and title")
        if step_id in ids:
            raise ValueError("plan step ids must be unique")
        ids.add(step_id)
        normalized.append({"id": step_id, "title": title.strip()})
    return {"goal": value["goal"].strip(), "steps": normalized}


def fix_app_name(name):
    low = name.lower().strip()
    if low in APP_ALIASES:
        return APP_ALIASES[low]
    if low in KNOWN_APPS:
        return low
    close = difflib.get_close_matches(low, KNOWN_APPS + list(APP_ALIASES), n=1, cutoff=0.75)
    if close:
        return APP_ALIASES.get(close[0], close[0])
    return name


def open_in_browser(url, browser=""):
    if '"' in url or not url.startswith(("http://", "https://")):
        return False
    if browser and re.fullmatch(r"[\w .\-]+", browser):
        subprocess.Popen(f'start "" {browser} "{url}"', shell=True)
    else:
        webbrowser.open(url)
    return True


def _pyautogui():
    import pyautogui
    pyautogui.FAILSAFE = True  # preserve the corner emergency stop
    return pyautogui


def grab():
    """Screenshot with a labeled grid, returned as base64 JPEG."""
    pyautogui = _pyautogui()
    from PIL import Image, ImageDraw, ImageFont
    shot = pyautogui.screenshot()
    sw, sh = shot.size
    w = 1280
    h = int(sh * w / sw)
    img = shot.resize((w, h)).convert("RGBA")
    ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    try:
        font = ImageFont.truetype("arial.ttf", 13)
    except Exception:
        font = ImageFont.load_default()
    cw, ch = w / COLS, h / ROWS
    for c in range(COLS + 1):
        x = int(c * cw)
        d.line([(x, 0), (x, h)], fill=(255, 0, 0, 110), width=1)
    for r in range(ROWS + 1):
        y = int(r * ch)
        d.line([(0, y), (w, y)], fill=(255, 0, 0, 110), width=1)
    for c in range(COLS):
        for r in range(ROWS):
            d.text((int(c * cw) + 3, int(r * ch) + 2), f"{chr(65 + c)}{r + 1}",
                   fill=(255, 0, 0, 235), font=font)
    img = Image.alpha_composite(img, ov).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=70)
    return base64.b64encode(buf.getvalue()).decode()


def cell_to_xy(args):
    pyautogui = _pyautogui()
    m = re.fullmatch(r"([A-Pa-p])([1-9])", str(args.get("cell", "")).strip())
    if not m:
        return None
    col = ord(m.group(1).upper()) - 65
    row = int(m.group(2)) - 1
    dx = min(max(float(args.get("dx", 0.5)), 0.0), 1.0)
    dy = min(max(float(args.get("dy", 0.5)), 0.0), 1.0)
    sw, sh = pyautogui.size()
    return int((col + dx) / COLS * sw), int((row + dy) / ROWS * sh)


def run_action(action, args, settings=None):
    settings = settings or {}
    pyautogui = _pyautogui()
    import pyperclip
    browser = settings.get("browser", "")
    if action == "open_app":
        name = fix_app_name(str(args.get("name", "")))
        if not re.fullmatch(r"[\w .\-]+", name):
            return "refused: invalid app name"
        subprocess.Popen(f'start "" {name}', shell=True)
        return f"opened {name}"
    if action == "type_text":
        time.sleep(0.4)
        pyperclip.copy(str(args.get("text", "")))
        pyautogui.hotkey("ctrl", "v")
        return "typed text"
    if action == "press_key":
        keys = [k.strip().lower() for k in str(args.get("keys", "")).split("+") if k.strip()]
        if not keys:
            return "no keys given"
        pyautogui.hotkey(*keys)
        return f"pressed {'+'.join(keys)}"
    if action == "open_url":
        url = str(args.get("url", ""))
        return f"opened {url}" if open_in_browser(url, browser) else "refused: url must start with http(s)"
    if action == "youtube_search":
        open_in_browser("https://www.youtube.com/results?search_query=" + quote_plus(str(args.get("query", ""))), browser)
        return "opened youtube results"
    if action == "open_path":
        path = os.path.expandvars(os.path.expanduser(str(args.get("path", ""))))
        if not os.path.exists(path):
            return f"path not found: {path}"
        os.startfile(path)
        return f"opened {path}"
    if action == "play_song":
        want = str(args.get("name", "")).lower()
        music = os.path.join(os.path.expanduser("~"), "Music")
        for root, _, files in os.walk(music):
            for f in files:
                if want in f.lower() and f.lower().endswith((".mp3", ".wav", ".flac", ".m4a", ".ogg")):
                    os.startfile(os.path.join(root, f))
                    return f"playing {f}"
        return "song not found in Music folder"
    if action in ("click", "double_click", "right_click"):
        xy = cell_to_xy(args)
        if not xy:
            return "bad cell, use a letter A-P and number 1-9 like F5"
        x, y = xy
        pyautogui.moveTo(x, y, duration=0.25)
        if action == "click":
            pyautogui.click()
        elif action == "double_click":
            pyautogui.doubleClick()
        else:
            pyautogui.rightClick()
        return f"{action} at cell {args.get('cell')}"
    if action == "scroll":
        amount = int(args.get("amount", 3)) * 100
        pyautogui.scroll(-amount if str(args.get("direction", "down")).lower() == "down" else amount)
        return "scrolled"
    if action == "wait":
        time.sleep(min(float(args.get("seconds", 1)), 10))
        return "waited"
    return f"unknown action: {action}"


def _client_for(settings):
    client = settings.get("client") or settings.get("model_client")
    if client is not None:
        return client
    from openai import OpenAI
    return OpenAI(base_url=settings.get("base", "").strip() or None,
                  api_key=settings.get("key", "").strip() or "none")


def _content(response):
    return response.choices[0].message.content or ""


def _action_failed(result):
    if result is False:
        return True
    text = str(result).strip().lower()
    return text.startswith((
        "refused:", "action failed:", "unknown action:", "path not found:",
        "song not found", "bad cell,", "no keys given",
    ))


def _action_label(action, args):
    if action == "open_app":
        return f"open_app {args.get('name', '')}".strip()
    if action == "press_key":
        return f"press_key {args.get('keys', '')}".strip()
    if action == "open_url":
        return f"open_url {args.get('url', '')}".strip()
    if action == "youtube_search":
        return f"youtube_search {args.get('query', '')}".strip()
    if action in ("type_text", "open_path", "play_song"):
        key = {"type_text": "text", "open_path": "path", "play_song": "name"}[action]
        value = str(args.get(key, ""))
        return f"{action} {value[:80]}".strip()
    return action


def _parse_action_reply(data):
    """Normalize old single-action and new multi-action model response shapes."""
    if "actions" not in data:
        action, args = data.get("action", ""), data.get("args", {}) or {}
        if not isinstance(action, str) or not action.strip() or not isinstance(args, dict):
            raise ValueError("action and args have invalid types")
        return [{"action": action, "args": args, "step": data.get("step")}], False
    values = data.get("actions")
    if not isinstance(values, list) or not values:
        raise ValueError("actions must be a non-empty list")
    # Validate the bounded part only; anything beyond the safety cap is ignored.
    normalized = []
    for item in values[:4]:
        if not isinstance(item, dict):
            raise ValueError("each action must be an object")
        action = item.get("action")
        args = item.get("args", {}) or {}
        if not isinstance(action, str) or not action.strip() or not isinstance(args, dict):
            raise ValueError("each action needs a name and object args")
        normalized.append({"action": action, "args": args, "step": item.get("step", data.get("step"))})
    return normalized, True


def run_task(task, settings, emit, stop_event):
    """Run planning and action loop. ``settings`` may inject client/action_runner for tests."""
    settings = dict(settings or {})
    started = time.monotonic()
    api_calls = 0
    actions_executed = 0
    plan = None
    completed = set()
    active_id = None
    outcome = "Task ended."
    calls_limit = MAX_STEPS_VISION if settings.get("vision", False) else MAX_STEPS
    sleep = settings.get("sleep", time.sleep)
    vision = bool(settings.get("vision", False))
    batching_enabled = bool(settings.get("batch_actions", True)) and not vision

    def send(kind, **data):
        event = {"type": kind, "time": _now()}
        event.update(data)
        try:
            emit(event)
        except Exception:
            pass

    def complete_step(step_id):
        if step_id is not None and step_id not in completed:
            completed.add(step_id)
            send("step_done", step_id=step_id)

    def start_step(item):
        nonlocal active_id
        if not plan:
            return None
        step_id = item.get("step")
        valid_ids = {part["id"] for part in plan["steps"]}
        if isinstance(step_id, bool) or not isinstance(step_id, int) or step_id not in valid_ids:
            send("warning", text="Missing or invalid plan step id; using the first plan step.")
            step_id = plan["steps"][0]["id"]
        if active_id != step_id:
            if active_id is not None:
                complete_step(active_id)
            active_id = step_id
            send("step_start", step_id=step_id)
        return step_id

    try:
        send("thought", text=f"Starting task: {task}")
        if stop_event.is_set():
            outcome = "Stopped before planning."
            return
        try:
            client = _client_for(settings)
        except Exception as exc:
            send("error", text=f"Could not initialize model client: {exc}")
            outcome = "Could not initialize the model client."
            return

        # Plan once, retry once only for invalid planner JSON/schema.
        for attempt in range(2):
            if stop_event.is_set():
                outcome = "Stopped during planning."
                return
            try:
                api_calls += 1
                response = client.chat.completions.create(
                    model=settings.get("model", ""),
                    messages=[{"role": "system", "content": PLANNER_SYSTEM},
                              {"role": "user", "content": "Task: " + task}],
                    temperature=0)
                plan = parse_plan(_content(response))
                break
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                if attempt == 0:
                    send("warning", text=f"Invalid plan; retrying once ({exc}).")
                    continue
                send("warning", text=f"Planner returned an invalid plan twice; continuing without a plan ({exc}).")
                plan = None
            except Exception as exc:
                send("warning", text=f"Planner call failed; continuing without a plan: {exc}")
                plan = None
                break

        if plan:
            send("plan", goal=plan["goal"], steps=plan["steps"])
            system = SYSTEM + (VISION_ADDON if vision else "")
            user_context = ("Task: " + task + "\n\nPlan (follow these steps; include the current plan step id "
                            "on every action, including each action in a batch):\n" +
                            json.dumps(plan, ensure_ascii=False))
        else:
            system = SYSTEM.replace(', "step": <plan step id>', "")
            if vision:
                system += VISION_ADDON
            user_context = "Task: " + task
        if batching_enabled:
            system += BATCH_ADDON

        msgs = [{"role": "system", "content": system}, {"role": "user", "content": user_context}]
        if vision:
            sleep(0.8)

        loop_ended = False
        for action_index in range(1, calls_limit + 1):
            if stop_event.is_set():
                outcome = "Stopped by user."
                send("warning", text=outcome)
                loop_ended = True
                break
            call_msgs = msgs
            if vision:
                b64 = grab()
                call_msgs = msgs[:-1] + [{"role": "user", "content": [
                    {"type": "text", "text": msgs[-1]["content"] + "\n(Latest screenshot with grid attached.)"},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
                ]}]
            try:
                api_calls += 1
                response = client.chat.completions.create(model=settings.get("model", ""),
                                                          messages=call_msgs, temperature=0)
                text = _content(response)
            except Exception as exc:
                hint = " (this model may not accept images - untick vision)" if vision else ""
                send("error", text=f"API ERROR: {exc}{hint}")
                outcome = "Stopped after an API error."
                loop_ended = True
                break
            msgs.append({"role": "assistant", "content": text})
            try:
                data = parse_json(text)
                actions, is_batch = _parse_action_reply(data)
            except Exception:
                send("warning", text=f"[{action_index}] Bad reply; asking again.")
                msgs.append({"role": "user", "content": "Invalid. Reply with ONLY the JSON object."})
                continue

            requested_count = len(actions)
            if is_batch and len(data.get("actions", [])) > 4:
                send("warning", text="Batch exceeds four actions; extra actions ignored.")
            if is_batch and vision and requested_count > 1:
                send("warning", text="Vision mode: one action per turn.")
                actions = actions[:1]
            elif is_batch and not batching_enabled and requested_count > 1:
                send("warning", text="Batch actions are disabled; running only the first action.")
                actions = actions[:1]

            # A finish action can only terminate the executed batch.
            if any(item["action"] == "finish" for item in actions[:-1]):
                send("warning", text="Invalid batch: finish must be the last action; asking again.")
                msgs.append({"role": "user", "content": "Invalid batch. The finish action may only be last."})
                continue

            send("thought", text=str(data.get("thought", "")))
            send("batch", count=len(actions))
            combined = []
            single_result_text = "NOT RUN"
            stopped_in_batch = False
            failed_in_batch = False
            finished_task = False
            emergency_stop = False
            for index, item in enumerate(actions):
                action, args = item["action"], item["args"]
                if stop_event.is_set():
                    stopped_in_batch = True
                    for rest_index in range(index, len(actions)):
                        rest = actions[rest_index]
                        label = _action_label(rest["action"], rest["args"])
                        combined.append(f"{rest_index + 1}. {label}: NOT RUN (stopped)")
                        send("action", action=rest["action"], args=rest["args"],
                             step_id=rest.get("step"), skipped=True)
                        send("result", text="NOT RUN (stopped)", step_id=rest.get("step"), skipped=True)
                    break

                step_id = start_step(item)
                send("thought", text=str(data.get("thought", "")), step_id=step_id)
                send("action", action=action, args=args, step_id=step_id)
                actions_executed += 1
                label = _action_label(action, args)
                if action == "finish":
                    message = str(args.get("message", ""))
                    result = "Done: " + message
                    single_result_text = result
                    send("result", text=result, step_id=step_id)
                    if active_id is not None:
                        complete_step(active_id)
                        active_id = None
                    outcome = message or "Task completed."
                    combined.append(f"{index + 1}. {label}: OK ({result})")
                    finished_task = True
                    break

                try:
                    action_runner = settings.get("action_runner", run_action)
                    result = action_runner(action, args, settings)
                    if action == "open_app" and not _action_failed(result):
                        sleep(1.5)
                except Exception as exc:
                    try:
                        import pyautogui
                        failsafe = isinstance(exc, pyautogui.FailSafeException)
                    except Exception:
                        failsafe = exc.__class__.__name__ == "FailSafeException"
                    if failsafe:
                        send("warning", text="Emergency stop (mouse in corner).")
                        outcome = "Emergency stop (mouse in corner)."
                        result = outcome
                        emergency_stop = True
                    else:
                        result = f"action failed: {exc}"

                failed = _action_failed(result)
                result_text = str(result)
                single_result_text = result_text
                send("result", text=result_text, step_id=step_id)
                if failed:
                    combined.append(f"{index + 1}. {label}: FAILED ({result_text})")
                    failed_in_batch = True
                    for rest_index in range(index + 1, len(actions)):
                        rest = actions[rest_index]
                        rest_label = _action_label(rest["action"], rest["args"])
                        combined.append(f"{rest_index + 1}. {rest_label}: NOT RUN")
                        send("action", action=rest["action"], args=rest["args"],
                             step_id=rest.get("step"), skipped=True)
                        send("result", text="NOT RUN", step_id=rest.get("step"), skipped=True)
                    break
                combined.append(f"{index + 1}. {label}: OK ({result_text})")
                if emergency_stop:
                    failed_in_batch = True
                    break

            # Send exactly one combined result turn to the model for a batch response.
            if vision and is_batch and requested_count > 1:
                feedback = "Vision mode: one action per turn.\n" + "\n".join(combined)
            elif is_batch:
                feedback = "\n".join(combined)
            else:
                feedback = single_result_text
            if vision and is_batch and requested_count > 1:
                msgs.append({"role": "user", "content": feedback})
            elif is_batch:
                msgs.append({"role": "user", "content": "Batch results:\n" + feedback})
            else:
                msgs.append({"role": "user", "content": "Result: " + feedback})

            if stopped_in_batch:
                outcome = "Stopped by user during batch."
                loop_ended = True
                break
            if emergency_stop:
                loop_ended = True
                break
            if finished_task:
                loop_ended = True
                break
            # A failed batch stops at that action; the next model turn receives the failure.
            if failed_in_batch:
                pass
            sleep(1.2)
        if not loop_ended:
            outcome = "Reached the step limit."
            send("warning", text=outcome)
    except Exception as exc:
        send("error", text=f"ERROR: {exc}")
        outcome = f"Task failed: {exc}"
    finally:
        total = len(plan["steps"]) if plan else 0
        estimated_saved = actions_executed - api_calls
        send("summary", goal=plan["goal"] if plan else task,
             steps_completed=len(completed), steps_total=total,
             actions_executed=actions_executed,
             api_calls=api_calls, estimated_calls_saved=estimated_saved,
             seconds=round(time.monotonic() - started, 2),
             outcome=" ".join(str(outcome).split()))
