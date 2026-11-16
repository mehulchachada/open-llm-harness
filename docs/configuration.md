# Configuration

Everything a job is allowed to do is decided by one file on the machine that runs the models:
`config.json` in the local state directory. `llm init` writes a starter copy and prints where it
is. A complete example is in [`examples/config.example.json`](../examples/config.example.json).

The file is read at the start of every job, so changes apply to the next job without restarting
the dispatcher.

## Where things live

| What | Default | Override |
|---|---|---|
| Queue repo (jobs, runs, project notes) | `~/llm-harness` | `LLM_HARNESS_HOME` |
| Local state (`config.json`, locks, screenshots) | `<queue repo>-local`, next to it | `LLM_HARNESS_STATE` |

The state directory is kept out of the queue repo on purpose. Other machines can push job files
into the queue; nothing they push can change which folders a job may read or which commands it
may run.

## Top-level keys

| Key | Default | Meaning |
|---|---|---|
| `ollama` | `http://127.0.0.1:11434` | Ollama's address. |
| `default_model` | `""` | Model for queued jobs when the job does not name one. It should support tool calling. Vision and thinking are used when the model has them. |
| `sync_model` | `""` | Model for `llm ask` when nothing is loaded. Something small that loads fast is a good choice. Empty means the same as `default_model`. |
| `default_ctx` | `16384` | Context window for a job that does not ask for one. |
| `max_ctx` | `65536` | Upper limit for `--ctx`. Set it to what fits in your VRAM with the default model. |
| `think` | `"auto"` | Reasoning, on a model that has it: `on` every turn, `auto` only where the turn decides something, `off` never. A job overrides it with `--think`. |
| `think_level` | `""` | If the backend takes a reasoning level (`low`, `medium`, `high`), the level to ask for on a thinking turn. Empty sends a plain on/off. |
| `think_budget` | `0.10` | Share of the window one turn may generate before reasoning is switched off for the next few turns. `0` removes the ceiling. |
| `gate_baseline` | `true` | When a gate fails, re-run it on the files as they were before the job, so a gate that was already failing is reported rather than charged to the model. Costs nothing when the gates pass. |
| `searxng` | `http://127.0.0.1:8888` | SearXNG instance used by `web_search`. Only jobs with `web: true` touch it. It must have the JSON output format enabled. |
| `dashboard_url` | `""` | Optional live reporting, see [dashboard.md](dashboard.md). Empty means off. |
| `host_name` | `""` | This machine's name in job ids and commit messages. Empty means the hostname. |
| `projects` | `{}` | The projects jobs may work on. See below. |

No model name is built in. With `default_model` empty, a job runs on a model that is already
loaded if there is one, otherwise on the first installed model, and in both cases a model that
can call tools is preferred over one that cannot. That is fine for trying things out. Once you
have more than one model installed, name the one you want; `llm models` lists what is installed
and what each can do. A job can always ask for a specific model with `--model`.

## Reasoning

On a reasoning model, thinking is the largest thing a job generates, and the harness drops it after
the turn: it is already in the transcript, and sending it back is the biggest avoidable context
cost. That makes it worth spending deliberately rather than everywhere.

With `think: auto` the harness asks for reasoning on the turns that decide something - the first
turn, a turn that follows something arriving from outside (a file, a search, a page, a picture), a
failure to diagnose, and any turn the harness itself interrupted with a nudge, a pinned state or a
failed gate. A turn that only acknowledges the model's own successful write, or a check that passed,
gets none. There is also a floor, so a long run of mechanical turns still reasons occasionally.

`num_predict` is not a reliable ceiling on reasoning - a turn generating 38k tokens under a 13k cap
has been observed - so `think_budget` backs it up after the fact: a turn that generates more than
that share of the window costs the next few turns their reasoning. Without it, one runaway turn
tends to become a run of them.

`result.json` records `think` and `think_turns` for every job, so the setting can be compared
against your own work rather than taken on trust.

## Projects

A project gives jobs a `repo/` to look at. A job asks for it by name (`project: webapp`); the
path comes from here and nowhere else.

```json
"projects": {
  "webapp": {
    "path": "/home/me/src/webapp",
    "write_globs": ["client/src/*"],
    "checks": { ... },
    "gates": { ... },
    "preview": { ... }
  }
}
```

Only `path` is required. With just a path, jobs can read the tree and nothing more.

Standing notes for a project go in the queue repo at `projects/<name>/context/`. That folder is
mounted read-only as `shared/` in every job for the project. See
[`examples/project-overview.md`](../examples/project-overview.md) for what a good one looks like.

### `write_globs`

Paths inside the repo that a job with `edit: true` may create or change, relative to `path`.
Matching is Python's `fnmatch`, where `*` also crosses `/`, so `client/src/*` covers everything
below `client/src`. Without `edit: true` in the job, the repo stays read-only whatever is listed
here.

### `checks`

Commands the model may run itself through the `check` tool, by name. The model never gets a
shell; it can only pick a name from this list.

```json
"test": {
  "about": "run one vitest file; target = the test file path",
  "cmd": ["npx", "vitest", "run", "{target}"],
  "cwd": "client",
  "target_glob": "src/*",
  "timeout": 300
}
```

- `cmd` is an argument list, run without a shell. On Windows use the real file name
  (`npx.cmd`, not `npx`).
- `cwd` is relative to the project path.
- `about` is shown to the model in the tool description. Say whether the check takes a target.
- If any part of `cmd` contains `{target}`, the model must pass one. It may only contain
  letters, digits and `_ . / -`, no `..`, and has to match `target_glob`.
- `timeout` is in seconds (default 300). The last 6000 characters of output go back to the model.

### `gates`

Commands the harness runs itself when the model says it is finished and has changed files in
the repo. The model cannot skip them.

```json
"tests": {
  "cmd": ["npx", "vitest", "run", "--reporter=dot"],
  "cwd": "client",
  "timeout": 600,
  "when": ["client/*"]
}
```

- A gate with a `when` list runs only if a file the job changed matches one of the patterns.
  A gate without `when` always runs.
- A failing gate's output is handed back to the model to fix, up to twice. If it still fails,
  the job ends as `error: final gate failed`.
- A project with no gates still lets edit jobs finish, but the result says
  `final gate: none configured - UNVERIFIED`.
- With `gate_baseline` on, a gate that fails is re-run once on the files as they were before the
  job: the harness puts back the original versions of the files the job changed, runs only the
  failing gates, then restores the job's versions (kept in `<run-dir>/gate-swap/` until they are
  back, so a crash loses nothing). A gate that also fails there was red before the model arrived:
  it is not handed back and not counted against the job, the gate status reads `pre-failing`, and
  the Result section names it. A job whose gates pass pays nothing for this. If a changed file was
  too large to keep a copy of (over 1 MB), the swap is skipped and the failure counts as the job's.

Checks are for the model's own loop; gates are the verdict. It is normal for the same command to
appear in both.

### `preview`

Lets a vision model look at the running app. The harness opens a page in a throwaway headless
browser and shows the picture to the model.

| Key | Default | Meaning |
|---|---|---|
| `base` | required | Address of your dev server, e.g. `http://localhost:5173`. The model can only ask for paths under it. |
| `browser` | auto | Path to a Chromium-family browser. If unset, Edge, Chromium and Chrome are looked for in the usual places. |
| `width`, `height` | `1440`, `900` | Viewport. |
| `settle_ms` | `5000` | How long to let the page load before the picture is taken. |
| `theme_key` | none | localStorage key your app reads its theme from. Enables the tool's `theme` argument (`dark` / `light`). |
| `local_storage` | `{}` | Keys to set before the page loads, for example to skip an onboarding screen. |

The dev server has to be running already; the harness does not start it. Screenshots need
Node.js 22 or newer on the machine. Pictures are saved under `shots/<job id>/` in the state
directory, never in the project. A job may take 12.

## What is never readable

Whatever the config says, these are hidden from every job, in every mount:

- directories: `.git`, `secrets`, `.terraform`, `node_modules`, `__pycache__`, `.ssh`, `.aws`
- files: `*.env`, `.env*`, `*.tfvars`, `*.tfstate`, `*.tfstate.*`, `*.pem`, `*.key`, `*.pfx`,
  `*.p12`, `id_rsa*`, `id_ed25519*`, `*.kdbx`, `credentials*`

A file whose name ends in `.example` is exempt, so `.env.example` can be read. The list lives at
the top of `bin/llmjob.py` if you need to extend it.

This is a filter on file names, not a security boundary around the process. The model has no
shell, but the check and gate commands you configure run with your user's full rights. Only
list commands you would run yourself.

## Environment variables

| Variable | Used for |
|---|---|
| `LLM_HARNESS_HOME` | The queue repo. |
| `LLM_HARNESS_STATE` | The local state directory. |
| `LLM_HARNESS_DASHBOARD_URL` | Overrides `dashboard_url`. |
| `CLAUDE_CODE_SESSION_ID` | Set by Claude Code. If present when a job is submitted, the job records it as `parent_session`. `--no-parent` turns that off. |
