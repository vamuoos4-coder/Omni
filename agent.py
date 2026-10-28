"""Simple desktop automation agent (Windows) - v2 with optional vision.
Type a task (typos are fine), the AI picks one action at a time, this script runs it.
Works with any OpenAI-compatible endpoint (freellmapi, OpenRouter, OpenAI, Gemini compat...).
"""
import base64
import ctypes
import difflib
import io
import json
import os
import re
import subprocess
import threading
import time
import webbrowser
import tkinter as tk
from tkinter import scrolledtext
from urllib.parse import quote_plus

import pyautogui
import pyperclip
from openai import OpenAI
from PIL import Image, ImageDraw, ImageFont

try:  # make screen coordinates match real pixels on scaled displays
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")
MAX_STEPS = 15
MAX_STEPS_VISION = 25
COLS, ROWS = 16, 9
pyautogui.FAILSAFE = True  # slam mouse into a screen corner to abort
BROWSER = ""  # set from the UI; blank = Windows default browser

APP_ALIASES = {
    "calculator": "calc", "paint": "mspaint", "edge": "msedge", "word": "winword",
    "file explorer": "explorer", "files": "explorer", "vscode": "code",
    "command prompt": "cmd", "terminal": "cmd", "task manager": "taskmgr",
}
KNOWN_APPS = ["notepad", "chrome", "brave", "msedge", "firefox", "calc", "explorer", "mspaint",
              "winword", "excel", "powerpnt", "spotify", "code", "cmd", "taskmgr", "whatsapp", "discord"]

SYSTEM = """You control a Windows PC by choosing ONE action at a time.
The user often makes spelling mistakes. Infer what they meant (e.g. "notpad" = notepad,
"yutube" = youtube) and use correct spelling when typing or searching, unless the odd
spelling is clearly intended.

Reply with ONLY a JSON object, no markdown, no extra text:
{"thought": "short reasoning", "action": "<name>", "args": {...}}

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

Rules: after open_app, use wait 1-2 seconds before type_text. Do exactly what was asked,
nothing more. Do the typing step BEFORE opening a browser (a browser steals focus).
Call finish when the task is complete."""

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


def open_in_browser(url):
    if '"' in url or not url.startswith(("http://", "https://")):
        return False
    if BROWSER and re.fullmatch(r"[\w .\-]+", BROWSER):
        subprocess.Popen(f'start "" {BROWSER} "{url}"', shell=True)
    else:
        webbrowser.open(url)
    return True


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


def grab():
    """Screenshot with a labeled grid, returned as base64 JPEG."""
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
    m = re.fullmatch(r"([A-Pa-p])([1-9])", str(args.get("cell", "")).strip())
    if not m:
        return None
    col = ord(m.group(1).upper()) - 65
    row = int(m.group(2)) - 1
    dx = min(max(float(args.get("dx", 0.5)), 0.0), 1.0)
    dy = min(max(float(args.get("dy", 0.5)), 0.0), 1.0)
    sw, sh = pyautogui.size()
    return int((col + dx) / COLS * sw), int((row + dy) / ROWS * sh)


def run_action(action, args):
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
        return f"opened {url}" if open_in_browser(url) else "refused: url must start with http(s)"
    if action == "youtube_search":
        open_in_browser("https://www.youtube.com/results?search_query=" + quote_plus(str(args.get("query", ""))))
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


def parse_json(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON found")
    return json.loads(m.group(0))


class App:
    def __init__(self, root):
        self.root = root
        root.title("Simple Automation Agent v2")
        root.geometry("660x640")
        self.stop = threading.Event()
        cfg = {}
        if os.path.exists(CONFIG):
            try:
                cfg = json.load(open(CONFIG, encoding="utf-8"))
            except Exception:
                pass

        self.base = self._field("Base URL (e.g. http://localhost:3001/v1)", cfg.get("base", ""))
        self.key = self._field("API key", cfg.get("key", ""), show="*")
        self.model = self._field("Model name", cfg.get("model", ""))
        self.browser = self._field("Browser for links (blank = Windows default, or: chrome, brave, msedge)",
                                   cfg.get("browser", ""))
        self.vision = tk.BooleanVar(value=cfg.get("vision", False))
        tk.Checkbutton(root, text="Use vision (sends a screenshot every step; window minimizes while running)",
                       variable=self.vision).pack(anchor="w", padx=10)
        self.remember = tk.BooleanVar(value=bool(cfg))
        tk.Checkbutton(root, text="Remember settings in config.json", variable=self.remember).pack(anchor="w", padx=10)

        tk.Label(root, text="Task (spelling mistakes are OK)").pack(anchor="w", padx=10)
        self.task = tk.Text(root, height=4)
        self.task.pack(fill="x", padx=10)

        bar = tk.Frame(root)
        bar.pack(fill="x", padx=10, pady=6)
        self.run_btn = tk.Button(bar, text="Run", width=12, command=self.start)
        self.run_btn.pack(side="left")
        tk.Button(bar, text="Stop", width=12, command=self.stop.set).pack(side="left", padx=6)
        tk.Label(bar, text="Emergency stop: slam the mouse into a screen corner").pack(side="left", padx=8)

        self.log = scrolledtext.ScrolledText(root, state="disabled")
        self.log.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    def _field(self, label, value, show=None):
        tk.Label(self.root, text=label).pack(anchor="w", padx=10)
        e = tk.Entry(self.root, show=show)
        e.insert(0, value)
        e.pack(fill="x", padx=10)
        return e

    def say(self, msg):
        def _do():
            self.log.configure(state="normal")
            self.log.insert("end", msg + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
        self.root.after(0, _do)

    def start(self):
        global BROWSER
        BROWSER = self.browser.get().strip()
        if self.remember.get():
            json.dump({"base": self.base.get(), "key": self.key.get(), "model": self.model.get(),
                       "browser": BROWSER, "vision": self.vision.get()},
                      open(CONFIG, "w", encoding="utf-8"))
        elif os.path.exists(CONFIG):
            os.remove(CONFIG)
        task = self.task.get("1.0", "end").strip()
        if not task or not self.model.get().strip():
            self.say("Enter a model name and a task first.")
            return
        self.stop.clear()
        self.run_btn.configure(state="disabled")
        use_vision = self.vision.get()
        if use_vision:
            self.root.iconify()
        threading.Thread(target=self.loop, args=(task, use_vision), daemon=True).start()

    def loop(self, task, use_vision):
        try:
            client = OpenAI(base_url=self.base.get().strip() or None,
                            api_key=self.key.get().strip() or "none")
            msgs = [{"role": "system", "content": SYSTEM + (VISION_ADDON if use_vision else "")},
                    {"role": "user", "content": "Task: " + task}]
            self.say(f"=== Task: {task}  (vision {'ON' if use_vision else 'off'})")
            if use_vision:
                time.sleep(0.8)  # let our window minimize
            for step in range(1, (MAX_STEPS_VISION if use_vision else MAX_STEPS) + 1):
                if self.stop.is_set():
                    self.say("Stopped.")
                    return
                call_msgs = msgs
                if use_vision:
                    b64 = grab()
                    call_msgs = msgs[:-1] + [{"role": "user", "content": [
                        {"type": "text", "text": msgs[-1]["content"] + "\n(Latest screenshot with grid attached.)"},
                        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
                    ]}]
                try:
                    resp = client.chat.completions.create(model=self.model.get().strip(),
                                                          messages=call_msgs, temperature=0)
                except Exception as e:
                    hint = "  (this model may not accept images - untick vision)" if use_vision else ""
                    self.say(f"API ERROR: {e}{hint}")
                    return
                text = resp.choices[0].message.content or ""
                msgs.append({"role": "assistant", "content": text})
                try:
                    data = parse_json(text)
                    action, args = data.get("action", ""), data.get("args", {}) or {}
                except Exception:
                    self.say(f"[{step}] bad reply, asking again")
                    msgs.append({"role": "user", "content": "Invalid. Reply with ONLY the JSON object."})
                    continue
                self.say(f"[{step}] {data.get('thought', '')}\n     -> {action} {json.dumps(args, ensure_ascii=False)}")
                if action == "finish":
                    self.say("Done: " + str(args.get("message", "")))
                    return
                try:
                    result = run_action(action, args)
                except pyautogui.FailSafeException:
                    self.say("Emergency stop (mouse in corner).")
                    return
                except Exception as e:
                    result = f"action failed: {e}"
                self.say("     result: " + result)
                msgs.append({"role": "user", "content": "Result: " + result})
                time.sleep(1.2)
            self.say("Reached the step limit.")
        except Exception as e:
            self.say(f"ERROR: {e}")
        finally:
            def _restore():
                self.root.deiconify()
                self.run_btn.configure(state="normal")
            self.root.after(0, _restore)


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
