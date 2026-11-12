"""End-to-end test of the commands against a fake Ollama server. No GPU, no model, no network.

  python tests/e2e_fake_ollama.py

Starts a tiny HTTP server that answers /api/tags, /api/ps and /api/chat the way Ollama does, then
drives the real commands as subprocesses in a throwaway queue: init (with a bare remote), ask,
submit, the dispatcher, status, wait, get, log and cancel. Exits 1 if any check fails.
"""
import http.server, json, os, pathlib, subprocess, sys, tempfile, threading

BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"
tmp = pathlib.Path(tempfile.mkdtemp())
ENV = dict(os.environ, LLM_HARNESS_HOME=str(tmp / "queue"), LLM_HARNESS_STATE=str(tmp / "state"),
           PYTHONIOENCODING="utf-8")
ENV.pop("CLAUDE_CODE_SESSION_ID", None)


class FakeOllama(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/api/tags":
            self._send({"models": [{"name": "plain:1", "capabilities": ["completion"]},
                                   {"name": "fake:1", "capabilities": ["completion", "tools"]}]})
        elif self.path == "/api/ps":
            self._send({"models": []})
        else:
            self.send_error(404)

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if req.get("stream"):                       # `llm ask`: newline-delimited chunks
            chunks = [{"message": {"content": "four "}}, {"message": {"content": "words here"}, "done": True, "eval_count": 3}]
            self._send("".join(json.dumps(c) + "\n" for c in chunks).encode(), "application/x-ndjson")
        elif not any(m["role"] == "tool" for m in req["messages"]):   # a job's first turn: use a tool
            self._send({"message": {"content": "", "tool_calls": [{"function": {"name": "write_file", "arguments": {
                "path": "artifacts/answer.md", "content": "# Answer\n\n42\n"}}}]}, "prompt_eval_count": 900, "eval_count": 40})
        else:                                       # second turn: finish
            self._send({"message": {"content": "Wrote artifacts/answer.md."}, "prompt_eval_count": 1000, "eval_count": 12})


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
threading.Thread(target=server.serve_forever, daemon=True).start()


def run(script, *args, stdin=None):
    r = subprocess.run([sys.executable, str(BIN / script), *args], env=ENV, input=stdin, capture_output=True,
                       text=True, encoding="utf-8", timeout=120)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


ok = True
def check(label, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"\n     {detail}"))


code, out, err = run("llm.py", "ps")
check("before init: commands say where the queue should be", code == 2 and "llm init" in err, err)

code, out, err = run("llm.py", "init", "--bare", str(tmp / "queue.git"))
check("init: creates the queue, its remote and config.json",
      code == 0 and (tmp / "queue" / "jobs" / "inbox").is_dir() and (tmp / "state" / "config.json").is_file(), out + err)
cfg = json.loads((tmp / "state" / "config.json").read_text(encoding="utf-8"))
check("init: the starter config names no model", cfg["default_model"] == "" and cfg["sync_model"] == "", str(cfg))
cfg.update(ollama=f"http://127.0.0.1:{server.server_address[1]}", host_name="testbox")   # no model set, on purpose
(tmp / "state" / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
code, out, err = run("llm.py", "init")
check("init: running it again changes nothing", code == 0 and json.loads((tmp / "state" / "config.json").read_text(encoding="utf-8")) == cfg, out + err)

code, out, err = run("llm.py", "ask", "say", "four", "words")
check("ask: streams the answer", code == 0 and out == "four words here", out + err)
check("ask: the question and answer are logged", any("four words here" in p.read_text(encoding="utf-8")
                                                      for p in (tmp / "queue" / "runs" / "sync").glob("*.jsonl")))

code, jid, err = run("llm.py", "submit", "--return", "report", "Write the answer to artifacts/answer.md")
check("submit: prints a job id named after this machine", code == 0 and "-testbox-" in jid, jid + err)
code, out, err = run("llm.py", "status", jid)
check("status: queued", "queued - 0 job(s) ahead" in out, out + err)
code, cancel_id, err = run("llm.py", "submit", "a job that is cancelled before it starts")
code, out, err = run("llm.py", "cancel", cancel_id)
check("cancel: a queued job is removed", code == 0 and "cancelled" in out, out + err)

code, out, err = run("dispatch.py", "--once")
check("dispatcher: runs the queue and exits", code == 0 and f"{jid}: ok" in out, out + err)
code, out, err = run("llm.py", "wait", jid, "--timeout", "5")
check("wait: result has the status and the model's answer", "status: **ok**" in out and "Wrote artifacts/answer.md." in out, out + err)
code, out, err = run("llm.py", "get", jid)
check("get: lists the artifact", out == "answer.md", out + err)
code, out, err = run("llm.py", "get", jid, "answer.md")
check("get: prints the artifact", "42" in out, out + err)
code, out, err = run("llm.py", "log", jid)
check("log: shows the tool call and the end of the job", "write_file <-" in out and "job_done" in out, out + err)
check("no model configured: the job ran on the installed model that can call tools", "fake:1  ctx=" in out, out)
code, out, err = run("llm.py", "ps")
check("ps: dispatcher reported as not running once it has exited", "NOT RUNNING" in out and jid in out, out + err)

log = subprocess.run(["git", "--git-dir", str(tmp / "queue.git"), "log", "--format=%s"], capture_output=True, text=True).stdout
check("git: submit, claim and result were pushed to the remote",
      all(s in log for s in (f"job {jid}: submitted by testbox", f"job {jid}: claimed by testbox", f"job {jid}: ok")), log)

print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
