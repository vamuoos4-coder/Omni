"""Simple Windows desktop automation agent UI. Orchestration lives in engine.py."""
import ctypes
import json
import os
import threading
import tkinter as tk
from datetime import datetime
from tkinter import scrolledtext

from engine import run_task

try:  # make screen coordinates match real pixels on scaled displays
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")


class App:
    def __init__(self, root):
        self.root = root
        root.title("Simple Automation Agent v2")
        root.geometry("660x640")
        self.stop = threading.Event()
        self.plan_rows = {}
        cfg = {}
        if os.path.exists(CONFIG):
            try:
                with open(CONFIG, encoding="utf-8") as f:
                    cfg = json.load(f)
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
        self.batch_actions = tk.BooleanVar(value=cfg.get("batch_actions", True))
        tk.Checkbutton(root, text="Batch actions (faster, not used with vision)",
                       variable=self.batch_actions).pack(anchor="w", padx=10)
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

        self.plan_frame = tk.LabelFrame(root, text="Plan")
        self.plan_frame.pack(fill="x", padx=10, pady=(0, 6))
        self.plan_goal = tk.Label(self.plan_frame, text="No plan yet", anchor="w", justify="left")
        self.plan_goal.pack(fill="x", padx=6, pady=(3, 0))
        self.plan_list = tk.Frame(self.plan_frame)
        self.plan_list.pack(fill="x", padx=6, pady=(0, 4))

        self.log = scrolledtext.ScrolledText(root, state="disabled")
        self.log.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    def _field(self, label, value, show=None):
        tk.Label(self.root, text=label).pack(anchor="w", padx=10)
        e = tk.Entry(self.root, show=show)
        e.insert(0, value)
        e.pack(fill="x", padx=10)
        return e

    def _append(self, timestamp, text):
        for line in str(text).splitlines() or [""]:
            self.log.insert("end", f"{timestamp}  {line}\n")
        self.log.see("end")

    def emit(self, event):
        """Queue engine events onto Tk's main thread; engine never touches widgets."""
        self.root.after(0, self._render_event, event)

    def _render_event(self, event):
        kind = event.get("type", "event")
        stamp = event.get("time") or datetime.now().astimezone().isoformat(timespec="seconds")
        if kind == "plan":
            self.plan_goal.configure(text="Goal: " + event.get("goal", ""))
            self.current_steps = event.get("steps", [])
            for child in self.plan_list.winfo_children():
                child.destroy()
            self.plan_rows.clear()
            for step in event.get("steps", []):
                sid = step.get("id")
                label = tk.Label(self.plan_list, text=f"○  {step.get('title', '')}", anchor="w")
                label.pack(fill="x")
                self.plan_rows[sid] = label
            self._append(stamp, "Plan created: " + event.get("goal", ""))
            return
        if kind == "step_start":
            self._set_step(event.get("step_id"), "running")
            self._append(stamp, f"Step {event.get('step_id')} started")
        elif kind == "step_done":
            self._set_step(event.get("step_id"), "done")
            self._append(stamp, f"Step {event.get('step_id')} done")
        elif kind == "thought":
            text = event.get("text", "")
            if text:
                self._append(stamp, "Thought: " + text)
        elif kind == "batch":
            self._append(stamp, f"Batch: {event.get('count', 1)} action(s)")
        elif kind == "action":
            suffix = " [NOT RUN]" if event.get("skipped") else ""
            self._append(stamp, f"Action (step {event.get('step_id', '—')}): {event.get('action')} " +
                         json.dumps(event.get("args", {}), ensure_ascii=False) + suffix)
        elif kind == "result":
            suffix = " [NOT RUN]" if event.get("skipped") else ""
            self._append(stamp, "Result: " + event.get("text", "") + suffix)
        elif kind in ("warning", "error"):
            if kind == "warning" and "continuing without a plan" in event.get("text", ""):
                self.plan_goal.configure(text="No plan (continuing without one)")
            self._append(stamp, kind.upper() + ": " + event.get("text", ""))
        elif kind == "summary":
            self._append(stamp, "Summary: Goal: " + event.get("goal", "") + " | Outcome: " + event.get("outcome", "") +
                         f" | Steps {event.get('steps_completed', 0)}/{event.get('steps_total', 0)}" +
                         f" | Actions {event.get('actions_executed', 0)}" +
                         f" | API calls {event.get('api_calls', 0)}" +
                         f" | Calls saved (estimated) {event.get('estimated_calls_saved', 0)}" +
                         f" | {event.get('seconds', 0)} seconds")

    def _set_step(self, step_id, state):
        label = self.plan_rows.get(step_id)
        if not label:
            return
        step = next((item for item in getattr(self, "current_steps", []) if item.get("id") == step_id), None)
        title = step.get("title", "") if step else label.cget("text").split("  ", 1)[-1]
        marker = {"pending": "○", "running": "▶", "done": "✓"}[state]
        label.configure(text=f"{marker}  {title}")

    def start(self):
        if self.remember.get():
            with open(CONFIG, "w", encoding="utf-8") as f:
                json.dump({"base": self.base.get(), "key": self.key.get(), "model": self.model.get(),
                           "browser": self.browser.get().strip(), "vision": self.vision.get(),
                           "batch_actions": self.batch_actions.get()}, f)
        elif os.path.exists(CONFIG):
            os.remove(CONFIG)
        task = self.task.get("1.0", "end").strip()
        if not task or not self.model.get().strip():
            self.emit({"type": "warning", "time": "", "text": "Enter a model name and a task first."})
            return
        self.stop.clear()
        self.run_btn.configure(state="disabled")
        for child in self.plan_list.winfo_children():
            child.destroy()
        self.plan_rows.clear()
        self.current_steps = []
        self.plan_goal.configure(text="Planning…")
        settings = {"base": self.base.get().strip(), "key": self.key.get().strip(),
                    "model": self.model.get().strip(), "browser": self.browser.get().strip(),
                    "vision": self.vision.get(), "batch_actions": self.batch_actions.get()}
        if settings["vision"]:
            self.root.iconify()
        threading.Thread(target=self._run, args=(task, settings), daemon=True).start()

    def _run(self, task, settings):
        try:
            run_task(task, settings, self.emit, self.stop)
        except Exception as exc:
            self.emit({"type": "error", "time": "", "text": f"ERROR: {exc}"})
        finally:
            def restore():
                self.root.deiconify()
                self.run_btn.configure(state="normal")
            self.root.after(0, restore)


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
