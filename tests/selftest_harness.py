"""Offline test of the harness loop with a scripted fake model. No Ollama, no GPU, no network.

  python tests/selftest_harness.py

Covers the loop detector, the final gate and its hand-back, the pinned state, the per-job patch
and the two review warnings. Set HARNESS_MODULE to test a working copy of llmjob.py under another
name before swapping it in. Exits 1 if any check fails.
"""
import contextlib, importlib, io, json, os, pathlib, sys, tempfile

tmp = pathlib.Path(tempfile.mkdtemp())
os.environ["LLM_HARNESS_HOME"] = str(tmp / "queue")          # keep the test away from a real queue
os.environ["LLM_HARNESS_STATE"] = str(tmp / "state")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))
import common as C
L = importlib.import_module(os.environ.get("HARNESS_MODULE", "llmjob"))

repo = tmp / "repo"; repo.mkdir()
(repo / "a.txt").write_text("x" * 2000, encoding="utf-8")
CFG = dict(C.DEFAULT_CONFIG, dashboard_url="", projects={"t": {"path": str(repo), "write_globs": ["*"], "checks": {},
       "gates": {"always-fail": {"cmd": [sys.executable, "-c", "import sys; print('boom: 2 tests failed'); sys.exit(1)"]}}}})
C.config = lambda: CFG
C.installed_models = lambda: {"fake:1": ["tools"]}
C.resolve_model = lambda want, models: "fake:1"

def call(tool, **args):
    return {"function": {"name": tool, "arguments": args}}

def run(script, ptok=2000):
    sent = []
    def fake(path, payload=None, timeout=0):
        sent.append(payload["messages"])
        step = script[min(len(sent) - 1, len(script) - 1)]
        msg = {"content": step if isinstance(step, str) else "", **({} if isinstance(step, str) else {"tool_calls": step})}
        return {"message": msg, "prompt_eval_count": ptok(len(sent)) if callable(ptok) else ptok, "eval_count": 50}
    C.ollama = fake
    rd = tmp / f"run{len(list(tmp.glob('run*')))}"; rd.mkdir()
    job = rd / "job.md"
    job.write_text("---\nproject: t\nedit: true\nmax_turns: 40\nctx: 65536\nmodel: fake\n---\nDo the thing.", encoding="utf-8")
    sys.argv = ["llmjob.py", str(job), str(rd)]
    with contextlib.redirect_stdout(io.StringIO()):
        L.main()
    res = json.loads((rd / "result.json").read_text(encoding="utf-8"))
    notes = [json.loads(l).get("text", "") for l in (rd / "transcript.jsonl").read_text(encoding="utf-8").splitlines() if '"type": "note"' in l]
    return res, notes, sent

ok = True
def check(label, cond):
    global ok
    ok &= bool(cond); print(("PASS " if cond else "FAIL ") + label)

# A. a model that only reads is warned three times, then stopped
res, notes, sent = run([[call("read_file", path="repo/a.txt")] * 5])
check("loop: job stopped with a loop-detector error", res["status"] == "error" and "loop detector" in (res["error"] or ""))
check("loop: three warnings were given first", sum("warning" in n for n in notes) == 3)
check("loop: the warning carries the state", any("STOP" in m["content"].upper() and "You have not changed any file" in m["content"] for m in sent[-1] if m["role"] == "user"))
check("loop: stalled flag recorded", res["stats"]["stalled"] is True)

# B. reads broken up by real work are not a loop
res, notes, sent = run([[call("read_file", path="repo/a.txt")] * 7, [call("write_file", path="repo/b.txt", content="hi")]] * 3 + ["done"])
check("no false alarm when edits happen between reads", not any("loop detector" in n for n in notes))

# C. a failed gate is handed back twice, then the job is failed - whatever the model claims
check("gate: failing gate makes the job an error", res["status"] == "error" and "final gate failed: always-fail" in res["error"])
check("gate: result records status and rounds", res["gate"]["status"] == "failed" and res["gate"]["rounds"] == 3)
check("gate: output was handed back to the model", any("boom: 2 tests failed" in m["content"] for m in sent[-1] if m["role"] == "user"))

# D. a passing gate lets the job through
CFG["projects"]["t"]["gates"] = {"ok": {"cmd": [sys.executable, "-c", "print('fine')"]},
                                 "server-only": {"cmd": [sys.executable, "-c", "import sys; sys.exit(1)"], "when": ["server/*"]}}
res, notes, sent = run([[call("write_file", path="repo/c.txt", content="hi")], "all done"])
check("gate: passing gate -> ok, and a gate whose `when` does not match is skipped",
      res["status"] == "ok" and res["gate"] == {"status": "passed", "rounds": 1, "checks": [{"name": "ok", "passed": True}]})

# E. no repo file touched -> no gate at all
res, notes, sent = run([[call("write_file", path="artifacts/out.md", content="answer")], "done"])
check("gate: not run when the job changed nothing in the repo", res["status"] == "ok" and "gate" not in res)

# F. when old output is removed, the state is pinned (and only one pin stays)
script = [[call("write_file", path="repo/d.txt", content="v")]] + [[call("read_file", path="repo/a.txt")]] * 9 + [[call("write_file", path="artifacts/NOTES.md", content="item 1 done; next: item 2")]] + [[call("read_file", path="repo/a.txt")]] * 4 + ["done"]
res, notes, sent = run(script, ptok=lambda n: 36000)
pins = [m["content"] for m in sent[-1] if m["role"] == "user" and "Pinned by the harness" in m["content"]]
check("pin: state pinned after elision", len(pins) == 1 and "repo/d.txt" in pins[0])
check("pin: notes are shown back once written", "item 1 done; next: item 2" in pins[0])
check("pin: bookkeeping keys never reach the model", all("pin" not in m and "shot" not in m for m in sent[-1]))
# G. the job's changes come back as a patch; a big rewrite just before "done" is flagged
NL = chr(10)
big = NL.join(f"line {i}" for i in range(60)) + NL
(repo / "big.txt").write_text(big, encoding="utf-8")
CFG["projects"]["t"]["gates"] = {}
CFG["projects"]["t"]["checks"] = {"noop": {"cmd": [sys.executable, "-c", "print('fine')"]}}
rewrite = [call("write_file", path="repo/big.txt", content=NL.join(f"other {i}" for i in range(60)) + NL)]
noop = [call("check", name="noop")]
res, notes, sent = run([[call("read_file", path="repo/big.txt")]] + [noop] * 5 + [rewrite, "done"])
rd = sorted(tmp.glob("run*"), key=lambda p: p.stat().st_mtime)[-1]
patch = (rd / "artifacts" / "changes.patch").read_text(encoding="utf-8")
check("patch: artifacts/changes.patch holds the job's own changes",
      "+++ b/big.txt" in patch and "-line 0" in patch and "+other 0" in patch)
check("patch: result lists the file with line counts",
      res["changes"] == [{"path": "big.txt", "added": 60, "removed": 60, "new": False}])
check("warning: late rewrite is flagged",
      any(w.startswith("late rewrite: repo/big.txt") for w in res.get("warnings", [])))
check("warning: edit after the last check is flagged",
      any("edited after the model's last check" in w for w in res.get("warnings", [])))

# H. the same rewrite done early and then checked earns no warning; a new file shows as new
(repo / "big.txt").write_text(big, encoding="utf-8")
res, notes, sent = run([rewrite + [call("write_file", path="repo/new.txt", content="hello" + NL)]] + [noop] * 6 + ["done"])
check("no warning for an early, checked rewrite", "warnings" not in res)
check("patch: a created file is marked new",
      {"path": "new.txt", "added": 1, "removed": 0, "new": True} in res["changes"])
print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
