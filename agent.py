"""Simple Windows desktop automation agent UI. Orchestration lives in engine.py."""
import ctypes
import json
import os
import threading
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, scrolledtext, simpledialog, ttk

from engine import (REPORTS_DIR, SCREENSHOTS_DIR, SKILLS_DIR, cleanup_screenshot_folders,
                    clear_screenshot_folders, delete_skill, extract_skill, improve_prompt,
                    list_skills, load_skill, migrate_vision_mode, open_report_in_notepad,
                    redact_skill_secrets, run_task, save_skill, skill_exists, suggest_skill_name,
                    validate_skill_name)

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
        self.latest_report_path = ""
        self._skill_candidate = None
        self._skills_window = None
        self._skill_tree = None
        self._skill_search_var = None
        self._undo_original = None
        self._setting_task_text = False
        self._improving = False
        try:
            cleanup_screenshot_folders()
        except Exception:
            pass
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
        self.vision_mode = tk.StringVar(value=migrate_vision_mode(cfg))
        vision_row = tk.Frame(root)
        vision_row.pack(anchor="w", padx=10)
        tk.Label(vision_row, text="Vision:").pack(side="left")
        for label, value in (("Off", "off"), ("Auto", "auto"), ("On", "on")):
            tk.Radiobutton(vision_row, text=label, variable=self.vision_mode, value=value).pack(side="left")
        self.batch_actions = tk.BooleanVar(value=cfg.get("batch_actions", True))
        tk.Checkbutton(root, text="Batch actions (faster, not used with vision)",
                       variable=self.batch_actions).pack(anchor="w", padx=10)
        self.open_report_notepad = tk.BooleanVar(value=cfg.get("open_report_notepad", True))
        tk.Checkbutton(root, text="Open report in Notepad when done",
                       variable=self.open_report_notepad).pack(anchor="w", padx=10)
        self.remember = tk.BooleanVar(value=bool(cfg))
        tk.Checkbutton(root, text="Remember settings in config.json", variable=self.remember).pack(anchor="w", padx=10)

        tk.Label(root, text="Task (spelling mistakes are OK)").pack(anchor="w", padx=10)
        task_row = tk.Frame(root)
        task_row.pack(fill="x", padx=10)
        self.task = tk.Text(task_row, height=4)
        self.task.pack(side="left", fill="x", expand=True)
        task_buttons = tk.Frame(task_row)
        task_buttons.pack(side="left", fill="y", padx=(6, 0))
        self.improve_btn = tk.Button(task_buttons, text="Improve", command=self.improve_task)
        self.improve_btn.pack(fill="x")
        self.undo_btn = tk.Button(task_buttons, text="Undo", command=self.undo_improvement, state="disabled")
        self.undo_btn.pack(fill="x", pady=(4, 0))
        self.save_skill_btn = tk.Button(task_buttons, text="Save as skill", command=self.save_task_as_skill,
                                        state="disabled")
        self.save_skill_btn.pack(fill="x", pady=(4, 0))
        tk.Button(task_buttons, text="Skills", command=self.open_skills_window).pack(fill="x", pady=(4, 0))
        self.task.edit_modified(False)
        self.task.bind("<<Modified>>", self._task_modified)

        bar = tk.Frame(root)
        bar.pack(fill="x", padx=10, pady=6)
        self.run_btn = tk.Button(bar, text="Run", width=12, command=self.start)
        self.run_btn.pack(side="left")
        tk.Button(bar, text="Stop", width=12, command=self.stop.set).pack(side="left", padx=6)
        self.open_report_btn = tk.Button(bar, text="Open report in Notepad", command=self.open_latest_report,
                                         state="disabled")
        self.open_report_btn.pack(side="left", padx=6)
        tk.Button(bar, text="Open reports folder", command=self.open_reports_folder).pack(side="left", padx=6)
        tk.Button(bar, text="Open screenshots folder", command=self.open_screenshots_folder).pack(side="left", padx=6)
        tk.Button(bar, text="Clear screenshots", command=self.confirm_clear_screenshots).pack(side="left", padx=6)
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

    def _render_plan_checklist(self, goal, steps, completed_ids=(), note=""):
        self.current_steps = steps
        self.plan_goal.configure(text="Goal: " + goal + (" (" + note + ")" if note else ""))
        for child in self.plan_list.winfo_children():
            child.destroy()
        self.plan_rows.clear()
        completed_ids = set(completed_ids)
        for step in steps:
            sid = step.get("id")
            marker = "✓" if sid in completed_ids else "○"
            label = tk.Label(self.plan_list, text=f"{marker}  {step.get('title', '')}", anchor="w")
            label.pack(fill="x")
            self.plan_rows[sid] = label

    def _render_event(self, event):
        kind = event.get("type", "event")
        stamp = event.get("time") or datetime.now().astimezone().isoformat(timespec="seconds")
        if kind == "plan":
            note = event.get("note", "") if event.get("based_on_screen") else ""
            self._render_plan_checklist(event.get("goal", ""), event.get("steps", []), note=note)
            self._append(stamp, "Plan created: " + event.get("goal", "") + (" — " + note if note else ""))
            return
        if kind == "skill_loaded":
            self._append(stamp, f"Skill loaded: @{event.get('name', '')}")
            return
        if kind == "skill_save_offered":
            self._skill_candidate = event.get("candidate")
            self.save_skill_btn.configure(state="normal" if self._skill_candidate else "disabled")
            self._append(stamp, "Save as skill is available for this clean success.")
            if event.get("secrets_redacted"):
                self._append(stamp, "WARNING: Credential-like text was replaced with skill slots.")
                messagebox.showwarning("Skill secrets", "Credential-like text was replaced with slots before saving.",
                                       parent=self.root)
            return
        if kind == "skill_saved":
            self._append(stamp, f"Skill saved: @{event.get('name', '')}")
            self._skill_candidate = None
            self.save_skill_btn.configure(state="disabled")
            self._refresh_skills_tree()
            return
        if kind == "replan":
            self._render_plan_checklist(event.get("goal", ""), event.get("steps", []),
                                        event.get("completed_ids", []))
            self._append(stamp, f"Re-plan {event.get('number', 0)}: {event.get('reason', '')}")
        elif kind == "vision_enabled":
            mode = event.get("mode", "auto")
            self._append(stamp, f"Vision: ON ({mode}) because: {event.get('reason', '')}")
            if mode == "auto":
                self.root.iconify()
        elif kind == "screen":
            self._append(stamp, "Screen: " + event.get("text", ""))
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
        elif kind == "search":
            self._append(stamp, f"Search: {event.get('query', '')} ({event.get('result_count', 0)} results)")
        elif kind == "page_read":
            self._append(stamp, f"Page read: {event.get('url', '')} ({event.get('chars', 0)} chars)")
        elif kind == "note_saved":
            self._append(stamp, f"Note saved ({event.get('count', 0)}): {event.get('source', '')}")
        elif kind == "report_saved":
            self.latest_report_path = event.get("file_path", "")
            self.open_report_btn.configure(state="normal" if self.latest_report_path else "disabled")
            self._append(stamp, "Report saved: " + self.latest_report_path)
        elif kind == "screenshot_saved":
            self._append(stamp, "Screenshot saved: " + event.get("file_path", ""))
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
                         f" | Searches {event.get('searches', 0)}" +
                         f" | Pages read {event.get('pages_read', 0)}" +
                         f" | Notes saved {event.get('notes_saved', 0)}" +
                         f" | Report {event.get('report_path', '')}" +
                         f" | Vision {event.get('vision_used', 'no')}" +
                         f" | Re-plans {event.get('replans', 0)}" +
                         f" | Total API calls {event.get('total_api_calls', event.get('api_calls', 0))}" +
                         f" | Skill {'@' + event.get('skill_name', '') if event.get('skill_name') else 'none'}" +
                         f" | {event.get('seconds', 0)} seconds")
            if self._skills_window and self._skills_window.winfo_exists():
                self._refresh_skills_tree()

    def _set_step(self, step_id, state):
        label = self.plan_rows.get(step_id)
        if not label:
            return
        step = next((item for item in getattr(self, "current_steps", []) if item.get("id") == step_id), None)
        title = step.get("title", "") if step else label.cget("text").split("  ", 1)[-1]
        marker = {"pending": "○", "running": "▶", "done": "✓"}[state]
        label.configure(text=f"{marker}  {title}")

    def _task_text(self):
        return self.task.get("1.0", "end-1c")

    def _replace_task_text(self, text):
        self._setting_task_text = True
        try:
            self.task.delete("1.0", "end")
            self.task.insert("1.0", text)
            self.task.edit_modified(False)
        finally:
            self._setting_task_text = False

    def _clear_undo(self):
        self._undo_original = None
        if hasattr(self, "undo_btn"):
            self.undo_btn.configure(state="disabled")

    def _task_modified(self, _event=None):
        if self.task.edit_modified():
            self.task.edit_modified(False)
            if not self._setting_task_text:
                self._clear_undo()

    def undo_improvement(self):
        if self._undo_original is None:
            return
        original = self._undo_original
        self._replace_task_text(original)
        self._clear_undo()

    def improve_task(self):
        original = self._task_text()
        if not original.strip() or self._improving:
            return
        settings = {"base": self.base.get().strip(), "key": self.key.get().strip(),
                    "model": self.model.get().strip()}
        self._improving = True
        self.improve_btn.configure(state="disabled")
        self.run_btn.configure(state="disabled")

        def worker():
            try:
                result = improve_prompt(original, settings)
                error = "" if result else "No suggestion was returned."
            except Exception as exc:
                result = None
                error = " ".join(str(exc).split())[:180] or "The improvement request failed."
            self.root.after(0, self._improve_finished, original, result, error)

        threading.Thread(target=worker, daemon=True).start()

    def _improve_finished(self, original, result, error):
        if error or not result:
            self._improving = False
            self.improve_btn.configure(state="normal")
            self.run_btn.configure(state="normal")
            messagebox.showwarning("Improve prompt", error or "The model returned no suggestion.", parent=self.root)
            return

        dialog = tk.Toplevel(self.root)
        dialog.title("Improved task wording")
        dialog.geometry("540x430")
        dialog.transient(self.root)

        def add_text_box(label_text, content):
            tk.Label(dialog, text=label_text).pack(anchor="w", padx=10, pady=(8, 2))
            box = tk.Text(dialog, height=4, wrap="word")
            box.pack(fill="x", padx=10)
            box.insert("1.0", content)
            box.configure(state="disabled")

        add_text_box("Original", original)
        add_text_box("Improved", result["improved"])
        tk.Label(dialog, text="Changes: " + result["changes"], anchor="w", wraplength=510,
                 justify="left").pack(fill="x", padx=10, pady=8)
        buttons = tk.Frame(dialog)
        buttons.pack(fill="x", padx=10, pady=8)

        def close_dialog(use_improved):
            if use_improved:
                if self._task_text() != original:
                    messagebox.showwarning("Improve prompt", "The task changed while improving; your current text was kept.",
                                           parent=dialog)
                else:
                    self._undo_original = original
                    self._replace_task_text(result["improved"])
                    self.undo_btn.configure(state="normal")
            dialog.destroy()
            self._improving = False
            self.improve_btn.configure(state="normal")
            self.run_btn.configure(state="normal")

        tk.Button(buttons, text="Use this", command=lambda: close_dialog(True)).pack(side="left")
        tk.Button(buttons, text="Keep mine", command=lambda: close_dialog(False)).pack(side="left", padx=6)
        dialog.protocol("WM_DELETE_WINDOW", lambda: close_dialog(False))
        dialog.grab_set()

    def save_task_as_skill(self):
        candidate = self._skill_candidate
        if not candidate:
            return
        self.save_skill_btn.configure(state="disabled")
        settings = {"base": self.base.get().strip(), "key": self.key.get().strip(),
                    "model": self.model.get().strip()}

        def worker():
            try:
                extracted = extract_skill(candidate, settings)
                error = ""
            except Exception as exc:
                extracted = None
                error = " ".join(str(exc).split())[:240] or "The skill could not be extracted."
            self.root.after(0, self._skill_extraction_finished, candidate, extracted, error)

        threading.Thread(target=worker, daemon=True).start()

    def _skill_extraction_finished(self, candidate, extracted, error):
        if error or not extracted:
            self.save_skill_btn.configure(state="normal" if self._skill_candidate else "disabled")
            messagebox.showerror("Save as skill", error or "The extractor returned no skill.", parent=self.root)
            return
        self._show_skill_preview(candidate, extracted)

    def _show_skill_preview(self, candidate, extracted):
        dialog = tk.Toplevel(self.root)
        dialog.title("Save reusable skill")
        dialog.geometry("600x520")
        dialog.transient(self.root)
        tk.Label(dialog, text="Skill name (shown as @name)").pack(anchor="w", padx=10, pady=(10, 2))
        name_var = tk.StringVar(value=suggest_skill_name(candidate.get("original_task", ""),
                                                         candidate.get("goal", "")))
        name_entry = tk.Entry(dialog, textvariable=name_var)
        name_entry.pack(fill="x", padx=10)
        preview_time = datetime.now().astimezone().isoformat(timespec="seconds")
        preview = {"name": "@" + name_var.get(), **extracted,
                   "created": preview_time, "updated": preview_time, "uses": 0, "last_used": None,
                   "original_task": candidate.get("original_task", ""),
                   "goal": candidate.get("goal", ""), "steps": candidate.get("steps", []),
                   "needs_vision": candidate.get("needs_vision", False)}
        preview_box = tk.Text(dialog, wrap="word")
        preview_box.pack(fill="both", expand=True, padx=10, pady=8)
        preview_box.insert("1.0", json.dumps(preview, ensure_ascii=False, indent=2))
        preview_box.configure(state="disabled")

        def refresh_preview_name(*_args):
            preview["name"] = "@" + name_var.get()
            preview_box.configure(state="normal")
            preview_box.delete("1.0", "end")
            preview_box.insert("1.0", json.dumps(preview, ensure_ascii=False, indent=2))
            preview_box.configure(state="disabled")

        name_var.trace_add("write", refresh_preview_name)
        buttons = tk.Frame(dialog)
        buttons.pack(fill="x", padx=10, pady=(0, 10))

        def save_preview():
            try:
                name = validate_skill_name(name_var.get())
            except ValueError as exc:
                messagebox.showerror("Invalid skill name", str(exc), parent=dialog)
                return
            name_var.set(name)
            if skill_exists(name, SKILLS_DIR) and not messagebox.askyesno(
                    "Overwrite skill?", f"@{name} already exists. Replace it?", parent=dialog):
                return
            skill = {"description": extracted["description"],
                     "variables": extracted["variables"], "trace": extracted["trace"],
                     "hints": extracted["hints"],
                     "original_task": candidate.get("original_task", ""),
                     "goal": candidate.get("goal", ""), "steps": candidate.get("steps", []),
                     "needs_vision": candidate.get("needs_vision", False)}
            skill, pre_save_redaction = redact_skill_secrets(skill)
            if pre_save_redaction:
                messagebox.showwarning("Skill secrets", "Credential-like text was replaced with slots before saving.",
                                       parent=dialog)
            try:
                _saved, secrets_redacted = save_skill(skill, name, SKILLS_DIR, overwrite=True)
            except Exception as exc:
                messagebox.showerror("Save as skill", "Could not save skill: " + str(exc), parent=dialog)
                return
            dialog.destroy()
            self.emit({"type": "skill_saved", "time": "", "name": name})
            if secrets_redacted:
                messagebox.showwarning("Skill secrets", "Credential-like text was replaced with slots.",
                                       parent=self.root)

        def cancel_preview():
            dialog.destroy()
            self.save_skill_btn.configure(state="normal" if self._skill_candidate else "disabled")

        tk.Button(buttons, text="Save", command=save_preview).pack(side="left")
        tk.Button(buttons, text="Cancel", command=cancel_preview).pack(side="left", padx=6)
        dialog.protocol("WM_DELETE_WINDOW", cancel_preview)
        dialog.grab_set()
        name_entry.focus_set()

    def open_skills_window(self):
        if self._skills_window and self._skills_window.winfo_exists():
            self._skills_window.lift()
            self._refresh_skills_tree()
            return
        window = tk.Toplevel(self.root)
        window.title("Saved skills")
        window.geometry("760x400")
        self._skills_window = window
        search_row = tk.Frame(window)
        search_row.pack(fill="x", padx=10, pady=8)
        tk.Label(search_row, text="Search:").pack(side="left")
        self._skill_search_var = tk.StringVar()
        search_entry = tk.Entry(search_row, textvariable=self._skill_search_var)
        search_entry.pack(side="left", fill="x", expand=True, padx=(6, 0))
        columns = ("name", "description", "uses", "last_used")
        self._skill_tree = ttk.Treeview(window, columns=columns, show="headings", selectmode="browse")
        for column, label, width in (("name", "Name", 130), ("description", "Description", 340),
                                     ("uses", "Uses", 60), ("last_used", "Last used", 150)):
            self._skill_tree.heading(column, text=label)
            self._skill_tree.column(column, width=width, anchor="w")
        self._skill_tree.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        buttons = tk.Frame(window)
        buttons.pack(fill="x", padx=10, pady=(0, 10))
        tk.Button(buttons, text="Use", command=self._use_selected_skill).pack(side="left")
        tk.Button(buttons, text="View", command=self._view_selected_skill).pack(side="left", padx=6)
        tk.Button(buttons, text="Edit description", command=self._edit_selected_skill).pack(side="left", padx=6)
        tk.Button(buttons, text="Delete", command=self._delete_selected_skill).pack(side="left", padx=6)
        self._skill_search_var.trace_add("write", lambda *_: self._refresh_skills_tree())
        window.protocol("WM_DELETE_WINDOW", self._close_skills_window)
        self._refresh_skills_tree()
        search_entry.focus_set()

    def _close_skills_window(self):
        if self._skills_window and self._skills_window.winfo_exists():
            self._skills_window.destroy()
        self._skills_window = None
        self._skill_tree = None

    def _refresh_skills_tree(self):
        tree = self._skill_tree
        if not tree or not tree.winfo_exists():
            return
        query = (self._skill_search_var.get() if self._skill_search_var else "").casefold()
        for row in tree.get_children():
            tree.delete(row)
        try:
            skills = list_skills(SKILLS_DIR)
        except OSError as exc:
            self._append(datetime.now().astimezone().isoformat(timespec="seconds"),
                         f"Could not read skills: {exc}")
            return
        for skill in skills:
            values = ("@" + skill["name"], skill["description"], skill["uses"], skill.get("last_used") or "Never")
            if query and query not in " ".join(str(value) for value in values).casefold():
                continue
            tree.insert("", "end", iid=skill["name"], values=values)

    def _selected_skill_name(self):
        if not self._skill_tree or not self._skill_tree.winfo_exists():
            return ""
        selected = self._skill_tree.selection()
        return selected[0] if selected else ""

    def _view_selected_skill(self):
        name = self._selected_skill_name()
        if not name:
            return
        skill = load_skill(name, SKILLS_DIR)
        dialog = tk.Toplevel(self._skills_window)
        dialog.title("View @" + name)
        dialog.geometry("600x500")
        box = tk.Text(dialog, wrap="word")
        box.pack(fill="both", expand=True, padx=10, pady=10)
        box.insert("1.0", json.dumps(skill, ensure_ascii=False, indent=2))
        box.configure(state="disabled")

    def _edit_selected_skill(self):
        name = self._selected_skill_name()
        if not name:
            return
        skill = load_skill(name, SKILLS_DIR)
        description = simpledialog.askstring("Edit description", f"Description for @{name}:",
                                             initialvalue=skill["description"], parent=self._skills_window)
        if description is None:
            return
        skill["description"] = description.strip()
        skill, pre_save_redaction = redact_skill_secrets(skill)
        if pre_save_redaction:
            messagebox.showwarning("Skill secrets", "Credential-like text was replaced with slots before saving.",
                                   parent=self._skills_window)
        try:
            _saved, redacted = save_skill(skill, name, SKILLS_DIR, overwrite=True)
        except Exception as exc:
            messagebox.showerror("Edit skill", str(exc), parent=self._skills_window)
            return
        self._refresh_skills_tree()
        if redacted:
            messagebox.showwarning("Skill secrets", "Credential-like text was replaced with slots.",
                                   parent=self._skills_window)

    def _delete_selected_skill(self):
        name = self._selected_skill_name()
        if not name or not messagebox.askyesno("Delete skill", f"Delete @{name}?", parent=self._skills_window):
            return
        delete_skill(name, SKILLS_DIR)
        self._refresh_skills_tree()

    def _use_selected_skill(self):
        name = self._selected_skill_name()
        if not name:
            return
        self.task.insert("1.0", f"@{name} ")
        self.task.focus_set()

    def open_latest_report(self):
        if not self.latest_report_path:
            messagebox.showinfo("Reports", "No report has been saved yet.", parent=self.root)
            return
        warning = open_report_in_notepad(self.latest_report_path)
        if warning:
            self.emit({"type": "warning", "time": "", "text": warning})

    def open_screenshots_folder(self):
        os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
        try:
            os.startfile(SCREENSHOTS_DIR)
        except Exception as exc:
            self.emit({"type": "warning", "time": "",
                       "text": f"Could not open screenshots folder: {exc}"})

    def confirm_clear_screenshots(self):
        if not messagebox.askyesno("Clear screenshots", "Delete all saved screenshot task folders?", parent=self.root):
            return
        removed = clear_screenshot_folders()
        messagebox.showinfo("Clear screenshots", f"Deleted {len(removed)} screenshot task folder(s).",
                            parent=self.root)

    def open_reports_folder(self):
        os.makedirs(REPORTS_DIR, exist_ok=True)
        try:
            os.startfile(REPORTS_DIR)
        except Exception as exc:
            self.emit({"type": "warning", "time": "",
                       "text": f"Could not open reports folder: {exc}"})

    def start(self):
        if self.remember.get():
            with open(CONFIG, "w", encoding="utf-8") as f:
                json.dump({"base": self.base.get(), "key": self.key.get(), "model": self.model.get(),
                           "browser": self.browser.get().strip(), "vision_mode": self.vision_mode.get(),
                           "batch_actions": self.batch_actions.get(),
                           "open_report_notepad": self.open_report_notepad.get()}, f)
        elif os.path.exists(CONFIG):
            os.remove(CONFIG)
        task = self.task.get("1.0", "end").strip()
        if not task or not self.model.get().strip():
            self.emit({"type": "warning", "time": "", "text": "Enter a model name and a task first."})
            return
        self._clear_undo()
        self._skill_candidate = None
        self.save_skill_btn.configure(state="disabled")
        self.stop.clear()
        self.run_btn.configure(state="disabled")
        self.improve_btn.configure(state="disabled")
        for child in self.plan_list.winfo_children():
            child.destroy()
        self.plan_rows.clear()
        self.current_steps = []
        self.plan_goal.configure(text="Planning…")
        settings = {"base": self.base.get().strip(), "key": self.key.get().strip(),
                    "model": self.model.get().strip(), "browser": self.browser.get().strip(),
                    "vision_mode": self.vision_mode.get(), "batch_actions": self.batch_actions.get(),
                    "open_report_notepad": self.open_report_notepad.get()}
        if settings["vision_mode"] == "on":
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
                if not self._improving:
                    self.improve_btn.configure(state="normal")
            self.root.after(0, restore)


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
