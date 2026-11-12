"""Report a job to a dashboard while it runs. Optional: off unless `dashboard_url` is set.

The receiver is any HTTP service that accepts "external runs" at POST /api/hooks/external-run
(the payload is described in docs/dashboard.md). A job that an orchestrating session started
carries that session's id as `parent_session_id`, so the dashboard can file it under the
session as a sub-agent; a job nobody orchestrated becomes a run of its own.

Reporting is best-effort by design: the dashboard being down or slow must never fail or delay
a job. After three failed calls in a row this goes quiet for the rest of the job.
"""
import datetime, json, os, urllib.request, uuid

RESULT_CAP = 20000      # chars of one tool result kept in the dashboard conversation
EVENT_CAP = 4000        # chars of one tool result kept on the timeline event


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class Dashboard:
    def __init__(self, cfg, job_id, meta, cwd):
        self.url = (os.environ.get("LLM_HARNESS_DASHBOARD_URL") or cfg.get("dashboard_url") or "").rstrip("/")
        self.base = {"run_id": job_id, "provider": "ollama", "origin": meta.get("from") or None,
                     "cwd": cwd, "parent_session_id": meta.get("parent_session") or None}
        self.failures, self.where, self.model = 0, None, None

    # -- transport
    def send(self, event, **fields):
        if not self.url or self.failures >= 3:
            return None
        body = {**self.base, "event": event, **{k: v for k, v in fields.items() if v is not None}}
        try:
            req = urllib.request.Request(self.url + "/api/hooks/external-run", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=4) as r:
                out = json.loads(r.read())
            self.failures = 0
            self.where = {"mode": out.get("mode"), "session_id": out.get("session_id"),
                          "agent_id": out.get("agent_id"), "url": self.link(out)}
            return out
        except Exception:
            self.failures += 1
            return None

    def link(self, out):
        tab = "agents" if out.get("mode") == "child" else "conversation"
        return f"{self.url}/sessions/{out.get('session_id')}?tab={tab}"

    # -- lifecycle
    def start(self, model, ask, group=None, step=None):
        self.model = model
        self.base["model"] = model
        # The title is the brief's first line without its markdown heading marks, not the first 80
        # characters of the whole brief.
        first = next((l.strip().lstrip("#").strip() for l in ask.splitlines() if l.strip().lstrip("#").strip()), "")
        self.base["title"] = first[:80] or f"{model} job"
        # A job may belong to a larger piece of work ("group") and be one step of it ("2/3").
        self.base["group"], self.base["step"] = group or None, step or None
        return self.send("start", prompt=ask, transcript=[self.user_line(ask)])

    def tool_pre(self, name, args, assistant_line=None):
        return self.send("tool", phase="pre", tool_name=name, tool_input=args,
                         transcript=[assistant_line] if assistant_line else None)

    def tool_post(self, name, args, call_id, out, is_error):
        return self.send("tool", phase="post", tool_name=name, tool_input=args, tool_response=out[:EVENT_CAP],
                         is_error=bool(is_error), transcript=[self.result_line(call_id, out, is_error)])

    def end(self, ok, error=None, final_line=None, stats=None):
        return self.send("end", status="ok" if ok else "error", error=error, stats=stats,
                         transcript=[final_line] if final_line else None)

    # -- conversation lines (Claude-style message objects; the dashboard adds ids and session fields)
    @staticmethod
    def new_call_id():
        return "toolu_" + uuid.uuid4().hex[:24]

    @staticmethod
    def user_line(text):
        return {"type": "user", "timestamp": _now(), "message": {"content": text}}

    def assistant_line(self, thinking, text, calls, prompt_tokens, eval_tokens):
        """calls: list of (call_id, tool_name, args)."""
        blocks = []
        if thinking:
            blocks.append({"type": "thinking", "thinking": thinking, "signature": ""})
        if text:
            blocks.append({"type": "text", "text": text})
        for call_id, name, args in calls:
            blocks.append({"type": "tool_use", "id": call_id, "name": name, "input": args})
        if not blocks:
            return None
        return {"type": "assistant", "timestamp": _now(), "message": {
            "id": "msg_" + uuid.uuid4().hex[:24], "type": "message", "model": self.model, "content": blocks,
            "stop_reason": "tool_use" if calls else "end_turn",
            "usage": {"input_tokens": prompt_tokens or 0, "output_tokens": eval_tokens or 0}}}

    @staticmethod
    def result_line(call_id, out, is_error):
        text = out if len(out) <= RESULT_CAP else out[:RESULT_CAP] + f"\n[... {len(out) - RESULT_CAP} more chars not shown]"
        return {"type": "user", "timestamp": _now(), "message": {"content": [
            {"type": "tool_result", "tool_use_id": call_id, "content": text, "is_error": bool(is_error)}]}}
