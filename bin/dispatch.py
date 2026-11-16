#!/usr/bin/env python3
"""dispatch.py - drain jobs/inbox one at a time on the machine that has the GPU.

  python dispatch.py            loop forever (Ctrl+C to stop)
  python dispatch.py --once     process what is queued, then exit

One dispatcher at a time (OS file lock). One job at a time: a single GPU holds one large
model. Jobs for the model that is already in VRAM run first, so a mixed queue costs one
model swap per model instead of one per job.
"""
import argparse, json, os, pathlib, subprocess, sys, time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import common as C

B, HERE = C.QUEUE, pathlib.Path(__file__).resolve().parent


def say(msg):
    print(f"{time.strftime('%H:%M:%S')}  {msg}", flush=True)


def single_instance():
    C.STATE.mkdir(parents=True, exist_ok=True)
    f = open(C.STATE / "dispatch.lock", "a+")
    try:                                    # either lock is released by the OS if we die
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("another dispatcher is already running")
    return f


def heartbeat():
    (C.STATE / "dispatch.heartbeat").write_text(C.now(), encoding="utf-8")


def finish(jid, job_text, result):
    """Write jobs/done/<id>.md = the original job + a Result section, and commit the run."""
    st = result.get("stats") or {}
    lines = [job_text.rstrip(), "", "---", "", "## Result", "",
             f"- status: **{result['status']}**" + (f" - {result['error']}" if result.get("error") else ""),
             f"- model: {st.get('model', '?')}  ·  ctx {st.get('num_ctx', '?')}  ·  {st.get('turns', '?')} turns  ·  "
             f"{st.get('eval_tokens', '?')} tokens out  ·  {st.get('wall_s', '?')} s",
             f"- transcript: `runs/{jid}/transcript.jsonl`  (`llm log {jid}`)"]
    gate = result.get("gate")
    if gate and gate.get("status") in ("passed", "failed", "pre-failing"):
        names = ", ".join(("" if c["passed"] else "FAILED ") + c["name"] for c in gate.get("checks", []))
        lines.insert(-1, f"- final gate (run by the harness): **{gate['status']}** - {names}"
                         + (f"  ·  {gate['rounds'] - 1} fix round(s)" if gate.get("rounds", 1) > 1 else ""))
        if gate.get("already_failing"):
            lines.insert(-1, "- NOTE: " + ", ".join(f"`{n}`" for n in gate["already_failing"])
                         + " was already failing before this job, so it was not counted against the model."
                         + " Fix it in the project, or this gate verifies nothing.")
    elif gate:
        lines.insert(-1, "- final gate: none configured for this project - the result is UNVERIFIED")
    if st.get("files_changed") or st.get("screenshots"):
        lines.insert(-1, f"- work: {st.get('files_changed', 0)} file(s) changed  ·  {st.get('tool_calls', '?')} tool calls  ·  "
                         f"{st.get('checks', '?')} checks  ·  {st.get('screenshots', 0)} screenshot(s)"
                         + ("  ·  STOPPED BY LOOP DETECTOR" if st.get("stalled") else ""))
    if result.get("changes"):
        ch = result["changes"]
        lines.append(f"- changes: `runs/{jid}/artifacts/changes.patch`  ({len(ch)} file(s), "
                     f"+{sum(c['added'] for c in ch)} -{sum(c['removed'] for c in ch)} lines)")
    for w in result.get("warnings") or []:
        lines.append(f"- WARNING: {w}")
    if result.get("dashboard"):
        d = result["dashboard"]
        where = "under its orchestrating session" if d.get("mode") == "child" else "as a standalone run"
        lines.append(f"- dashboard: {d.get('url')}  ({where})")
    if result.get("artifacts"):
        lines += ["- artifacts:"] + [f"  - `runs/{jid}/{a}`" for a in result["artifacts"]]
    if result.get("final"):
        lines += ["", "### Answer", "", result["final"].strip()]
    meta, _ = C.parse_job(job_text)
    if meta.get("return") in ("patch", "pr"):
        lines += ["", f"> `return: {meta['return']}` - the model's output above is a draft. Turning it into a "
                  "reviewed patch or pull request is the orchestrator's step, not the dispatcher's."]
    with C.repo_lock():
        (B / "jobs" / "done" / f"{jid}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (B / "jobs" / "running" / f"{jid}.md").unlink(missing_ok=True)
        C.commit_push(f"job {jid}: {result['status']}", ["jobs", f"runs/{jid}", "runs/sync"])


def recover():
    """A job left in running/ means the last dispatcher died mid-job. Don't silently rerun it."""
    for p in sorted((B / "jobs" / "running").glob("*.md")):
        say(f"{p.stem}: found in running/ at startup -> marking interrupted")
        finish(p.stem, p.read_text(encoding="utf-8"),
               {"status": "interrupted", "error": "dispatcher stopped while this job was running; resubmit to retry"})


def pick():
    """Next job: same model as the one in VRAM first, then oldest."""
    cfg = C.config()
    try:
        loaded = set(C.loaded_models())
    except OSError:
        loaded = set()
    best = None
    for p in sorted((B / "jobs" / "inbox").glob("*.md")):
        if not C.valid_id(p.stem):
            say(f"skipping badly named job file: {p.name}")
            continue
        meta, _ = C.parse_job(p.read_text(encoding="utf-8", errors="replace"))
        want = meta.get("model") or cfg.get("default_model")
        # A job that names no model, on a config that names none, runs on whatever is loaded.
        warm = (want in loaded or f"{want}:latest" in loaded) if want else bool(loaded)
        key = (0 if warm else 1, p.stem)
        if best is None or key < best[0]:
            best = (key, p)
    return best[1] if best else None


def run_one(p):
    jid = p.stem
    job_text = p.read_text(encoding="utf-8", errors="replace")
    with C.repo_lock():                      # claim = move to running/ and push
        C.git("add", "--", f"jobs/inbox/{jid}.md")      # a file dropped in by hand is not tracked yet
        C.git("mv", f"jobs/inbox/{jid}.md", f"jobs/running/{jid}.md")
        C.commit_push(f"job {jid}: claimed by {C.host_name()}", ["jobs"])
    run_dir = B / "runs" / jid
    run_dir.mkdir(parents=True, exist_ok=True)
    say(f"{jid}: running   (watch: llm log {jid} -f)")
    proc = subprocess.Popen([sys.executable, str(HERE / "llmjob.py"),
                             str(B / "jobs" / "running" / f"{jid}.md"), str(run_dir)])
    flag, cancelled = C.STATE / "cancel" / jid, False
    while proc.poll() is None:
        heartbeat()
        if flag.exists():                    # `llm cancel ID`
            cancelled = True
            proc.kill()
            proc.wait(timeout=30)
            break
        time.sleep(2)
    flag.unlink(missing_ok=True)
    if cancelled:
        result = {"status": "cancelled", "error": "stopped on request (llm cancel); files it had already edited "
                                                   "were left as they are"}
        try:                                  # close the run on the dashboard too, best effort
            from dashboard import Dashboard
            meta, _ = C.parse_job(job_text)
            Dashboard(C.config(), jid, meta, str(run_dir)).end(False, "cancelled on request")
        except Exception:
            pass
    else:
        try:
            result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            result = {"status": "error", "error": f"harness exited {proc.returncode} without a result"}
    finish(jid, job_text, result)
    say(f"{jid}: {result['status']}")


def main():
    C.utf8_stdio()
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=10)
    a = ap.parse_args()
    if not (B / "jobs" / "inbox").is_dir():
        sys.exit(f"no job queue at {B}. Run `llm init` first, or set LLM_HARNESS_HOME.")
    lock = single_instance()
    say(f"dispatcher up  queue={B}")
    recover()
    try:
        while True:
            heartbeat()
            with C.repo_lock():
                C.sync()
            p = pick()
            if p:
                try:
                    run_one(p)
                except Exception as e:           # one bad job must not take the dispatcher down
                    say(f"{p.stem}: dispatcher error: {type(e).__name__}: {e}")
                    for src in (p, B / "jobs" / "running" / p.name):
                        if src.exists():
                            text = src.read_text(encoding="utf-8", errors="replace")
                            src.unlink()
                            with C.repo_lock():
                                C.git("reset", "-q", check=False)
                            finish(p.stem, text, {"status": "error", "error": f"dispatcher: {type(e).__name__}: {e}"})
                            break
                continue
            if a.once:
                break
            time.sleep(a.interval)
    except KeyboardInterrupt:
        say("stopped")
    finally:
        (C.STATE / "dispatch.heartbeat").unlink(missing_ok=True)
        lock.close()


if __name__ == "__main__":
    main()
