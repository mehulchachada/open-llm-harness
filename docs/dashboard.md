# Live reporting to a dashboard (optional)

A job always leaves a full transcript on disk, and `llm log ID -f` follows it live. That is enough
for most uses. If you also want jobs to show up in a web dashboard while they run, set
`dashboard_url` in `config.json` and the harness will post each step to it.

Nothing in this repository is the dashboard. The harness is the sending side only. The endpoint
shape below is the one used by a patched build of the Claude Code Agent Monitor dashboard that
this was developed against; that patch is not included here. Any service that accepts the same
requests will work.

Reporting can never hurt a job: each call has a 4 second timeout, errors are swallowed, and after
three failures in a row the harness stops trying for the rest of that job.

## Requests

Every request is `POST <dashboard_url>/api/hooks/external-run` with a JSON body. These fields are
on all of them:

| Field | Value |
|---|---|
| `run_id` | The job id. |
| `provider` | `"ollama"` |
| `origin` | The job's `from` (which machine submitted it). |
| `cwd` | The job's run directory. |
| `parent_session_id` | The orchestrating session, if the job has one. Otherwise absent. |
| `model`, `title`, `group`, `step` | Set from the first request on. `title` is the first line of the brief. |
| `event` | `start`, `tool` or `end`. |

Per event:

| `event` | Extra fields |
|---|---|
| `start` | `prompt` (the brief), `transcript` (one user message) |
| `tool` | `phase` (`pre` or `post`), `tool_name`, `tool_input`. On `post` also `tool_response` (first 4000 characters) and `is_error`. `transcript` carries the assistant message on the turn's first `pre`, and the tool result on `post`. |
| `end` | `status` (`ok` or `error`), `error`, `stats` (`wall_s`, `turns`, `input_tokens`, `cached_input_tokens`, `output_tokens`, `tool_calls`, `checks`), `transcript` (the final assistant message) |

`transcript` entries are message objects in the shape Claude Code writes to its own session
files (`type`, `timestamp`, `message.content` with `text`, `thinking`, `tool_use` and
`tool_result` blocks), so a viewer built for those can show a local model's run the same way.

`input_tokens` and `cached_input_tokens` are an estimate of what the same conversation would
have been billed as on a metered API: each turn re-sends the conversation, so the part that was
already sent counts as cached and only the growth counts as fresh.

## Response

The harness expects JSON back and uses three fields from it:

```json
{ "mode": "child", "session_id": "...", "agent_id": "..." }
```

`mode` is `child` when the dashboard filed the run under its parent session. The job result then
links to `<dashboard_url>/sessions/<session_id>?tab=agents`, otherwise to `?tab=conversation`.
