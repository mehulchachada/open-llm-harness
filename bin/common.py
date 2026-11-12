"""Shared pieces for the harness tools (stdlib only).

Two directories matter, and neither of them is this code checkout:

  queue repo    LLM_HARNESS_HOME, default ~/llm-harness. A git repository that holds jobs/,
                runs/, projects/ and notes/. Other machines may push job files into it.
  local state   LLM_HARNESS_STATE, default "<queue repo>-local" next to it. config.json, locks,
                screenshots. It is deliberately outside the queue repo: nothing a job file or
                another machine pushes can change what a job is allowed to touch.

`llm init` creates both.
"""
import contextlib, datetime, json, os, pathlib, re, socket, subprocess, sys, time, urllib.request

CODE = pathlib.Path(__file__).resolve().parent.parent
QUEUE = pathlib.Path(os.environ.get("LLM_HARNESS_HOME") or pathlib.Path.home() / "llm-harness").resolve()
STATE = pathlib.Path(os.environ.get("LLM_HARNESS_STATE") or QUEUE.parent / (QUEUE.name + "-local")).resolve()
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

DEFAULT_CONFIG = {
    "ollama": "http://127.0.0.1:11434",
    "default_model": "",                # queued jobs; "" = pick one that is installed (see pick_model)
    "sync_model": "",                   # `llm ask` when no model is loaded; "" = same as default_model
    "default_ctx": 16384,
    "max_ctx": 65536,                   # ceiling for --ctx. Set it to what fits in your VRAM
    "searxng": "http://127.0.0.1:8888", # only used by jobs with `web: true`
    "dashboard_url": "",                # optional live run reporting (docs/dashboard.md); "" = off
    "host_name": "",                    # this machine's name in job ids; "" = taken from the hostname
    "projects": {},                     # name -> {"path": "...", ...}  see docs/configuration.md
}


def utf8_stdio():
    for s in (sys.stdin, sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            s.reconfigure(encoding="utf-8", errors="replace")


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def config():
    cfg = dict(DEFAULT_CONFIG)
    p = STATE / "config.json"
    if p.exists():
        cfg.update(json.loads(p.read_text(encoding="utf-8")))
    return cfg


def host_name(cfg=None):
    """Short name of this machine, safe to use inside a job id."""
    name = (cfg or config()).get("host_name") or socket.gethostname()
    return re.sub(r"[^a-z0-9-]", "", name.lower().split(".")[0])[:20] or "host"


def valid_id(s):
    return bool(ID_RE.match(s or "")) and ".." not in s


# ------------------------------------------------------------------ job files
def parse_job(text):
    """Front matter is flat `key: value` lines between --- markers. No YAML library:
    a job file comes from another machine and is DATA, so the parser stays dumb."""
    meta, body = {}, text
    if text.startswith("---"):
        parts = text.split("\n---", 1)
        if len(parts) == 2:
            for line in parts[0].splitlines()[1:]:
                line = line.split(" #", 1)[0].strip()
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip().lower()] = v.strip().strip("\"'")
            body = parts[1].lstrip("-").lstrip("\n")
    return meta, body.strip()


def truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on")


# ------------------------------------------------------------------ ollama
def ollama(path, payload=None, timeout=1800):
    req = urllib.request.Request(
        config()["ollama"].rstrip("/") + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def installed_models():
    return {m["name"]: m.get("capabilities", []) for m in ollama("/api/tags", timeout=10)["models"]}


def resolve_model(name, models):
    """Accept `mymodel` for `mymodel:latest`. Returns the installed name or None."""
    if name in models:
        return name
    if name and ":" not in name and f"{name}:latest" in models:
        return f"{name}:latest"
    return None


def loaded_models():
    return [m["name"] for m in ollama("/api/ps", timeout=10).get("models", [])]


def pick_model(cfg, models, loaded=(), key="default_model"):
    """The model to use when nothing names one. No model name is built in: the configured one
    wins, and without it the harness takes what is there - a model that is already loaded
    before one that would have to be loaded, and one that can call tools before one that cannot.
    Returns a name (possibly not installed, if the config names a missing one) or None."""
    if cfg.get(key) or cfg.get("default_model"):
        return cfg.get(key) or cfg["default_model"]
    pool = [m for m in loaded if m in models] + [m for m in models if m not in loaded]
    return next((m for m in pool if "tools" in models[m]), pool[0] if pool else None)


# ------------------------------------------------------------------ transcript
class Transcript:
    """Append-only JSONL, flushed and fsynced on every record so the file is complete
    at every instant - readable mid-job and after a hard kill. This is the visibility."""

    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.seq = 0
        self.f = open(path, "a", encoding="utf-8")

    def log(self, **kw):
        self.seq += 1
        rec = {"t": now(), "seq": self.seq, **kw}
        self.f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.f.flush()
        os.fsync(self.f.fileno())
        return rec


def render(rec, width=220):
    """One transcript record -> one human-readable line."""
    t, ty = rec.get("t", "")[11:19], rec.get("type", "?")
    head = f"[{rec.get('seq', 0):>3}] {t}  {ty:<11}| "
    one = lambda s: " ".join(str(s).split())[:width]
    if ty == "job_start":
        return head + f"{rec.get('model')}  ctx={rec.get('num_ctx')}  job={rec.get('job_id')}  from={rec.get('from')}"
    if ty == "thinking":
        return head + f"({rec.get('eval_count')} tok, {rec.get('dur_ms')} ms) " + one(rec.get("text", ""))
    if ty == "text":
        return head + one(rec.get("text", ""))
    if ty == "tool_call":
        return head + f"{rec.get('tool')} <- " + one(json.dumps(rec.get("input"), ensure_ascii=False))
    if ty == "tool_result":
        flag = " ERROR" if rec.get("is_error") else ""
        return head + f"{rec.get('tool')} -> {rec.get('chars')} chars, {rec.get('dur_ms')} ms{flag}  " + one(rec.get("preview", ""))[:100]
    if ty == "context":
        return head + f"window {rec.get('pct')}% full  (prompt {rec.get('prompt_tokens')} tok, {rec.get('chars_per_token', '?')} chars/tok, {rec.get('read_chars')} chars read)"
    rest = {k: v for k, v in rec.items() if k not in ("t", "seq", "type")}
    return head + one(json.dumps(rest, ensure_ascii=False))


# ------------------------------------------------------------------ git + locks
@contextlib.contextmanager
def repo_lock(timeout=120):
    """Serialise git operations in the queue repo (dispatcher vs `llm submit`)."""
    STATE.mkdir(parents=True, exist_ok=True)
    lock = STATE / "repo.lock"
    t0 = time.time()
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            with contextlib.suppress(OSError):
                if time.time() - lock.stat().st_mtime > 300:   # stale: holder died
                    lock.unlink()
                    continue
            if time.time() - t0 > timeout:
                raise TimeoutError("repo.lock held too long")
            time.sleep(0.2)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            lock.unlink()


def git(*args, check=True):
    r = subprocess.run(["git", "-C", str(QUEUE), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip() or r.stdout.strip()}")
    return r


def has_remote():
    """A queue repo used by one machine has no remote; then everything stays a local commit."""
    return bool(git("remote", check=False).stdout.strip())


def sync():
    """Pick up what other machines pushed. A no-op for a queue without a remote."""
    if has_remote():
        git("pull", "-q", "--rebase", "--autostash", check=False)


def commit_push(message, paths):
    """Stage paths, commit if anything changed, then rebase onto the remote and push (if there is one)."""
    tracked = set(git("ls-files", "--", *paths, check=False).stdout.split("\n"))
    paths = [p for p in paths if (QUEUE / p).exists() or any(t == p or t.startswith(p + "/") for t in tracked)]
    if paths:
        git("add", "-A", "--", *paths)
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        return False
    git("commit", "-q", "-m", message)
    if not has_remote():
        return True
    for attempt in range(3):
        git("pull", "-q", "--rebase", "--autostash", check=False)
        if git("push", "-q", check=False).returncode == 0:
            return True
        time.sleep(1 + attempt)
    raise RuntimeError("push to the queue's remote failed after 3 attempts")
