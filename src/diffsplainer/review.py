"""The review file: what the reviewer decided about unpushed commits, shared with the agent that wrote them.

The format is documented in skill/diffsplainer-review/SKILL.md. Both programs write the file, so every write re-reads
it, keeps the fields it does not know, and swaps a finished temporary file in with `os.replace`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path

SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")  # SHA-1 or SHA-256
STATUSES = ("open", "resolved")
_ENTRY_FIELDS = ("file", "lines", "diff", "body")
_lock = threading.Lock()


class ReviewError(Exception):
    """The review file on disk is not one this program can safely rewrite."""


def path(repo: Path) -> Path:
    """The review file of `repo`. `--absolute-git-dir`, not `.git/`: in a linked worktree `.git` is a file."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--absolute-git-dir"], cwd=repo, capture_output=True, text=True, check=False
        )
    except OSError as e:
        raise ValueError(f"Cannot run git in {repo}: {e}") from None
    if r.returncode:
        raise ValueError(f"{repo} is not a git repository: {r.stderr.strip()}")
    return Path(r.stdout.strip()) / "diffsplainer" / "review.json"


def _raw(p: Path) -> bytes | None:
    try:
        return p.read_bytes()
    except FileNotFoundError:
        return None


def _parse(p: Path, raw: bytes | None) -> dict:
    try:
        data = {} if raw is None else json.loads(raw.decode("utf-8"))
    except ValueError as e:  # a JSON or a UTF-8 error
        raise ReviewError(f"{p} is not valid JSON ({e}). Fix or delete it.") from None
    if not isinstance(data, dict):
        raise ReviewError(f"{p} does not hold a JSON object. Fix or delete it.")
    for key in ("reviews", "approved", "entries"):
        if not isinstance(data.setdefault(key, []), list):
            raise ReviewError(f"{p}: `{key}` is not a list. Fix or delete the file.")
    return data


def read(repo: Path) -> dict:
    p = path(repo)
    return _parse(p, _raw(p))


def last_head(data: dict) -> str | None:
    """The HEAD sha of the newest review round, or None when it is missing or not a sha."""
    newest = data["reviews"][-1] if data["reviews"] else None
    head = newest.get("head") if isinstance(newest, dict) else None
    return head if isinstance(head, str) and SHA_RE.fullmatch(head) else None


def update(repo: Path, change, attempts: int = 5) -> dict:
    """Let `change(data)` edit the file's data and write it back atomically. Returns the new data.

    The lock only covers this process, and the agent writes from another one. If the file differs from what was read
    when the write is about to replace it, `change` runs again on the fresh data, so it must be safe to run twice.
    """
    with _lock:
        p = path(repo)
        for _ in range(attempts):
            raw = _raw(p)
            data = _parse(p, raw)
            change(data)
            if _raw(p) != raw:
                continue  # shortcut: the agent can still write between this check and the replace
            p.parent.mkdir(exist_ok=True)
            tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")  # not a name the agent is likely to pick
            try:
                tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                os.replace(tmp, p)
            finally:
                tmp.unlink(missing_ok=True)
            return data
    raise ReviewError(f"{p.name} keeps changing; try again")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def start_review(repo: Path, head: str) -> dict:
    return update(repo, lambda d: d["reviews"].append({"head": head, "at": _now()}))


def approve(repo: Path, sha: str) -> dict:
    return update(repo, lambda d: None if sha in d["approved"] else d["approved"].append(sha))


def add_entry(repo: Path, kind: str, sha: str, file: str, lines: str, diff: str, quote: str | None, body: str) -> dict:
    def change(d):
        ids = [
            int(m[1]) for e in d["entries"] if isinstance(e, dict) and (m := re.fullmatch(r"r(\d+)", str(e.get("id"))))
        ]
        d["entries"].append(
            {
                "id": f"r{max(ids, default=0) + 1}",
                "kind": kind,
                "status": "open",
                "sha": sha,
                "file": file,
                "lines": lines,
                "diff": diff,
                "quote": quote,
                "body": body,
                "reply": None,
                "created": _now(),
            }
        )

    return update(repo, change)


def set_status(repo: Path, entry_id, status) -> dict:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")

    def change(d):
        found = next((e for e in d["entries"] if isinstance(e, dict) and e.get("id") == entry_id), None)
        if found is None:
            raise ValueError(f"No entry {entry_id!r}")
        found["status"] = status

    return update(repo, change)


def apply(repo: Path, req, shas: set[str]) -> dict:
    """Run one request from the page. `shas` are the commits under review: the only ones a request may name."""
    if not isinstance(req, dict):
        raise ValueError("The request must be a JSON object")  # noqa: TRY004 - the server answers ValueError with 400
    action = req.get("action")
    if action == "status":
        return set_status(repo, req.get("id"), req.get("status"))
    if action not in ("start", "approve", "change", "question"):
        raise ValueError(f"Unknown action {action!r}")
    sha = req.get("head" if action == "start" else "sha")
    if not isinstance(sha, str) or sha not in shas:
        raise ValueError("The commit is not in the range under review")
    if action == "start":
        return start_review(repo, sha)
    if action == "approve":
        return approve(repo, sha)
    fields = {k: req.get(k) for k in _ENTRY_FIELDS}
    if not all(isinstance(v, str) for v in fields.values()) or not fields["body"].strip():
        raise ValueError("file, lines, diff and body must be strings, and body must not be empty")
    quote = req.get("quote") if action == "question" else None
    if action == "question" and not (isinstance(quote, str) and quote.strip()):
        raise ValueError("A question needs the selected text in quote")
    return add_entry(repo, action, sha, quote=quote, **fields)
