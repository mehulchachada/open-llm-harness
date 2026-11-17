#!/usr/bin/env python3
"""llm - front door to the local models on the machine that has the GPU.
Runs on that machine; other machines call it over SSH (see client/).

  llm init [--bare PATH]                               create the job queue and a starter config.json
  llm ask [-m MODEL] [--ctx N] [--think] QUESTION...   instant answer (sync lane), streamed
  llm ask -                                            question on stdin (use this over SSH)
  llm newid [--from NAME]                              reserve a job id
  llm put ID NAME                                      stdin -> runs/ID/context/NAME
  llm submit [-] [--id ID] [--from X] [--project P] [--model M] [--ctx N] [--web]
             [--return answer|report|patch|pr] [--parent SESSION | --no-parent]
             [--think on|auto|off] [--sampling PRESET] [ASK...]
                                                       queue an async job (`-`: job file on stdin)
  llm lint FILE                                        check a coding brief for the usual gaps (advice only)
  llm status ID                                        one line: queued (position) / running (time, turn) / done
  llm cancel ID                                        remove a queued job, or stop a running one
  llm get ID [NAME]                                    list a job's artifacts, or print one (e.g. changes.patch)
  llm wait ID [--timeout S]                            block until the job is done, print the result
  llm log ID [-f]                                      show a job transcript; -f follows it live
  llm ps                                               loaded model, queue, recent jobs
  llm models                                           installed models and capabilities
"""
import argparse, json, os, pathlib, secrets, subprocess, sys, time, urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import common as C
import brieflint

B = C.QUEUE


def die(msg, code=2):
    print(f"llm: {msg}", file=sys.stderr)
    sys.exit(code)


# ------------------------------------------------------------------ setup
def cmd_init(a):
    """Create the queue repo and the local state directory. Safe to run again: it only adds what
    is missing and never overwrites config.json."""
    for d in ("jobs/inbox", "jobs/running", "jobs/done", "runs", "projects", "notes"):
        (B / d).mkdir(parents=True, exist_ok=True)
        keep = B / d / ".gitkeep"
        if not any((B / d).iterdir()):
            keep.touch()
    for name, text in ((".gitattributes", "* text=auto eol=lf\n"), (".gitignore", "__pycache__/\n*.pyc\n")):
        if not (B / name).exists():
            (B / name).write_text(text, encoding="utf-8")
    if not (B / ".git").exists():
        C.git("init", "-q", "-b", "main")
    if not C.git("config", "user.email", check=False).stdout.strip():   # a machine with no git identity yet
        C.git("config", "user.name", "llm-harness")
        C.git("config", "user.email", "llm-harness@localhost")
    with C.repo_lock():
        C.git("add", "-A")
        if C.git("diff", "--cached", "--quiet", check=False).returncode != 0:
            C.git("commit", "-q", "-m", "queue: skeleton")
    if a.bare and not C.has_remote():
        bare = os.path.abspath(a.bare)
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", bare], check=True)
        C.git("remote", "add", "origin", bare)
        C.git("push", "-q", "-u", "origin", "main")
    C.STATE.mkdir(parents=True, exist_ok=True)
    cfg = C.STATE / "config.json"
    if not cfg.exists():
        cfg.write_text(json.dumps(C.DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8")
    print(f"queue:   {B}" + (f"   (remote: {C.git('remote', 'get-url', 'origin', check=False).stdout.strip()})"
                              if C.has_remote() else "   (no remote: single-machine use)"))
    print(f"config:  {cfg}")
    print("next:    start the dispatcher (`llm-dispatch`), then try `llm ask \"hello\"`.\n"
          "         Jobs use an installed model that can call tools. To pin one, set default_model\n"
          "         in config.json (`llm models` lists what you have).")


def need_id(s):
    if not C.valid_id(s):
        die(f"bad id/name '{s}' (letters, digits, . _ - only)")
    return s


# ------------------------------------------------------------------ sync lane
def cmd_ask(a):
    q = " ".join(a.question)
    if q.strip() in ("", "-"):
        q = sys.stdin.read()
    elif not sys.stdin.isatty() and a.stdin:
        q += "\n\n" + sys.stdin.read()
    if not q.strip():
        die("empty question")
    cfg, models = C.config(), C.installed_models()
    # No -m: reuse whatever is already in VRAM so a quick question never forces a model swap
    # in the middle of someone else's job. Only fall back to the sync model on an idle GPU.
    want = a.model or next(iter(C.loaded_models()), None) or C.pick_model(cfg, models, key="sync_model")
    if not want:
        die("no model is installed in Ollama (`ollama pull ...`)")
    model = C.resolve_model(want, models) or die(f"model '{want}' not installed; see `llm models`")
    payload = {"model": model, "stream": True, "messages": [{"role": "user", "content": q}],
               "options": {"num_ctx": a.ctx or cfg["default_ctx"]}}
    if "thinking" in models[model]:
        payload["think"] = bool(a.think)
    req = urllib.request.Request(cfg["ollama"].rstrip("/") + "/api/chat", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    t0, answer, last = time.time(), [], {}
    with urllib.request.urlopen(req, timeout=1800) as r:
        for line in r:
            if not line.strip():
                continue
            last = json.loads(line)
            piece = last.get("message", {}).get("content", "")
            if piece:
                answer.append(piece)
                sys.stdout.write(piece)
                sys.stdout.flush()
    print()
    log = B / "runs" / "sync" / (time.strftime("%Y-%m-%d") + ".jsonl")
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": C.now(), "type": "sync_ask", "from": os.environ.get("SSH_CLIENT", "local").split(" ")[0],
                            "model": model, "question": q, "answer": "".join(answer),
                            "eval_tokens": last.get("eval_count"), "wall_s": round(time.time() - t0, 1)},
                           ensure_ascii=False) + "\n")


# ------------------------------------------------------------------ async lane
def new_id(who):
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{who}-{secrets.token_hex(2)}"


def cmd_newid(a):
    print(new_id(need_id(a.from_ or C.host_name())))


def cmd_put(a):
    d = B / "runs" / need_id(a.id) / "context"
    d.mkdir(parents=True, exist_ok=True)
    data = sys.stdin.buffer.read()
    if len(data) > 5_000_000:
        die("context file over 5 MB - distil it first; the model's window is far smaller than that")
    (d / need_id(a.name)).write_bytes(data)
    print(f"runs/{a.id}/context/{a.name}  ({len(data)} bytes)")


def cmd_submit(a):
    ask = " ".join(x for x in a.ask if x != "-")
    if "-" in a.ask or not ask.strip():
        text = sys.stdin.read()
        meta, body = C.parse_job(text)
    else:
        meta, body = {}, ask
    if not body.strip():
        die("empty job")
    for k, v in (("from", a.from_), ("project", a.project), ("model", a.model), ("ctx", a.ctx), ("think", a.think),
                 ("sampling", a.sampling), ("return", a.return_), ("web", "true" if a.web else None), ("parent_session", a.parent),
                 ("edit", "true" if a.edit else None), ("max_turns", a.max_turns),
                 ("group", a.group), ("step", a.step)):
        if v:
            meta[k] = str(v)
    # A job submitted from inside a Claude Code session belongs to that session: Claude Code exports
    # its session id to the commands it runs, so the link needs no flag. --no-parent makes it standalone.
    if a.no_parent:
        meta.pop("parent_session", None)
    elif not meta.get("parent_session") and os.environ.get("CLAUDE_CODE_SESSION_ID"):
        meta["parent_session"] = os.environ["CLAUDE_CODE_SESSION_ID"]
    if meta.get("parent_session") and not C.valid_id(meta["parent_session"]):
        die("bad parent session id")
    # group / step label a job as part of a larger piece of work ("search-filters", "2/3"). One line each.
    for k, cap in (("group", 80), ("step", 40)):
        if meta.get(k):
            meta[k] = " ".join(str(meta[k]).split())[:cap]
    meta.setdefault("from", C.host_name())
    meta.setdefault("return", "answer")
    jid = need_id(a.id or new_id(need_id(meta["from"])))
    meta["id"], meta["submitted"] = jid, C.now()
    # Advice only: a brief with these gaps is the usual reason a coding job loops or drifts.
    for w in brieflint.lint(meta, body, C.config()):
        print(f"llm: brief warning: {w}", file=sys.stderr)
    front = "\n".join(f"{k}: {meta[k]}" for k in ("id", "from", "submitted", "parent_session", "project", "model", "ctx", "think", "sampling", "web", "edit",
                                                         "max_turns", "group", "step", "return") if meta.get(k))
    path = B / "jobs" / "inbox" / f"{jid}.md"
    with C.repo_lock():
        if any((B / "jobs" / s / f"{jid}.md").exists() for s in ("inbox", "running", "done")):
            die(f"job {jid} already exists")
        path.write_text(f"---\n{front}\n---\n\n{body}\n", encoding="utf-8")
        try:
            C.commit_push(f"job {jid}: submitted by {meta['from']}", ["jobs", f"runs/{jid}"])
        except Exception:
            path.unlink(missing_ok=True)      # never leave a half-submitted job in the queue
            C.git("reset", "-q", "--", "jobs", check=False)
            raise
    print(jid)


def cmd_lint(a):
    text = sys.stdin.read() if a.file == "-" else open(a.file, encoding="utf-8").read()
    meta, body = C.parse_job(text)
    warnings = brieflint.lint(meta, body, C.config())
    for w in warnings:
        print(f"warning: {w}")
    if not warnings:
        print("no warnings" if C.truthy(meta.get("edit")) else "not an edit job (edit: true) - nothing to check")


def job_state(jid):
    for s in ("inbox", "running", "done"):
        if (B / "jobs" / s / f"{jid}.md").exists():
            return s
    return None


def result_line(jid):
    """The `- status: ...` line of a finished job."""
    for line in (B / "jobs" / "done" / f"{jid}.md").read_text(encoding="utf-8").splitlines():
        if line.startswith("- status:"):
            return line[2:].replace("**", "")
    return "status: unknown"


def cmd_status(a):
    jid = need_id(a.id)
    state = job_state(jid)
    if not state:
        die(f"no such job: {jid}", 1)
    if state == "inbox":
        queue = sorted(p.stem for p in (B / "jobs" / "inbox").glob("*.md"))
        ahead = queue.index(jid) + len(list((B / "jobs" / "running").glob("*.md")))
        print(f"{jid}: queued - {ahead} job(s) ahead of it")
    elif state == "running":
        turn, started, path = 0, None, B / "runs" / jid / "transcript.jsonl"
        if path.exists():
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                started = started or rec.get("t")
                turn = max(turn, rec.get("turn") or 0)
        age = ""
        if started:
            import datetime
            secs = int((datetime.datetime.now().astimezone() - datetime.datetime.fromisoformat(started)).total_seconds())
            age = f" for {secs // 60}m {secs % 60}s"
        print(f"{jid}: running{age}, turn {turn}   (watch: llm log {jid} -f)")
    else:
        print(f"{jid}: done - {result_line(jid)}")


def cmd_cancel(a):
    jid = need_id(a.id)
    state = job_state(jid)
    if not state:
        die(f"no such job: {jid}", 1)
    if state == "done":
        print(f"{jid}: already finished - {result_line(jid)}")
        return
    if state == "inbox":
        with C.repo_lock():
            src = B / "jobs" / "inbox" / f"{jid}.md"
            if src.exists():                      # still queued now that we hold the lock
                text = src.read_text(encoding="utf-8", errors="replace")
                src.unlink()
                (B / "jobs" / "done" / f"{jid}.md").write_text(
                    text.rstrip() + "\n\n---\n\n## Result\n\n- status: **cancelled** - cancelled before it started\n",
                    encoding="utf-8")
                C.commit_push(f"job {jid}: cancelled before it started", ["jobs"])
                print(f"{jid}: cancelled (it had not started)")
                return
    # Running (or claimed while we waited for the lock): ask the dispatcher to stop it.
    flag = C.STATE / "cancel" / jid
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text(C.now(), encoding="utf-8")
    t0 = time.time()
    while job_state(jid) != "done":
        if time.time() - t0 > 60:
            die(f"asked the dispatcher to stop {jid}, but it has not confirmed after 60s. Is it running? (`llm ps`)", 3)
        time.sleep(1)
    print(f"{jid}: {result_line(jid)}")
    print("note: files the job had already edited stay as they are - review or revert them in the project.")


def cmd_get(a):
    jid = need_id(a.id)
    folder = B / "runs" / jid / "artifacts"
    if not a.name:
        files = sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()) if folder.is_dir() else []
        print("\n".join(files) if files else f"{jid}: no artifacts")
        return
    target = (folder / a.name).resolve()
    if folder.resolve() not in target.parents or not target.is_file():
        die(f"no artifact '{a.name}' for {jid} (list them with: llm get {jid})", 1)
    sys.stdout.flush()
    sys.stdout.buffer.write(target.read_bytes())


def cmd_wait(a):
    jid, t0 = need_id(a.id), time.time()
    if not job_state(jid):
        die(f"no such job: {jid}", 1)
    while job_state(jid) != "done":
        if time.time() - t0 > a.timeout:
            die(f"timed out after {a.timeout}s; job is '{job_state(jid)}'. Check `llm ps`.", 3)
        time.sleep(1)
    print((B / "jobs" / "done" / f"{jid}.md").read_text(encoding="utf-8"))


def cmd_log(a):
    path = B / "runs" / need_id(a.id) / "transcript.jsonl"
    t0 = time.time()
    while not path.exists():
        if not a.follow or time.time() - t0 > 600:
            die(f"no transcript yet for {a.id} (state: {job_state(a.id)})", 1)
        time.sleep(1)
    with open(path, encoding="utf-8") as f:
        while True:
            line = f.readline()
            if line.endswith("\n"):
                rec = json.loads(line)
                print(C.render(rec), flush=True)
                if rec.get("type") == "job_done":
                    return
            elif a.follow:
                time.sleep(0.5)
            else:
                return


def cmd_ps(a):
    loaded = C.ollama("/api/ps", timeout=10).get("models", [])
    print("GPU:   " + (", ".join(f"{m['name']} ({m.get('size_vram', 0) / 2**30:.1f} GB VRAM)" for m in loaded) or "idle - no model loaded"))
    disp = C.STATE / "dispatch.heartbeat"
    alive = disp.exists() and time.time() - disp.stat().st_mtime < 90
    print("Disp:  " + ("running" if alive else "NOT RUNNING - queued jobs will wait (start: llm-dispatch)"))
    for s in ("inbox", "running"):
        names = sorted(p.stem for p in (B / "jobs" / s).glob("*.md"))
        print(f"{s + ':':<7}{len(names)}  " + " ".join(names[:6]))
    done = sorted((B / "jobs" / "done").glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]
    print("done:  " + (" ".join(p.stem for p in done) or "-"))


def cmd_models(a):
    loaded = set(C.loaded_models())
    for name, caps in sorted(C.installed_models().items()):
        if not name.startswith("hf.co/"):
            print(f"{'*' if name in loaded else ' '} {name:<24} {','.join(caps)}")


def main():
    C.utf8_stdio()
    ap = argparse.ArgumentParser(prog="llm", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init"); p.set_defaults(fn=cmd_init)
    p.add_argument("--bare", metavar="PATH", help="also create a bare repo there and make it the queue's remote, "
                                                  "so other machines can clone and push job files")
    p = sub.add_parser("ask"); p.add_argument("question", nargs="*"); p.add_argument("-m", "--model")
    p.add_argument("--ctx", type=int); p.add_argument("--think", action="store_true")
    p.add_argument("--stdin", action="store_true", help="append stdin to the question"); p.set_defaults(fn=cmd_ask)
    p = sub.add_parser("newid"); p.add_argument("--from", dest="from_"); p.set_defaults(fn=cmd_newid)
    p = sub.add_parser("put"); p.add_argument("id"); p.add_argument("name"); p.set_defaults(fn=cmd_put)
    p = sub.add_parser("submit"); p.add_argument("ask", nargs="*"); p.add_argument("--id")
    p.add_argument("--from", dest="from_"); p.add_argument("--project"); p.add_argument("--model")
    p.add_argument("--ctx", type=int); p.add_argument("--web", action="store_true")
    p.add_argument("--sampling", help="sampling preset for this job, by name from the config (default: the config's `sampling`)")
    p.add_argument("--think", choices=["on", "auto", "off"], help="reasoning for this job: every turn, only where the turn needs a decision, or never (default: the config)")
    p.add_argument("--return", dest="return_", choices=["answer", "report", "patch", "pr"])
    p.add_argument("--parent", help="id of the Claude Code session orchestrating this job (default: the calling session)")
    p.add_argument("--no-parent", action="store_true", help="file the job as standalone")
    p.add_argument("--edit", action="store_true", help="let the job edit the project's writable paths and run its checks")
    p.add_argument("--group", help="name of the larger piece of work this job belongs to")
    p.add_argument("--step", help="which step of the group this is, e.g. 2/3")
    p.add_argument("--max-turns", type=int, dest="max_turns"); p.set_defaults(fn=cmd_submit)
    p = sub.add_parser("wait"); p.add_argument("id"); p.add_argument("--timeout", type=int, default=3600); p.set_defaults(fn=cmd_wait)
    p = sub.add_parser("log"); p.add_argument("id"); p.add_argument("-f", "--follow", action="store_true"); p.set_defaults(fn=cmd_log)
    p = sub.add_parser("lint"); p.add_argument("file"); p.set_defaults(fn=cmd_lint)
    p = sub.add_parser("status"); p.add_argument("id"); p.set_defaults(fn=cmd_status)
    p = sub.add_parser("cancel"); p.add_argument("id"); p.set_defaults(fn=cmd_cancel)
    p = sub.add_parser("get"); p.add_argument("id"); p.add_argument("name", nargs="?"); p.set_defaults(fn=cmd_get)
    sub.add_parser("ps").set_defaults(fn=cmd_ps)
    sub.add_parser("models").set_defaults(fn=cmd_models)
    a = ap.parse_args()
    if a.cmd not in ("init", "models") and not (B / "jobs" / "inbox").is_dir():
        die(f"no job queue at {B}. Run `llm init` first, or point LLM_HARNESS_HOME at an existing one.")
    try:
        a.fn(a)
    except KeyboardInterrupt:
        sys.exit(130)
    except (OSError, RuntimeError) as e:
        die(f"{type(e).__name__}: {e}", 1)


if __name__ == "__main__":
    main()
