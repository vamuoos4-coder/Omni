"""Loopback-only FastAPI backend for the Omni web smoke-test UI."""
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import asyncio
import hmac
import json
import mimetypes
import os
from pathlib import Path
import platform
import secrets
import shutil
import subprocess
import threading
import time
import urllib.parse
import webbrowser

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response

import engine as default_engine

WEB_DIR = Path(__file__).resolve().parent / "web"


def _now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _safe_child(root, user_path, *, must_exist=True):
    root = Path(root).resolve()
    candidate = Path(user_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=must_exist)
        if os.path.commonpath((str(root), str(resolved))) != str(root):
            return None
    except (OSError, ValueError, RuntimeError):
        return None
    return resolved


def _settings_defaults():
    return {"base": "", "key": "", "model": "auto", "text_model": "auto",
            "vision_model": "auto", "browser": "", "vision_mode": "auto",
            "vision": False, "batch_actions": True, "open_report_notepad": True}


def _load_settings(path, engine_module):
    path = Path(path)
    loaded = False
    config = {}
    try:
        with path.open("r", encoding="utf-8") as source:
            config = json.load(source)
        if not isinstance(config, dict):
            config = {}
        else:
            loaded = True
    except (OSError, ValueError, TypeError):
        config = {}
    settings = _settings_defaults()
    settings.update({key: config[key] for key in settings if key in config})
    try:
        text_model, vision_model = engine_module.migrate_model_config(config)
    except Exception:
        text_model = str(config.get("text_model") or config.get("model") or "auto")
        vision_model = str(config.get("vision_model") or text_model)
    settings["text_model"] = text_model
    settings["vision_model"] = vision_model or text_model
    settings["model"] = text_model
    try:
        settings["vision_mode"] = engine_module.migrate_vision_mode(config)
    except Exception:
        if settings.get("vision_mode") not in ("off", "auto", "on"):
            settings["vision_mode"] = "on" if settings.get("vision") else "off"
    settings["key"] = str(config.get("key") or "")
    settings["vision"] = settings["vision_mode"] == "on"
    return settings, loaded


def _public_settings(settings):
    key = str(settings.get("key") or "")
    masked = "••••••" + key[-4:] if len(key) > 4 else ("••••••" if key else "")
    public = {name: value for name, value in settings.items() if name != "key"}
    public["key"] = masked
    public["has_key"] = bool(key)
    return public


def _masked_key(value):
    value = str(value or "").strip()
    return not value or value.startswith(("*", "•", "●"))


def create_app(*, port=8765, config_path=None, engine_module=None, web_dir=None,
               screenshots_dir=None, workspace_dir=None, window_mode="browser"):
    """Build an isolated app instance; injection points keep server tests fully fake/offline."""
    engine_module = engine_module or default_engine
    config_path = Path(config_path or (Path(__file__).resolve().parent / "config.json"))
    web_dir = Path(web_dir or WEB_DIR).resolve()
    screenshots_dir = Path(screenshots_dir or engine_module.SCREENSHOTS_DIR).resolve()
    workspace_dir = Path(workspace_dir or engine_module.APP_DIR).resolve()
    settings, settings_loaded = _load_settings(config_path, engine_module)

    @asynccontextmanager
    async def lifespan(_app):
        app.state.loop = asyncio.get_running_loop()
        app.state.event_lock = asyncio.Lock()
        yield

    app = FastAPI(title="Omni Agent local server", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.state.port = int(port)
    app.state.session_token = secrets.token_urlsafe(32)
    app.state.started_at = time.monotonic()
    app.state.window_mode = window_mode
    app.state.settings = settings
    app.state.settings_loaded = settings_loaded
    app.state.engine = engine_module
    app.state.config_path = config_path
    app.state.web_dir = web_dir
    app.state.screenshots_dir = screenshots_dir
    app.state.workspace_dir = workspace_dir
    app.state.event_history = deque(maxlen=500)
    app.state.event_seq = 0
    app.state.event_clients = set()
    app.state.event_lock = None
    app.state.loop = None
    app.state.running = False
    app.state.run_thread = None
    app.state.stop_event = None
    app.state.pending_asks = {}
    app.state.pending_asks_lock = threading.Lock()
    app.state.last_summary = None
    app.state.last_skill_candidate = None
    app.state.last_run_error = None

    def _valid_token(supplied):
        return isinstance(supplied, str) and hmac.compare_digest(supplied, app.state.session_token)

    def _host_valid(host):
        if not host:
            return False
        host = host.strip().lower()
        return host in {f"127.0.0.1:{app.state.port}", f"localhost:{app.state.port}"}

    def _origin_valid(origin, host):
        return not origin or origin == "http://" + host

    @app.middleware("http")
    async def _secure_http(request: Request, call_next):
        host = request.headers.get("host", "")
        if not _host_valid(host):
            return JSONResponse({"detail": "Host not allowed"}, status_code=403)
        origin = request.headers.get("origin")
        if request.method == "POST" and not _origin_valid(origin, host):
            return JSONResponse({"detail": "Origin not allowed"}, status_code=403)
        if request.url.path.startswith("/api/"):
            token = request.headers.get("x-token", "")
            if request.url.path == "/api/screenshot" and not token:
                token = request.query_params.get("t", "")
            if not _valid_token(token):
                return JSONResponse({"detail": "Invalid or missing session token"}, status_code=401)
        return await call_next(request)

    async def _publish(event):
        if not isinstance(event, dict):
            event = {"type": "warning", "text": "Engine emitted a non-object event."}
        kind = str(event.get("type") or "unknown")
        app.state.event_seq += 1
        envelope = dict(event)
        envelope["seq"] = app.state.event_seq
        envelope["type"] = kind
        envelope.setdefault("time", _now())
        app.state.event_history.append(envelope)
        if kind == "summary":
            app.state.last_summary = dict(event)
        elif kind == "skill_save_offered":
            candidate = event.get("candidate")
            app.state.last_skill_candidate = candidate if isinstance(candidate, dict) else None
        for queue in tuple(app.state.event_clients):
            try:
                queue.put_nowait(envelope)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(envelope)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
        return envelope

    def _emit_threadsafe(event):
        loop = app.state.loop
        if loop is None or loop.is_closed():
            return None
        return asyncio.run_coroutine_threadsafe(_publish(event), loop)

    def _engine_emit(event):
        if not isinstance(event, dict):
            event = {"type": "warning", "text": "Engine emitted a non-object event."}
        kind = str(event.get("type", ""))
        if kind in {"ask", "permission_asked", "permission_request", "question_asked"}:
            ask_id = str(event.get("id") or secrets.token_urlsafe(12))
            ask_kind = "permission" if kind in {"permission_asked", "permission_request"} else str(event.get("kind", "question"))
            if ask_kind not in {"permission", "question"}:
                ask_kind = "question"
            pending = {"ready": threading.Event(), "answer": None}
            with app.state.pending_asks_lock:
                app.state.pending_asks[ask_id] = pending
            ask_event = {"type": "ask", "id": ask_id, "kind": ask_kind,
                         "text": str(event.get("text", event.get("message", ""))),
                         "options": event.get("options", [])}
            future = _emit_threadsafe(ask_event)
            if future is not None:
                try:
                    future.result(timeout=5)
                except Exception:
                    pass
            while not pending["ready"].wait(0.1):
                if app.state.stop_event is not None and app.state.stop_event.is_set():
                    pending["answer"] = {"cancelled": True}
                    pending["ready"].set()
            with app.state.pending_asks_lock:
                app.state.pending_asks.pop(ask_id, None)
            return pending["answer"]
        return _emit_threadsafe(event)

    async def _finish_run(error=None):
        app.state.running = False
        app.state.last_run_error = error
        await _publish({"type": "run_finished", "time": _now(),
                        "error": error or "", "summary": app.state.last_summary})

    def _run_worker(task, run_settings, stop_event):
        error = None
        try:
            engine_module.run_task(task, run_settings, _engine_emit, stop_event)
        except Exception as exc:
            error = " ".join(str(exc).split())[:500] or exc.__class__.__name__
            _emit_threadsafe({"type": "error", "time": _now(), "text": "Task worker failed: " + error})
        finally:
            loop = app.state.loop
            if loop is not None and not loop.is_closed():
                asyncio.run_coroutine_threadsafe(_finish_run(error), loop)

    def _settings_for_run():
        result = dict(app.state.settings)
        result["workspace_dir"] = str(app.state.workspace_dir)
        return result

    @app.get("/")
    async def index():
        return await _static_file("index.html")

    async def _static_file(relative_path):
        path = _safe_child(app.state.web_dir, relative_path)
        if path is None or not path.is_file():
            return Response("Not found", status_code=404, media_type="text/plain")
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return FileResponse(path, media_type=media_type)

    @app.get("/api/health")
    async def health():
        return {"uptime_seconds": round(time.monotonic() - app.state.started_at, 2),
                "python_version": platform.python_version(), "window_mode": app.state.window_mode,
                "settings_loaded": bool(app.state.settings_loaded)}

    @app.get("/api/settings")
    async def get_settings():
        return _public_settings(app.state.settings)

    @app.post("/api/settings")
    async def post_settings(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"detail": "Expected a JSON object"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"detail": "Expected a JSON object"}, status_code=400)
        updated = dict(app.state.settings)
        allowed = {"base", "model", "text_model", "vision_model", "browser", "vision_mode",
                   "vision", "batch_actions", "open_report_notepad"}
        for key in allowed:
            if key in body:
                updated[key] = body[key]
        incoming_key = body.get("key")
        if not _masked_key(incoming_key):
            updated["key"] = str(incoming_key)
        if "text_model" not in body and "model" in body:
            updated["text_model"] = str(body["model"] or "auto")
        elif "text_model" in body:
            updated["text_model"] = str(body["text_model"] or "auto")
        if "model" not in body or "text_model" in body:
            updated["model"] = updated["text_model"]
        updated["vision_model"] = str(body.get("vision_model") or updated.get("vision_model") or
                                       updated.get("text_model") or "auto")
        mode = str(updated.get("vision_mode", "auto")).lower()
        if mode not in {"off", "auto", "on"}:
            return JSONResponse({"detail": "vision_mode must be off, auto, or on"}, status_code=422)
        updated["vision_mode"] = mode
        updated["vision"] = mode == "on"
        updated["base"] = str(updated.get("base") or "").strip()
        updated["browser"] = str(updated.get("browser") or "").strip()
        app.state.settings = updated
        try:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            serialized = dict(updated)
            temp_path = config_path.with_name(config_path.name + ".tmp")
            with temp_path.open("w", encoding="utf-8") as output:
                json.dump(serialized, output, ensure_ascii=False, indent=2)
                output.write("\n")
            os.replace(temp_path, config_path)
            app.state.settings_loaded = True
        except OSError as exc:
            return JSONResponse({"detail": f"Could not save settings: {exc}"}, status_code=500)
        return _public_settings(updated)

    @app.get("/api/models")
    async def get_models():
        base = str(app.state.settings.get("base") or "")
        if not base:
            return {"models": list(engine_module.MODEL_PRESETS), "warning": ""}
        options, warning = await asyncio.to_thread(
            engine_module.fetch_model_options, base, api_key=app.state.settings.get("key", ""))
        return {"models": options, "warning": warning}

    @app.post("/api/run")
    async def run(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"detail": "Expected a JSON object"}, status_code=400)
        task = body.get("task") if isinstance(body, dict) else None
        if not isinstance(task, str) or not task.strip():
            return JSONResponse({"detail": "task must be non-empty text"}, status_code=422)
        if app.state.running:
            return JSONResponse({"detail": "A task is already running"}, status_code=409)
        app.state.running = True
        app.state.last_skill_candidate = None
        app.state.last_run_error = None
        stop_event = threading.Event()
        app.state.stop_event = stop_event
        run_id = secrets.token_urlsafe(10)
        app.state.last_run_id = run_id
        await _publish({"type": "run_started", "time": _now(), "run_id": run_id, "task": task.strip()})
        thread = threading.Thread(target=_run_worker,
                                  args=(task.strip(), _settings_for_run(), stop_event),
                                  name="omni-engine-run", daemon=True)
        app.state.run_thread = thread
        thread.start()
        return JSONResponse({"running": True, "run_id": run_id}, status_code=202)

    @app.post("/api/stop")
    async def stop():
        if app.state.stop_event is not None and app.state.running:
            app.state.stop_event.set()
            with app.state.pending_asks_lock:
                pending = list(app.state.pending_asks.values())
            for ask in pending:
                ask["answer"] = {"cancelled": True}
                ask["ready"].set()
            return {"stopping": True}
        return {"stopping": False}

    @app.post("/api/answer")
    async def answer(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"detail": "Expected a JSON object"}, status_code=400)
        if not isinstance(body, dict) or not isinstance(body.get("id"), str) or "answer" not in body:
            return JSONResponse({"detail": "id and answer are required"}, status_code=422)
        with app.state.pending_asks_lock:
            ask = app.state.pending_asks.get(body["id"])
            if ask is None:
                return JSONResponse({"detail": "Unknown or expired ask id"}, status_code=404)
            ask["answer"] = body["answer"]
            ask["ready"].set()
        await _publish({"type": "permission_answered", "time": _now(), "id": body["id"],
                        "answer": body["answer"]})
        return {"accepted": True}

    @app.get("/api/status")
    async def status():
        return {"running": bool(app.state.running), "last_summary": app.state.last_summary,
                "can_save_skill": isinstance(app.state.last_skill_candidate, dict)}

    @app.get("/api/skills")
    async def skills_list():
        try:
            return {"skills": await asyncio.to_thread(engine_module.list_skills,
                                                        str(engine_module.SKILLS_DIR))}
        except Exception as exc:
            return JSONResponse({"detail": str(exc)}, status_code=500)

    @app.get("/api/skills/{name}")
    async def skills_get(name: str):
        try:
            skill = await asyncio.to_thread(engine_module.load_skill, name, str(engine_module.SKILLS_DIR))
        except FileNotFoundError:
            return JSONResponse({"detail": "Skill not found"}, status_code=404)
        except (ValueError, OSError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=422)
        return skill

    @app.delete("/api/skills/{name}")
    async def skills_delete(name: str):
        try:
            deleted = await asyncio.to_thread(engine_module.delete_skill, name, str(engine_module.SKILLS_DIR))
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=422)
        if not deleted:
            return JSONResponse({"detail": "Skill not found"}, status_code=404)
        return {"deleted": True, "name": name}

    @app.patch("/api/skills/{name}")
    async def skills_patch(name: str, request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"detail": "Expected a JSON object"}, status_code=400)
        if not isinstance(body, dict) or not isinstance(body.get("description"), str):
            return JSONResponse({"detail": "description must be text"}, status_code=422)
        try:
            skill = await asyncio.to_thread(engine_module.load_skill, name, str(engine_module.SKILLS_DIR))
            skill["description"] = body["description"].strip()
            saved, redacted = await asyncio.to_thread(
                engine_module.save_skill, skill, name, str(engine_module.SKILLS_DIR), overwrite=True)
        except FileNotFoundError:
            return JSONResponse({"detail": "Skill not found"}, status_code=404)
        except (ValueError, OSError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=422)
        return {"skill": saved, "secrets_redacted": bool(redacted)}

    @app.post("/api/skills/save-from-last-run")
    async def save_skill_from_last_run(request: Request):
        if app.state.running:
            return JSONResponse({"detail": "Wait for the current run to finish"}, status_code=409)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"detail": "Expected a JSON object"}, status_code=400)
        if not isinstance(body, dict) or not isinstance(body.get("name"), str):
            return JSONResponse({"detail": "name is required"}, status_code=422)
        candidate = app.state.last_skill_candidate
        if not isinstance(candidate, dict):
            return JSONResponse({"detail": "There is no successful run to save"}, status_code=409)
        name = body["name"].strip()
        overwrite = body.get("overwrite") is True
        extraction_input = dict(candidate)
        settings_for_extract = dict(app.state.settings)
        try:
            extracted = await asyncio.to_thread(engine_module.extract_skill,
                                                extraction_input, settings_for_extract)
            if not isinstance(extracted, dict):
                return JSONResponse({"detail": "Skill extraction returned no skill"}, status_code=422)
            skill = dict(extracted)
            skill.update({"name": name, "original_task": candidate.get("original_task", ""),
                          "goal": candidate.get("goal", ""), "steps": candidate.get("steps", []),
                          "needs_vision": candidate.get("needs_vision") is True})
            saved, redacted = await asyncio.to_thread(
                engine_module.save_skill, skill, name, str(engine_module.SKILLS_DIR), overwrite=overwrite)
        except FileExistsError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=409)
        except (ValueError, OSError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=422)
        except Exception as exc:
            return JSONResponse({"detail": str(exc)}, status_code=502)
        app.state.last_skill_candidate = None
        await _publish({"type": "skill_saved", "time": _now(), "name": name,
                        "secrets_redacted": bool(redacted)})
        return {"skill": saved, "secrets_redacted": bool(redacted)}

    @app.post("/api/improve")
    async def improve(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"detail": "Expected a JSON object"}, status_code=400)
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str):
            return JSONResponse({"detail": "text must be a string"}, status_code=422)
        try:
            result = await asyncio.to_thread(engine_module.improve_prompt, text, dict(app.state.settings))
        except Exception as exc:
            return JSONResponse({"detail": " ".join(str(exc).split())[:400]}, status_code=502)
        return {"improved": result}

    @app.post("/api/open")
    async def open_local(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"detail": "Expected a JSON object"}, status_code=400)
        what = body.get("what") if isinstance(body, dict) else None
        if what not in {"report", "reports_folder", "screenshots_folder", "workspace_folder", "workspace_vscode"}:
            return JSONResponse({"detail": "Unsupported open target"}, status_code=422)
        try:
            if what == "report":
                reports = Path(engine_module.REPORTS_DIR)
                files = sorted(reports.glob("*.md"), key=lambda item: item.stat().st_mtime, reverse=True)
                if not files:
                    return JSONResponse({"detail": "No report is available"}, status_code=404)
                warning = engine_module.open_report_in_notepad(str(files[0]))
                if warning:
                    return JSONResponse({"detail": warning}, status_code=500)
                path = str(files[0])
            elif what == "reports_folder":
                path = str(engine_module.REPORTS_DIR)
            elif what == "screenshots_folder":
                path = str(engine_module.SCREENSHOTS_DIR)
            else:
                path = str(app.state.workspace_dir)
            if what in {"reports_folder", "screenshots_folder", "workspace_folder"}:
                Path(path).mkdir(parents=True, exist_ok=True)
                if hasattr(os, "startfile"):
                    os.startfile(path)
                else:
                    webbrowser.open(Path(path).resolve().as_uri())
            elif what == "workspace_vscode":
                subprocess.Popen(["code", str(app.state.workspace_dir)])
            return {"opened": what, "path": path}
        except Exception as exc:
            return JSONResponse({"detail": " ".join(str(exc).split())[:400]}, status_code=500)

    @app.get("/api/screenshot")
    async def screenshot(request: Request, path: str):
        allowed = _safe_child(app.state.screenshots_dir, path)
        if allowed is None:
            return JSONResponse({"detail": "Screenshot path is outside the screenshots folder"}, status_code=403)
        if not allowed.is_file():
            return JSONResponse({"detail": "Screenshot not found"}, status_code=404)
        return FileResponse(allowed, media_type=mimetypes.guess_type(allowed.name)[0] or "image/jpeg")

    @app.get("/{asset_path:path}")
    async def static_assets(asset_path: str):
        if asset_path.startswith("api/") or asset_path == "ws":
            return Response("Not found", status_code=404)
        return await _static_file(asset_path)

    @app.websocket("/ws")
    async def websocket_events(websocket: WebSocket):
        host = websocket.headers.get("host", "")
        if not _host_valid(host):
            await websocket.close(code=4403, reason="Host not allowed")
            return
        origin = websocket.headers.get("origin")
        if not _origin_valid(origin, host):
            await websocket.close(code=4403, reason="Origin not allowed")
            return
        if not _valid_token(websocket.query_params.get("t", "")):
            await websocket.close(code=4401, reason="Invalid or missing session token")
            return
        await websocket.accept()
        queue = asyncio.Queue(maxsize=600)
        async with app.state.event_lock:
            replay = list(app.state.event_history)
            app.state.event_clients.add(queue)
        try:
            for event in replay:
                await websocket.send_json(event)
            await _publish({"type": "connected", "time": _now(), "replayed": len(replay)})
            while True:
                event = await queue.get()
                await websocket.send_json(event)
        except WebSocketDisconnect:
            pass
        finally:
            app.state.event_clients.discard(queue)

    return app


app = create_app()
