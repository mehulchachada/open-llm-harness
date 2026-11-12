"""Advice on a coding brief before it is queued. Never blocks: it prints warnings.

Each rule comes from a job that went wrong:
  - no done condition          -> the model decides for itself when it is finished
  - no files named             -> it explores the repo to find where to work
  - a very large file named    -> it pages through the file and loses the read to context trims
  - "read the spec in shared/" -> it re-reads the spec after every trim instead of working
  - a long brief               -> the brief is re-sent every turn
"""
import pathlib, re

BIG_FILE_LINES = 800
LONG_BRIEF_CHARS = 14000
DONE_RE = re.compile(r"(?im)^\s*#{1,4}\s*(done|done condition|definition of done|finish)\b|done condition|done means")
PATH_RE = re.compile(r"repo/([A-Za-z0-9_./\-\[\]{},*]+\.[A-Za-z0-9]{1,5})")
SPEC_RE = re.compile(r"(?i)\bread\b[^\n]{0,140}?shared/(?!overview)[\w./-]+")
CAREFUL_RE = re.compile(r"(?i)do not read it|do not read the whole|never read (it|them) whole|very large file|search\b.{0,60}\bread\b")


def lint(meta, body, cfg):
    """Returns a list of warning strings for a job about to be submitted."""
    if str(meta.get("edit", "")).strip().lower() not in ("1", "true", "yes", "on"):
        return []
    out = []
    if not DONE_RE.search(body):
        out.append("no done condition: end the brief with which checks must pass and what the summary must say")
    paths = sorted(set(PATH_RE.findall(body)))
    if not paths:
        out.append("no files named: list the exact repo/ paths the job creates or edits")
    root = (cfg.get("projects", {}).get(meta.get("project") or "") or {}).get("path")
    if root and not CAREFUL_RE.search(body):
        for rel in paths:
            f = pathlib.Path(root) / rel
            try:
                if f.is_file() and f.stat().st_size < 3_000_000:
                    n = sum(1 for _ in f.open("rb"))
                    if n > BIG_FILE_LINES:
                        out.append(f"repo/{rel} has {n} lines: give the exact anchor text for each edit and tell "
                                   "the model not to read the file whole")
            except OSError:
                pass
    if SPEC_RE.search(body):
        out.append("the brief sends the model to read a spec in shared/: put what this job needs in the brief "
                   "itself and say the brief is complete (a spec gets re-read after every context trim)")
    if not meta.get("max_turns"):
        out.append("no max_turns: the default is 30, which is short for a coding job (60-120 is usual)")
    if len(body) > LONG_BRIEF_CHARS:
        out.append(f"the brief is {len(body)} chars and is re-sent every turn: split the job or move standing "
                   "rules into the project's shared/overview.md")
    if not meta.get("group"):
        out.append("no group/step: add `group:` and `step:` so the run is filed under its task on the dashboard")
    return out
