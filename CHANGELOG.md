# Changelog

- Split desktop automation orchestration/actions into `engine.py`; `agent.py` now owns the Tkinter UI and renders timestamped engine events.
- Added a spelling-tolerant planner with one invalid-plan retry, plan-aware action step IDs, and a live plan checklist.
- Added end-of-task summaries with goal, completed/total steps, API-call count, elapsed time, and outcome.
- Added optional non-vision batching (up to four actions), stop-on-failure behavior, per-action step tracking, and automatic post-open-app waiting; vision remains single-action.
- Added the default-on batch checkbox and saved preference, plus action/API/call-savings summary metrics.
- Added `web_search`, SSRF-protected `read_page`, per-task `save_note`, sourced Markdown reports, research events, and an Open Reports Folder button.
- Added research source/safety instructions and per-task search/page limits. `run.bat` installs `ddgs`, `requests`, and `trafilatura` alongside the existing packages.
- Added offline fake-client/fake-action and mocked HTTP/DNS research tests.
- Added asynchronous prompt improvement with confirmation and one-step Undo.
- Added Notepad report opening with default-app fallback and a saved auto-open preference.
- Vision screenshots are now saved per task, with click markers, seven-day cleanup, open/clear controls, and live events.
- Expanded offline tests for prompt improvement, Notepad fallback, screenshot storage, click markers, and cleanup.
- Added Off/Auto/On vision with legacy-config migration, screen-first planning, Auto escalation, bounded re-planning, and one-line screen memory.
- Added vision/re-plan logging and summary metrics, plus fake-client tests for vision modes, image fallback, planner/re-plan flow, screen events, and call counts.
- Added saved reusable skills with safe extraction/redaction, @skill guidance, usage tracking, and a searchable Skills manager.
- Added immediate, readable model-operation logs with five-second heartbeats, reply durations, and clear 90-second timeout warnings; checklist progress now completes skipped prior steps and finalizes correctly on finish, stop, or failure.
- Added workspace-first file-task routing, task-scoped saved-file tracking, and safe open_file handling for browser-rendered HTML and Notepad-opened text files.
