"""PR review app: diff chunks on the right, Claude Code explanations aligned on the left."""

from __future__ import annotations

import json
import mimetypes
import os
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import github as gh
from .claude import explain_file, explain_overview
from .context import project_instructions, related_pulls
from .diff import chunk_files

PORT = int(os.environ.get("PORT", "3000"))
CONCURRENCY = int(os.environ.get("CLAUDE_CONCURRENCY", "3"))
MAX_PRS = 10
BATCH_CHUNKS = 12
BATCH_LINES = 400
PR_TTL_S = 60
EXPLAIN_CACHE_SIZE = 50
STATIC = Path(__file__).parent / "static"

PR_RE = re.compile(r"^/api/pr/([\w.-]+)/([\w.-]+)/(\d+)$")
EXPLAIN_RE = re.compile(r"^/api/explain/([\w.-]+)/([\w.-]+)/(\d+)$")

_pr_cache: dict[str, tuple[float, dict]] = {}
_explain_cache: OrderedDict[str, list] = OrderedDict()
_cache_lock = threading.Lock()


def load_pr(owner: str, repo: str, num: int) -> dict:
    key = f"{owner}/{repo}/{num}"
    with _cache_lock:
        hit = _pr_cache.get(key)
    if hit and time.time() - hit[0] < PR_TTL_S:
        return hit[1]
    with ThreadPoolExecutor(max_workers=2) as pool:
        pr_f = pool.submit(gh.get_pull, owner, repo, num)
        files_f = pool.submit(gh.get_pull_files, owner, repo, num)
        pr, raw_files = pr_f.result(), files_f.result()
    data = {
        "pr": {
            "number": pr["number"],
            "title": pr["title"],
            "body": pr.get("body") or "",
            "url": pr["html_url"],
            "state": "merged" if pr.get("merged_at") else pr["state"],
            "draft": pr.get("draft", False),
            "user": (pr.get("user") or {}).get("login"),
            "avatar": (pr.get("user") or {}).get("avatar_url"),
            "base": pr["base"]["ref"],
            "head": pr["head"]["ref"],
            "baseSha": pr["base"]["sha"],
            "headSha": pr["head"]["sha"],
            "additions": pr.get("additions", 0),
            "deletions": pr.get("deletions", 0),
            "changed_files": pr.get("changed_files", len(raw_files)),
        },
        "files": chunk_files(raw_files),
        "_raw": pr,
    }
    with _cache_lock:
        _pr_cache[key] = (time.time(), data)
    return data


def batches(file: dict) -> list[dict]:
    """Split a file's chunks into prompt-sized batches."""
    out, cur, lines = [], [], 0
    for c in file["chunks"]:
        if cur and (len(cur) >= BATCH_CHUNKS or lines + len(c["lines"]) > BATCH_LINES):
            out.append(cur)
            cur, lines = [], 0
        cur.append(c)
        lines += len(c["lines"])
    if cur:
        out.append(cur)
    return [{**file, "chunks": b} for b in out]


class ClientGone(Exception):
    pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter logs: skip static assets
        if "/api/" in self.path:
            super().log_message(fmt, *args)

    def do_GET(self):
        url = urlparse(self.path)
        path = unquote(url.path)
        if m := PR_RE.match(path):
            return self.api_pr(m[1], m[2], int(m[3]))
        if m := EXPLAIN_RE.match(path):
            try:
                prs = int(parse_qs(url.query).get("prs", ["3"])[0])
            except ValueError:
                prs = 3
            return self.api_explain(m[1], m[2], int(m[3]), max(0, min(MAX_PRS, prs)))
        return self.static(path)

    # --- helpers -------------------------------------------------------

    def send_body(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, code: int, obj):
        self.send_body(code, json.dumps(obj).encode(), "application/json")

    def static(self, path: str):
        # Real assets are served as-is; any other path (e.g. /owner/repo/pull/123) gets the app shell.
        target = (STATIC / path.lstrip("/")).resolve()
        if not (target.is_file() and target.is_relative_to(STATIC.resolve())):
            target = STATIC / "index.html"
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self.send_body(200, target.read_bytes(), ctype)

    # --- API -----------------------------------------------------------

    def api_pr(self, owner: str, repo: str, num: int):
        try:
            data = load_pr(owner, repo, num)
        except gh.GitHubError as e:
            return self.send_json(e.status, {"error": str(e)})
        except Exception as e:
            return self.send_json(500, {"error": str(e)})
        self.send_json(200, {k: v for k, v in data.items() if k != "_raw"})

    def api_explain(self, owner: str, repo: str, num: int, pr_limit: int):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        write_lock = threading.Lock()
        gone = threading.Event()

        def send(event: str, data):
            if gone.is_set():
                raise ClientGone
            msg = f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()
            with write_lock:
                try:
                    self.wfile.write(msg)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    gone.set()
                    raise ClientGone from None

        try:
            self._explain(owner, repo, num, pr_limit, send, gone)
        except ClientGone:
            pass
        except Exception as e:
            try:
                send("error", {"scope": "fatal", "message": str(e)})
            except ClientGone:
                pass

    def _explain(self, owner, repo, num, pr_limit, send, gone):
        data = load_pr(owner, repo, num)
        key = f"{owner}/{repo}/{num}@{data['pr']['headSha']}:{pr_limit}"
        with _cache_lock:
            cached = _explain_cache.get(key)
            if cached:
                _explain_cache.move_to_end(key)
        if cached:
            for event, payload in cached:
                send(event, payload)
            return send("done", {})

        log: list = []

        def emit(event, payload):
            log.append((event, payload))
            send(event, payload)

        paths = [f["filename"] for f in data["files"]]
        send("status", {"message": "Loading project instructions and related PRs…"})
        with ThreadPoolExecutor(max_workers=2) as pool:
            instr_f = pool.submit(project_instructions, owner, repo, data["pr"]["baseSha"], paths)
            rel_f = pool.submit(related_pulls, owner, repo, data["_raw"], paths, pr_limit)
            instructions, related = instr_f.result(), rel_f.result()
        emit(
            "context",
            {
                "instructions": [d["path"] for d in instructions],
                "related": [{k: v for k, v in r.items() if k != "body"} for r in related],
            },
        )

        ctx = {"pr": data["pr"], "instructions": instructions, "related": related, "files": data["files"]}
        work = [b for f in data["files"] if f["chunks"] for b in batches(f)]
        total = len(work) + 1
        send("status", {"message": f"Asking Claude Code to explain {len(work)} file batch(es)…", "done": 0, "total": total})

        def overview_task():
            if gone.is_set():
                return
            try:
                emit("overview", explain_overview(ctx, data["files"]))
                return True
            except ClientGone:
                raise
            except Exception as e:
                send("error", {"scope": "overview", "message": str(e)})
                return False

        def file_task(file):
            if gone.is_set():
                return
            try:
                out = explain_file(ctx, file)
            except Exception as e:
                send("error", {"scope": "file", "ids": [c["id"] for c in file["chunks"]], "message": str(e)})
                return False
            valid = {c["id"] for c in file["chunks"]}
            for c in out.get("chunks", []):
                if c.get("id") in valid:
                    emit("chunk", c)
            return True

        ok = True
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            futures = [pool.submit(overview_task)] + [pool.submit(file_task, f) for f in work]
            for done, fut in enumerate(as_completed(futures), 1):
                try:
                    ok = fut.result() is True and ok
                    send("status", {"done": done, "total": total})
                except ClientGone:
                    gone.set()
                    for f in futures:
                        f.cancel()
                    raise

        if ok:
            with _cache_lock:
                _explain_cache[key] = log
                while len(_explain_cache) > EXPLAIN_CACHE_SIZE:
                    _explain_cache.popitem(last=False)
        send("done", {})


def main():
    server = ThreadingHTTPServer(("", PORT), Handler)
    server.daemon_threads = True
    print(f"PR review app on http://localhost:{PORT}")
    print(f"Open e.g. http://localhost:{PORT}/owner/repo/pull/123")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
