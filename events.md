# Omni web event contract

## WebSocket envelope

Connect to `/ws?t=<session-token>`. Each JSON message has the server envelope fields below, followed by the event's fields. Server-assigned values take precedence if an engine field collides:

```json
{"seq":1,"type":"plan","time":"2026-10-28T12:00:00+00:00","goal":"...","steps":[]}
```

- `seq`: monotonically increasing integer for this server process.
- `type`: event name.
- `time`: ISO-8601 timestamp. Engine timestamps are retained; server events supply their own.
- On connect, the server replays up to the most recent 500 events in sequence order, then emits `connected`.
- The stream carries each engine event with its other fields intact. Optional fields are omitted when not applicable. The event history is process-local and resets when the server restarts.

## Events emitted by `engine.run_task` today

Every engine event contains `type` and an ISO-8601 `time` before the server envelope is added.

| Type | Event fields | Meaning |
| --- | --- | --- |
| `log` | `text` | Progress, model-call, heartbeat, or model timing line. Timing lines can include `via <X-Routed-Via>` when supplied by the endpoint. |
| `plan` | `goal`, `steps`, `based_on_screen`, `note` | Initial plan; `steps` contains objects with integer `id` and `title`. |
| `replan` | `reason`, `number`, `goal`, `steps`, `completed_ids` | Revised plan; `completed_ids` contains completed step IDs. |
| `step_start` | `step_id` | A plan step became active. |
| `step_done` | `step_id` | A plan step completed. |
| `thought` | `text`; optional `step_id` | Model thought text, associated with a step when available. |
| `action` | `action`, `args`; optional `step_id`, `skipped` | An action is about to run or is marked skipped. `skipped` is true for actions not run. |
| `result` | `text`; optional `step_id`, `skipped` | Action result or a `NOT RUN` marker. |
| `batch` | `count` | Number of parsed actions in the model reply. |
| `screen` | `text`; optional `step`, `replan` | Current-screen description, optionally associated with an action index or re-plan number. |
| `vision_enabled` | `mode`, `reason`; optional `step` | Vision mode activation. `mode` is `on` or `auto`. |
| `warning` | `text` | Recoverable issue, retry, timeout, stop, or fallback notice. |
| `error` | `text` | Model initialization, API, or task error. |
| `screenshot_saved` | `file_path` | Screenshot or click-marked screenshot saved to disk. |
| `file_saved` | `path`, `how` | A created or saved workspace file was recorded. |
| `report_saved` | `file_path` | Research report saved to disk. |
| `skill_loaded` | `name` | A saved skill was selected for the task. |
| `skill_save_offered` | `candidate`, `secrets_redacted` | A clean successful run may be saved as a reusable skill. `candidate` contains the run data; `secrets_redacted` indicates redaction. |
| `summary` | `goal`, `steps_completed`, `steps_total`, `actions_executed`, `api_calls`, `total_api_calls`, `estimated_calls_saved`, `searches`, `pages_read`, `notes_saved`, `report_path`, `vision_used`, `replans`, `skill_name`, `created_files`, `finished_by_finish`, `seconds`, `outcome` | Final run metrics and outcome. `created_files` contains `{path, how}` entries. |

Model heartbeats and reply timing are `log` events, not separate event types. Search, page-read, note-save, file-edit, file-delete, file-open, and command-run activities currently appear through the generic `action` / `result` stream (or file/report events where noted); the engine does **not** currently emit separate `search`, `page_read`, `note_saved`, `file_written`, `file_edited`, `file_deleted`, `file_opened`, or `command_run` event types. The current engine also has no permission/question prompt emitter.

## Server-generated events

| Type | Event fields | Meaning |
| --- | --- | --- |
| `connected` | `replayed` | Sent after the client receives the replay snapshot; `replayed` is the number of historical events in that snapshot. |
| `run_started` | `run_id`, `task` | A task was accepted and its worker thread started. |
| `run_finished` | `error`, `summary` | The worker ended. `error` is an empty string on normal completion; `summary` is the latest engine summary or null. |
| `ask` | `id`, `kind`, `text`, `options` | Normalized question or permission request. `kind` is `question` or `permission`. POST `/api/answer` with `{ "id": ..., "answer": ... }` resolves the waiting engine callback. |
| `permission_answered` | `id`, `answer` | The server accepted an answer for a pending ask. |
| `skill_saved` | `name`, `secrets_redacted` | A last-run skill was extracted and saved through the skills API. |

The server adapts engine callback events named `ask`, `permission_asked`, `permission_request`, or `question_asked` into the normalized `ask` contract. Stop releases a waiting ask with `{ "cancelled": true }`. The current `run_task` implementation does not emit these prompt events; the adapter is in place without changing engine behavior.
