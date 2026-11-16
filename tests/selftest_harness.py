"""Offline test of the harness loop with a scripted fake model. No Ollama, no GPU, no network.

  python tests/selftest_harness.py

Covers the loop detector, the final gate and its hand-back, the pinned state, the per-job patch,
the two review warnings, the think policy and the reasoning-runaway guards. Set HARNESS_MODULE to test a working copy of llmjob.py under another
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
CAPS = ["tools", "thinking"]
PAYLOADS = []            # every request the loop sent, so a test can assert on think
C.installed_models = lambda: {"fake:1": list(CAPS)}
C.resolve_model = lambda want, models: "fake:1"

def call(tool, **args):
    return {"function": {"name": tool, "arguments": args}}

def run(script, ptok=2000, front=''):
    sent = []
    PAYLOADS.clear()
    def fake(path, payload=None, timeout=0):
        sent.append(payload["messages"])
        PAYLOADS.append(payload)
        step = script[min(len(sent) - 1, len(script) - 1)]
        if isinstance(step, dict):       # full control of one turn: thinking, done_reason, token counts
            msg = {k: step.get(k, "") for k in ("content", "thinking")}
            if step.get("tool_calls"):
                msg["tool_calls"] = step["tool_calls"]
            out = {"message": msg, "prompt_eval_count": ptok(len(sent)) if callable(ptok) else ptok,
                   "eval_count": step.get("eval_count", 50)}
            if step.get("done_reason"):
                out["done_reason"] = step["done_reason"]
            return out
        msg = {"content": step if isinstance(step, str) else "", **({} if isinstance(step, str) else {"tool_calls": step})}
        return {"message": msg, "prompt_eval_count": ptok(len(sent)) if callable(ptok) else ptok, "eval_count": 50}
    C.ollama = fake
    rd = tmp / f"run{len(list(tmp.glob('run*')))}"; rd.mkdir()
    job = rd / "job.md"
    job.write_text("---\nproject: t\nedit: true\nmax_turns: 40\nctx: 65536\nmodel: fake\n"
                   + front + "---\nDo the thing.", encoding="utf-8")
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

# C. a gate the job BREAKS is handed back twice, then the job is failed - whatever the model claims.
# The gate passes while repo/b.txt is absent, so the baseline is green and the failure is the job's.
BREAK_B = "import os, sys\nif os.path.exists('b.txt'):\n    print('boom: 2 tests failed')\n    sys.exit(1)\n"
(repo / "b.txt").unlink(missing_ok=True)
CFG["projects"]["t"]["gates"] = {"broke-b": {"cmd": [sys.executable, "-c", BREAK_B], "cwd": "."}}
res, notes, sent = run([[call("read_file", path="repo/a.txt")] * 7,
                        [call("write_file", path="repo/b.txt", content="hi")]] * 3 + ["done"])
check("gate: a gate the job breaks makes the job an error",
      res["status"] == "error" and "final gate failed: broke-b" in (res["error"] or ""))
check("gate: result records status and rounds", res["gate"]["status"] == "failed" and res["gate"]["rounds"] == 3)
check("gate: output was handed back to the model",
      any("boom: 2 tests failed" in m["content"] for m in sent[-1] if m["role"] == "user"))
check("gate: the failing gate was re-run on the pre-job files, where it passed",
      any("pre-job gate check" in n and "broke-b=passed before the job" in n for n in notes))
check("swap: the file the job created is back in place afterwards",
      (repo / "b.txt").read_text(encoding="utf-8") == "hi")

# C2. a gate that was ALREADY red before the job is reported, not charged to the model
res, notes, sent = run([[call("write_file", path="repo/b.txt", content="hi")], "done"])
check("gate: a gate already failing before the job does not fail the job",
      res["status"] == "ok" and res["gate"]["status"] == "pre-failing"
      and any("broke-b=ALSO FAILED BEFORE THE JOB" in n for n in notes))
check("gate: it is named on the result rather than passed off as verified",
      res["gate"].get("already_failing") == ["broke-b"] and res["stats"]["gates_already_failing"] == 1)
check("gate: and it is not handed back, so the model is not sent to fix what it did not break",
      not any("boom: 2 tests failed" in m["content"] for m in sent[-1] if m["role"] == "user"))
(repo / "b.txt").unlink(missing_ok=True)

# D. a passing gate lets the job through
CFG["projects"]["t"]["gates"] = {"ok": {"cmd": [sys.executable, "-c", "print('fine')"]},
                                 "server-only": {"cmd": [sys.executable, "-c", "import sys; sys.exit(1)"], "when": ["server/*"]}}
res, notes, sent = run([[call("write_file", path="repo/c.txt", content="hi")], "all done"])
check("gate: passing gate -> ok, and a gate whose `when` does not match is skipped",
      res["status"] == "ok" and res["gate"] == {"status": "passed", "rounds": 1, "checks": [{"name": "ok", "passed": True}]})
check("gate: a job whose gates pass pays nothing for the pre-job check",
      not any("pre-job gate check" in n for n in notes))

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
# I. reasoning is spent where it changes the next move, not on acknowledging the model's own work
CFG["projects"]["t"]["gates"] = {}
CFG["projects"]["t"]["checks"] = {"noop": {"cmd": [sys.executable, "-c", "print('fine')"]}}
res, notes, sent = run([[call("read_file", path="repo/a.txt")],
                        [call("write_file", path="repo/e.txt", content="1")],
                        [call("write_file", path="repo/f.txt", content="2")],
                        [call("check", name="noop")],
                        "done"])
check("think: on turn one and after a read, off after the model's own successful work",
      [p.get("think") for p in PAYLOADS] == [True, True, False, False, False])
check("think: the policy and its cost are on the scorecard",
      res["stats"]["think"] == "auto" and res["stats"]["think_turns"] == 2)

CFG["projects"]["t"]["checks"] = {"bad": {"cmd": [sys.executable, "-c", "import sys; sys.exit(1)"]}}
res, notes, sent = run([[call("write_file", path="repo/g.txt", content="1")],
                        [call("check", name="bad")],
                        [call("write_file", path="repo/g.txt", content="2")],
                        "done"])
check("think: a failed check earns the next turn its reasoning",
      [p.get("think") for p in PAYLOADS] == [True, False, True, False])

res, notes, sent = run([[call("write_file", path="repo/h.txt", content="1")], "done"], front="think: on\n")
check("think: on reasons on every turn", [p.get("think") for p in PAYLOADS] == [True, True])
res, notes, sent = run([[call("write_file", path="repo/i.txt", content="1")], "done"], front="think: off\n")
check("think: off asks for no reasoning explicitly, since a thinking model reasons by default",
      [p.get("think") for p in PAYLOADS] == [False, False])

# J. a turn that only reasons is not a finished job, and not a full window either
mute = {"thinking": "I will consider every option. " * 40, "content": "", "eval_count": 900}
res, notes, sent = run([mute, [call("write_file", path="repo/j.txt", content="1")], "done"])
check("runaway: reasoning with no answer and no tool call is retried, not accepted",
      res["status"] == "ok" and any("with no tool call and no answer" in n for n in notes))
check("runaway: the retry switches reasoning off", [p.get("think") for p in PAYLOADS] == [True, False, False])

# K. num_predict may not bind reasoning, so an over-budget turn costs the next few turns theirs
over = {"thinking": "x" * 100, "eval_count": 9000, "tool_calls": [call("read_file", path="repo/a.txt")]}
res, notes, sent = run([over] + [[call("read_file", path="repo/a.txt")]] * 4 + ["done"])
check("budget: an over-budget turn is logged", any("over the reasoning budget" in n for n in notes))
check("budget: the cooldown overrides a read that would otherwise have earned reasoning",
      [p.get("think") for p in PAYLOADS] == [True, False, False, False, True, True])

# L. the two context failures no longer share one message
res, notes, sent = run([mute] * 12)
check("error: a reasoning runaway is named as one, not blamed on the window",
      res["status"] == "error" and "reasoning runaway, not a full window" in (res["error"] or ""))
res, notes, sent = run([mute] * 12, ptok=60000)
check("error: a genuinely full window still asks for a larger ctx",
      res["status"] == "error" and "Resubmit with a larger ctx" in (res["error"] or ""))

# M. a listing is new information: what to open next is a decision, so it earns the next turn
res, notes, sent = run([[call("list_dir", path="repo")],
                        [call("write_file", path="repo/l.txt", content="1")], "done"])
check("think: a directory listing earns the next turn its reasoning",
      [p.get("think") for p in PAYLOADS] == [True, True, False])

# N. the budget is a runaway detector at every window size, not a share of a small one
nearly = {"thinking": "x" * 100, "eval_count": 2000,
          "tool_calls": [call("read_file", path="repo/a.txt")]}
res, notes, sent = run([nearly, [call("read_file", path="repo/a.txt")], "done"], front="ctx: 16384\n")
check("budget: a 2k-token turn in a 16k window is an ordinary turn, not a runaway",
      not any("over the reasoning budget" in n for n in notes)
      and [p.get("think") for p in PAYLOADS] == [True, True, True])
over16 = dict(nearly, eval_count=5000)
res, notes, sent = run([over16, [call("read_file", path="repo/a.txt")], "done"], front="ctx: 16384\n")
check("budget: a 5k-token turn in a 16k window still is one",
      any("over the reasoning budget of 4096" in n for n in notes)
      and [p.get("think") for p in PAYLOADS] == [True, False, False])

# O. a model that has delivered but will not summarise must not become a new failure
CFG["projects"]["t"]["gates"] = {}
res, notes, sent = run([[call("write_file", path="artifacts/out.md", content="answer")]] + [mute] * 12)
check("runaway: a model that has delivered but stays silent still ends ok, not error",
      res["status"] == "ok" and sum("no tool call and no answer" in n for n in notes) == 8)
check("runaway: mid-job, having started, it is told to carry on - not to summarise and stop",
      any("carry on with the next unfinished step" in m["content"] for m in sent[2] if m["role"] == "user")
      and not any("Reply now in plain text" in m["content"] for m in sent[-1] if m["role"] == "user"))
res, notes, sent = run([[call("write_file", path="artifacts/out.md", content="answer")]] + [mute] * 12,
                       front="max_turns: 6\n")
check("runaway: in the last turns of the job it is asked for the summary instead",
      any("Reply now in plain text" in m["content"] for m in sent[2] if m["role"] == "user"))

# O2. the job that ended "ok" at turn 19 of 90: a runaway, then a progress report taken for the
#     final answer while the model's own checklist was still open
OPEN2 = "## Status\n- [ ] item 1: spacing\n- [ ] item 2: padding\n"
res, notes, sent = run([[call("write_file", path="artifacts/NOTES.md", content=OPEN2)],
                        [call("write_file", path="repo/o.txt", content="1")],
                        mute,
                        "Status so far: I am mid-item-1.",
                        [call("write_file", path="artifacts/NOTES.md",
                              content="- [x] item 1: spacing\n- [-] item 2: padding (no such table)\n")],
                        "Done. Item 2 could not be done: there is no such table."])
check("open items: a final answer while NOTES.md has unticked items is handed back, not accepted",
      any("2 unticked item(s)" in n and "handed back" in n for n in notes)
      and any("- [ ] item 2: padding" in m["content"] for m in sent[4] if m["role"] == "user"))
check("open items: ticked and explicitly dropped items let the job finish ok",
      res["status"] == "ok" and res["stats"]["open_items"] == 0 and res["final"].startswith("Done."))
res, notes, sent = run([[call("write_file", path="artifacts/NOTES.md", content=OPEN2)],
                        [call("write_file", path="repo/o.txt", content="2")],
                        "Status so far: I am mid-item-1.", "Still mid-item-1."])
check("open items: stopping again with the list still open ends incomplete, not ok",
      res["status"] == "incomplete" and "2 unticked item(s)" in (res["error"] or "")
      and "item 1: spacing" in res["error"] and res["stats"]["open_items"] == 2)
check("open items: handed back once only", sum("handed back to the model" in n for n in notes) == 1)
res, notes, sent = run([[call("write_file", path="artifacts/NOTES.md", content="- [x] all of it\n")],
                        [call("write_file", path="repo/o.txt", content="3")], "done"])
check("open items: a fully ticked list, or none at all, costs nothing",
      res["status"] == "ok" and not any("unticked" in n for n in notes) and len(sent) == 3)
(repo / "o.txt").unlink(missing_ok=True)

# P. a model without thinking is untouched by any of this
CAPS[:] = ["tools"]
res, notes, sent = run([[call("write_file", path="repo/m.txt", content="1")], "done"])
check("a model with no thinking capability is never sent a think flag",
      res["status"] == "ok" and all("think" not in p for p in PAYLOADS))
CAPS[:] = ["tools", "thinking"]

# Q. the pre-job check puts the job's exact bytes back, runs once however many hand-back rounds,
#    and an edit to an existing CRLF file survives the swap byte for byte
(repo / "crlf.txt").write_bytes(b"a\r\nb\r\n")
NO_X = "import sys\nif 'X' in open('crlf.txt').read():\n    print('found X'); sys.exit(1)\n"
CFG["projects"]["t"]["gates"] = {"no-x": {"cmd": [sys.executable, "-c", NO_X], "cwd": "."}}
res, notes, sent = run([[call("edit_file", path="repo/crlf.txt", old_string="a", new_string="X")],
                        "done", "done", "done"])
check("swap: a gate the job broke is still the job's",
      res["status"] == "error" and any("no-x=passed before the job" in n for n in notes))
check("swap: checked once, not once per hand-back round",
      sum("pre-job gate check" in n for n in notes) == 1 and res["gate"]["rounds"] == 3)
check("swap: the job's edit is back in place byte for byte, line endings included",
      (repo / "crlf.txt").read_bytes() == b"X\r\nb\r\n")
rd = sorted(tmp.glob("run*"), key=lambda q: q.stat().st_mtime)[-1]
check("swap: no backup is left behind once everything is back", not (rd / "gate-swap").exists())
(repo / "crlf.txt").unlink()

# R. a changed file too large to keep a copy of: no swap at all, and the failure stays the job's -
#    the behaviour from before this check existed. Slower, never wrong, never deletes a real file.
saved_max = L.SNAPSHOT_MAX
L.SNAPSHOT_MAX = 10
(repo / "big2.txt").write_text("y" * 50, encoding="utf-8")
CFG["projects"]["t"]["gates"] = {"red": {"cmd": [sys.executable, "-c", "import sys; sys.exit(1)"]}}
res, notes, sent = run([[call("write_file", path="repo/big2.txt", content="z" * 50)], "done", "done", "done"])
L.SNAPSHOT_MAX = saved_max
check("swap: a file too large to restore means no swap, and the failure stays the job's",
      res["status"] == "error" and any("too large to restore" in n for n in notes))
check("swap: and that large file is left exactly as the job wrote it",
      (repo / "big2.txt").read_text(encoding="utf-8") == "z" * 50)
(repo / "big2.txt").unlink()
CFG["projects"]["t"]["gates"] = {}

print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
