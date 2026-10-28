# Changelog

- Split desktop automation orchestration/actions into `engine.py`; `agent.py` now owns the Tkinter UI and renders timestamped engine events.
- Added a spelling-tolerant planner with one invalid-plan retry, plan-aware action step IDs, and a live plan checklist.
- Added end-of-task summaries with goal, completed/total steps, API-call count, elapsed time, and outcome.
- Added optional non-vision batching (up to four actions), stop-on-failure behavior, per-action step tracking, and automatic post-open-app waiting; vision remains single-action.
- Added the default-on batch checkbox and saved preference, plus action/API/call-savings summary metrics.
- Added offline fake-client/fake-action engine tests. `run.bat` remains unchanged.
