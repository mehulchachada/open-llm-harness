# Using the GPU machine from your other machines

One machine has the GPU and runs the models. Your laptop, a handheld, a second desktop: they do
not run anything. They send work over SSH with one script, `llm-remote`, and get the result back
as text, files or a patch.

A client needs `ssh` and `bash`. Nothing else is installed on it.

## Setup

1. The GPU machine must accept SSH logins from the client with a key, and `llm` must be on the
   PATH of the shell that SSH lands in. On Windows hosts that is usually PowerShell or cmd, which
   pick up `llm.cmd`; on a bash login it is the `llm` script. Both are in `bin/`.

2. Add the host to `~/.ssh/config` on the client. [`ssh_config.example`](ssh_config.example) has
   the block; the alias `llmhost` is what the script looks for by default.

3. Check the connection. You should see a `GPU:` line and a `Disp:` line:

       ssh llmhost "llm ps"

4. Install the script somewhere on your PATH:

       mkdir -p ~/.local/bin
       curl -fsSL https://raw.githubusercontent.com/mehulchachada/open-llm-harness/main/client/llm-remote -o ~/.local/bin/llm-remote
       chmod +x ~/.local/bin/llm-remote
       llm-remote ask "say hello in five words"

5. Optional: give the machine a short name. It shows up in job ids, so you can tell at a glance
   who sent what.

       export LLM_REMOTE_FROM=laptop

`LLM_REMOTE_HOST` changes the SSH alias. `LLM_REMOTE_SSH` replaces the whole ssh command, for
example `LLM_REMOTE_SSH="ssh -p 2222 me@box"`.

## Two ways to ask

| | Instant question | Queued job |
|---|---|---|
| Command | `llm-remote ask "..."` | `llm-remote submit ...` / `brief FILE` / `run ...` |
| What the model can do | Answer from the text you give it. No files, no tools, no web. | Read a project's files, search the web if allowed, write reports, edit code if allowed. |
| How the answer comes back | Printed as it is generated. | A result you fetch when the job is done. |
| Recorded on the GPU machine | Yes, in the day's log. | Yes: full transcript, result and files. |

Use `ask` for a quick opinion or a rewrite of text you can paste. Use a job for anything that
has to look at a project, takes more than one step, or should leave a record.

## Commands

| Command | What it does |
|---|---|
| `llm-remote ask "question"` | Instant answer. Pipe text in to include it: `cat notes.md \| llm-remote ask "summarise this"`. |
| `llm-remote submit [options] "what you want"` | Queue a job. Prints the job id. |
| `llm-remote brief FILE` | Queue a job written as a file (front matter plus text). Prints the job id. |
| `llm-remote run [options] "what you want"` | Queue a job, show its log live, then print the result. |
| `llm-remote status ID` | One line: queued (and how many are ahead), running (how long, which turn), or done (with the outcome). |
| `llm-remote log ID [-f]` | What the model has done so far, step by step. `-f` follows until the job ends. |
| `llm-remote wait ID` | Block until the job is done, then print the result. Gives up after an hour; the job keeps running. |
| `llm-remote get ID` | List the files the job produced. |
| `llm-remote get ID NAME > file` | Download one, for example `llm-remote get ID changes.patch > fix.patch`. |
| `llm-remote cancel ID` | Remove a queued job, or stop a running one. |
| `llm-remote ps` | Loaded model, whether the dispatcher is running, the queue, the last five finished jobs. |
| `llm-remote models` | Installed models and what each can do. |
| `llm-remote lint FILE` | Check a coding brief for common gaps without queueing it. |

Options for `submit` and `run`:

| Option | Meaning |
|---|---|
| `--project NAME` | Give the job access to that project on the GPU machine. Projects are defined there, in `config.json`. You cannot name a path from a client. |
| `--file PATH` | Send a file from this machine as evidence. Repeatable, 5 MB each. Up to four images (png, jpg, webp, 3 MB each) are shown to a vision model as pictures. |
| `--web` | Let the job search and read the web. |
| `--return answer\|report\|patch\|pr` | What you expect back. Default `answer`. |
| `--model NAME` | Use a model other than the default. |
| `--ctx N` | Context window in tokens. Ask for more when the job has to read a lot. |
| `--edit` | Coding job: allow edits inside the project's writable paths and run its checks. |
| `--max-turns N` | How many steps the model may take. Default 30; 60 to 120 is usual for coding; 300 at most. |
| `--group NAME` `--step 1/3` | Label the job as one step of a larger task. |

Without `--project`, a job sees only the files you send with `--file`.

    llm-remote ask "Explain what git rebase --autostash does, in three sentences"
    llm-remote run --project webapp --return report "List every route that has no test and write the list to artifacts/untested.md"
    llm-remote submit --file ./error.log --file ./screenshot.png "Why does this page fail to load? Say what evidence you would still need"

## What comes back

`llm-remote wait ID` prints the result: a status line, a summary of the work, and the model's
answer.

| You asked for | What comes back | What you do next |
|---|---|---|
| `--return answer` | The answer as text. | Read it. |
| `--return report` | One or more files plus a short summary. | `llm-remote get ID`, then `get ID NAME > file`. |
| A coding job (`--edit`) | The changes are made in the project folder on the GPU machine and are not committed. You also get `changes.patch` (exactly what the job changed) and the outcome of the project's final gate. | Fetch the patch, review it, and `git apply` it to your own clone if it is at the same state. |
| `--return patch` or `pr` | The same as a coding job. No pull request is opened for you. | Review the draft and open the pull request yourself. |

The status is one of:

- `ok`: finished. For a coding job, the harness ran the project's gate and it passed.
- `error`: with the reason. Ran out of steps (`max_turns`), ran out of context, `final gate
  failed`, or `stopped by the loop detector`. The transcript and any partial files are kept.
- `cancelled`: someone stopped it.
- `interrupted`: the dispatcher went down mid-job. Queue it again.

A coding job's result can carry two warnings above the answer. Read them before trusting the
change: `late rewrite` (a file was largely replaced in the job's last steps) and `edited after
the model's last check`.

Stopping a coding job does not undo edits it had already made, and it leaves no `changes.patch`.

## How long things take

One job runs at a time, because one GPU holds one large model. `llm-remote status ID` tells you
how many jobs are ahead of yours.

Times depend on the model and the GPU. As a rough guide, with a model of about 27B parameters
that fits entirely in the memory of one consumer GPU:

| Kind of work | Time |
|---|---|
| Instant question, model already loaded | about a second |
| Small question as a job | about 10 seconds |
| A focused coding step (a few files, exact brief) | 40 seconds to 3 minutes |
| A larger coding step (several files, tests) | 3 to 8 minutes |
| Open-ended design work | 10 to 14 minutes, and the least reliable |

Do not poll in a tight loop. `llm-remote wait ID` blocks; `llm-remote run` shows the work live.

## When something does not work

| What you see | What it means |
|---|---|
| `ssh: connect ... timed out` or `refused` | The GPU machine is off, or not reachable from the network you are on. |
| `Permission denied (publickey)` | The GPU machine does not have this machine's SSH key. |
| `llm: command not found` | `bin/` is not on the PATH of the shell SSH lands in on the GPU machine. |
| `URLError ... refused` from `ps` or `ask` | Ollama is not running over there. |
| `Disp: NOT RUNNING` in `ps` | The dispatcher is not running. Jobs can be queued but will wait. |
| `search backend unreachable` in a job | SearXNG is down. Start it, or run the job without `--web`. |
| `status: interrupted` | The GPU machine or its dispatcher restarted during your job. |

## Optional: the tunnel

Only needed if you want to talk to the GPU machine's Ollama API directly from the client, for
example to point another tool at it.

    ssh -N llmhost-tunnel      # then http://127.0.0.1:11434 on this machine is the remote Ollama

To keep it up permanently there is a systemd user unit ([`llm-tunnel.service`](llm-tunnel.service))
and a launchd agent ([`local.llm-tunnel.plist`](local.llm-tunnel.plist)). Install paths are in
the comments at the top of each file. Both are provided as written; neither has been run in
anger yet, so treat them as a starting point.

## Optional: clone the queue

The queue is a git repository. If the GPU machine was set up with `llm init --bare PATH`, a
client can clone that bare repository over SSH and read results as plain files (`jobs/done/ID.md`,
`runs/ID/`). A job file pushed into `jobs/inbox/` is picked up within about ten seconds.

`llm-remote` over SSH is the tested route. Cloning from a client is how the design is meant to
work but has seen little use so far.
