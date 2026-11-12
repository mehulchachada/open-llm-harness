<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg">
    <img src="assets/logo-light.svg" alt="open-llm-harness" width="480">
  </picture>
</p>

A small harness for getting real work out of a model running on your own GPU. You queue a job in
plain words, a local [Ollama](https://ollama.com) model works on it inside a sandbox, and you get
back an answer, a report or a patch, together with a complete record of how the model got there.

It is not built around one model. Anything Ollama can serve will run: the harness asks Ollama
what the model can do (tool calling, vision, thinking) and works with what is there.

It is about 2,000 lines of Python with no dependencies beyond the standard library.

## Why this exists

A model small enough to run on a consumer GPU can read code, write reports and make focused
edits. Left alone in a loop, it also does a few predictable things:

- it reads until its context window is full and then has no room left to write the answer;
- it re-reads the same files in circles after old output scrolls out of the window;
- it says "done, all tests pass" when it never ran the tests.

The harness is built around those failures. Reads are metered against the real token count, so
the model is told to stop reading while it can still write. When old output is dropped, the
harness pins a short state summary so the model does not go looking for it again. And "done" is
not the model's call: when it claims to be finished, the harness runs your project's tests
itself and hands back the failures.

It works on its own. It works better with a reviewer: you, or a stronger model such as a Claude
Code session, writing the brief and checking the result. The local model does the legwork and
its output is treated as a draft. That orchestrator is optional and nothing here depends on it.

## How it works

```
  you, a script, or an            the machine with the GPU
  orchestrating model
                                   +-------------+     +----------+     +--------+
  llm submit "..."  ----------->   | jobs/inbox  | --> | dispatch | --> | Ollama |
  llm-remote ... (over SSH)        +-------------+     +----------+     +--------+
                                                            |               ^
                                                            v               | tool calls
                                   +-------------+     +----------+         |
  llm log / wait / get  <-------   | jobs/done   | <-- | sandbox: context/ shared/ repo/ artifacts/
                                   | runs/<id>/  |     +----------------------------------------+
                                   +-------------+
```

The queue is an ordinary git repository. A job is a Markdown file. Submitting it is a commit,
the dispatcher claiming it is a commit, and the result is a commit, so the history of the queue
is the history of the work. One dispatcher runs one job at a time, because one GPU holds one
large model.

Every model turn is appended to `runs/<id>/transcript.jsonl` before the next one starts. You can
watch a job live, and if the machine dies mid-job the record up to that point is intact.

## What you need

- Python 3 (developed and tested on 3.12) and git
- [Ollama](https://ollama.com), with at least one model that supports tool calling
- Optional: Node.js 22+ and a Chromium-family browser, only for the screenshot tool
- Optional: a [SearXNG](https://docs.searxng.org) instance, only for jobs that search the web

It was developed on Windows. The tests also pass on Linux (Ubuntu 24.04), and CI runs them on
both. macOS is untested, and it has had little real use outside Windows so far, so expect to
find a rough edge there.

## Getting started

```
git clone https://github.com/mehulchachada/open-llm-harness
```

Put `open-llm-harness/bin` on your PATH. Then:

```
llm init
```

This creates the queue (`~/llm-harness`) and a `config.json` next to it, and prints both paths.
You do not have to edit the config to start: with no model named, jobs run on an installed model
that can call tools. Once you have several, set `default_model` to the one you want (`llm models`
lists them). The rest of the file is covered in [docs/configuration.md](docs/configuration.md).

Start the dispatcher in a terminal of its own and leave it open:

```
llm-dispatch
```

Now ask something:

```
llm ask "What does git rebase --autostash do? Two sentences."

llm submit --return report "Explain the difference between a mutex and a semaphore. Write it to artifacts/answer.md"
llm wait <the id it printed>
```

Nothing runs unless the dispatcher is running. `llm ps` tells you whether it is.

The command is called `llm`. If that name is already taken on your machine, run
`python bin/llm.py` and `python bin/dispatch.py` directly, or alias them to something else.

## Commands

```
llm ask "question"        instant answer, streamed. Reuses whatever model is already loaded,
                          so a quick question never forces a model swap under a running job
llm submit [opts] "..."   queue a job, print its id
llm status <id>           queued (and position), running (time, turn), or done
llm log <id> -f           watch the model work, live
llm wait <id>             block until done, print the result
llm get <id> [name]       list a job's output files, or print one
llm cancel <id>           remove a queued job, or stop a running one
llm lint brief.md         check a coding brief for the usual gaps
llm ps                    loaded model, dispatcher state, queue
llm models                installed models and what each can do
llm-dispatch              run the dispatcher (--once drains the queue and exits)
```

`llm ask` is for a quick opinion on text you can paste. It has no tools and sees no files. A job
is for anything that has to look at a project, takes more than one step, or should leave a record.

## A job file

`llm submit` builds this for you from its flags. You can also write it by hand and pipe it in
(`llm submit - < job.md`), or drop it into `jobs/inbox/` and push.

```
---
project: webapp          # optional: mounts shared/ and repo/
model: your-model:tag    # optional: any installed model
ctx: 32768               # optional, tokens
web: true                # optional: adds web_search and web_fetch
edit: true               # optional: edit rights on the project's writable paths, plus its checks
max_turns: 80            # optional (default 30, max 300)
group: search-filters    # optional: the larger piece of work this job belongs to
step: 2/3                # optional: which step of the group
return: report           # answer | report | patch | pr
---

What you want done, in plain words. Say what the deliverable is and where to write it.
```

`return: patch` and `return: pr` do not make anything open a pull request. They record what you
expect. The model's output is a draft, and turning it into a reviewed patch is your step.

## What a job can touch

The model never sees the real filesystem and never gets a shell. It gets four virtual folders
and a handful of tools (`list_dir`, `read_file`, `search`, `write_file`, and for coding jobs
`edit_file` and `check`).

| Folder | What it is | Access |
|---|---|---|
| `context/` | Evidence for this one job: files you attached | read |
| `shared/` | Standing notes for the project, from `projects/<name>/context/` in the queue | read |
| `repo/` | The project's source tree, if `config.json` maps the project to a path | read; write only where the config allows, and only if the job has `edit: true` |
| `artifacts/` | Where the model puts its deliverables | write |

Three things keep that honest:

- A job file cannot name a path. It names a project, and the path for that project is in
  `config.json` on the GPU machine, outside the queue. A job pushed from another machine cannot
  point the model at a folder you did not offer.
- Secrets are filtered out of every folder: `.git`, `.ssh`, `.env` files, keys, Terraform state
  and similar. The full list is in [docs/configuration.md](docs/configuration.md).
- Everything the model reads, whether job text, files or web pages, is treated as data. The
  system prompt says so, and `web_fetch` refuses private and local network addresses.

This is a set of guard rails for a cooperative model, not a security sandbox. The check commands
you configure run as you.

## The context budget

The window is the scarce resource, so the harness accounts for it in real tokens. After every
turn it takes the prompt size Ollama reports and recalibrates its characters-per-token estimate.
(For code and YAML that ratio is around 2.5, well below the 3.5-4 people assume from prose.)

- At 50% full, the bodies of old tool results are removed from the conversation. The six most
  recent are always kept, and the full text stays in the transcript.
- At 70%, reading and searching are switched off and the model is told to finish with what it has.
- A single read can never push the window past 75%. It is cut short instead.
- One turn may generate at most a fifth of the window. A model that spends a whole turn
  reasoning without acting is cut off and retried with reasoning switched off.

A job that still runs out of room ends as `status: error` with the reason. It does not come back
as an empty "ok".

The default window is 16k tokens. Ask for more per job with `--ctx`, up to `max_ctx` in the config.

## Coding jobs

With `edit: true`, the job may change files inside the project's `write_globs` and run the
project's named checks. All of that is configured per project in `config.json`, never in the job
file. Around the model, the harness does the following:

**The final gate.** When the model says it is done and it has changed files, the harness runs
the project's gate commands itself: typecheck, tests, formatting, whatever you listed. A failure
goes back to the model to fix, twice. After that the job is `error: final gate failed`, whatever
the model claimed. A project with no gates gets `final gate: none configured - UNVERIFIED` in
the result, so you can tell the difference.

**Pinned state.** Each time old output is removed, the model is shown its own
`artifacts/NOTES.md`, the list of files it has changed and the last result of each check. The
harness builds that summary, so it is correct even when the notes are stale.

**A loop detector.** Fifteen reads or searches in a row with no edit and no check gets a warning
that includes the pinned state. After three warnings without progress, the job is stopped.

**A patch of exactly what the job did.** The harness remembers what each file looked like before
the job first wrote to it and saves the difference as `runs/<id>/artifacts/changes.patch`. It is
the job's own delta, not a diff against git, so uncommitted work you already had in the tree is
not mixed in. Nothing is committed to your project.

**Two warnings for the reviewer.** `late rewrite` means an existing file of 30+ lines had 40% or
more of it replaced in the job's last five turns, which is how a working version gets swapped
for an untested idea just before "done". `edited after the model's last check` means what it
says.

**Screenshots.** If the project has a `preview` block and the model can see, it can open a page
of your local dev server in a throwaway headless browser and look at it. Layout problems do not
show up in tests.

**A scorecard.** `runs/<id>/result.json` records time, turns, tokens in and out, tool calls,
checks, files changed, gate status and whether the loop detector fired.

## Writing a brief that works

A one-line request is fine for a question. For a code change it is not. Start from
[`templates/coding-brief.md`](templates/coding-brief.md). The short version of what it encodes:

- One layer per job (logic, then UI, then wiring), in dependency order.
- Name the exact files. For a very large file, give the line each edit goes next to and tell the
  model not to read it whole.
- Put everything the model needs in the brief. A spec it has to go and read gets re-read after
  every context trim.
- End with a done condition: which checks must pass and what the summary must say.
- Decide; do not offer options. A local model follows the brief literally, mistakes included.

`llm submit` prints warnings when an edit job is missing these, and `llm lint brief.md` runs the
same check without queueing anything. It never blocks.

## From other machines

The GPU sits in one box; you are usually somewhere else. [`client/`](client/README.md) has a
single bash script, `llm-remote`, that runs every command above over SSH and can attach files
from the machine you are on:

```
llm-remote run --project webapp --file ./error.log "Why does this request fail? Write the cause to artifacts/cause.md"
```

A client needs `ssh` and `bash` and nothing else.

## With an orchestrating model

A good way to use this is with a stronger model in charge. A coding agent such as Claude Code
plans the work, splits it into briefs, sends them to the local model and reviews what comes
back. A few things exist for that case:

- A job submitted from inside a Claude Code session records that session's id, so runs can be
  traced back to the conversation that started them.
- `group` and `step` label a job as one part of a larger task.
- By convention the reviewer writes its verdict to `runs/<id>/review.md` next to the transcript.
- Jobs can report live to a dashboard. This is off by default; see [docs/dashboard.md](docs/dashboard.md).

None of this is specific to Claude. Anything that can run a command and read its output can
drive the queue.

## What is in the queue

```
jobs/inbox/      submitted, waiting
jobs/running/    claimed by the dispatcher
jobs/done/       finished: the original job plus a Result section
runs/<id>/       transcript.jsonl, context/ (evidence in), artifacts/ (deliverables out), result.json
runs/sync/       a daily log of `llm ask` calls
projects/<p>/    context/ = standing notes mounted into every job for project <p>
notes/           yours; the harness does not touch it
```

The queue holds your prompts, the model's reasoning and pieces of whatever files it read. Keep
it private. It is a separate repository from this code for exactly that reason.

## Tests

Both run offline, with no GPU and no model:

```
python tests/selftest_harness.py     # the job loop against a scripted fake model
python tests/e2e_fake_ollama.py      # the commands and the dispatcher against a fake Ollama server
```

## Where this is going

The aim is a harness that is useful with any local model, not one tuned for a single setup.
These are the plans. None of them is built yet.

- **More models, properly tested.** The thresholds and prompts have been tuned against a small
  set of models so far. The goal is sensible behaviour across model families and sizes, with
  per-model settings where one value does not fit all.
- **More backends.** Other model servers besides Ollama.
- **A GUI for configuration.** Projects, writable paths, checks and gates are edited by hand in
  `config.json` today.
- **Skills.** Reusable instructions a job can load when it needs them, instead of repeating
  them in every brief.
- **Agents.** Named roles with their own model, tools and limits, so a larger task can be
  split between them.
- **Memory.** A way to carry what one job learned into the next. Today the only carry-over is
  the `shared/` notes you write yourself.

Ideas and bug reports are welcome as issues.

## Limits worth knowing

- Ollama only, for now. The job loop talks to Ollama's `/api/chat` directly.
- One job at a time, by design.
- A model without tool support still works, but only single-shot: the attached files are pasted
  into the prompt and it answers once.
- Stopping a coding job does not undo the edits it had already made.
- A local model's output is a draft until someone has reviewed it. That applies most to what no
  check can verify: how a page looks, timing-dependent code, anything security-relevant.

## License

[MIT](LICENSE)
