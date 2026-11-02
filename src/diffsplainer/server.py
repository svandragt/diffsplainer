"""PR review app: diff chunks on the right, Claude Code explanations aligned on the left."""

from __future__ import annotations

import json
import mimetypes
import os
import re
import select
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import github as gh
from .claude import (
    ClaudeCancelled,
    ClaudeError,
    explain_file,
    explain_overview,
    explain_term,
)
from .context import project_instructions, related_pulls
from .diff import chunk_files

PORT = int(os.environ.get("PORT", "3000"))
CONCURRENCY = int(os.environ.get("CLAUDE_CONCURRENCY", "6"))
MAX_CHANGED_LINES = int(os.environ.get("MAX_CHANGED_LINES", "1000"))
MAX_PRS = 10
BATCH_CHUNKS = 12
BATCH_LINES = 400
PR_TTL_S = 60
PR_CACHE_SIZE = 50
EXPLAIN_CACHE_SIZE = 50
TERM_CACHE_SIZE = 200
MAX_TERM_CHARS = 300
MAX_AROUND_CHARS = 4000
WATCH_INTERVAL_S = 0.5
STATIC = Path(__file__).parent / "static"

# `.` and `..` would turn the GitHub API path into a different endpoint
_REPO = r"((?!\.\.?/)[\w.-]+)/((?!\.\.?/)[\w.-]+)/(\d+)$"
PR_RE = re.compile(r"^/api/pr/" + _REPO)
EXPLAIN_RE = re.compile(r"^/api/explain/" + _REPO)
TERM_RE = re.compile(r"^/api/explain-term/" + _REPO)
# Any host, so GitHub Enterprise PR URLs work like they do in the bookmarklet
CLI_PR_RE = re.compile(r"(?:^|/)((?!\.\.?/)[\w.-]+)/((?!\.\.?/)[\w.-]+)/pull/(\d+)(?:[/?#]|$)")
LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")

_pr_cache: OrderedDict[str, tuple[float, dict]] = OrderedDict()
_explain_cache: OrderedDict[str, list] = OrderedDict()
_term_cache: OrderedDict[tuple, dict] = OrderedDict()
_cache_lock = threading.Lock()


def load_pr(owner: str, repo: str, num: int) -> dict:
    key = f"{owner}/{repo}/{num}"
    with _cache_lock:
        hit = _pr_cache.get(key)
        if hit and time.time() - hit[0] >= PR_TTL_S:
            del _pr_cache[key]
            hit = None
        elif hit:
            _pr_cache.move_to_end(key)
    if hit:
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
        _pr_cache.move_to_end(key)
        while len(_pr_cache) > PR_CACHE_SIZE:
            _pr_cache.popitem(last=False)
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


@cache
def git_commit() -> str | None:
    """Short SHA of the checkout the package runs from; None outside a checkout."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel", "--short", "HEAD"],
            cwd=Path(__file__).parent,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    top, _, sha = r.stdout.strip().partition("\n")
    # a venv inside another repo would report that repo's SHA; src/ layout puts the checkout at parents[2]
    ours = r.returncode == 0 and Path(top).resolve() == Path(__file__).resolve().parents[2]
    return sha or None if ours else None


def host_name(host: str) -> str:
    """The Host header without its port, lowercased. The port is ignored because SSH tunnels change it."""
    name, colon, port = host.rpartition(":")
    return (name if colon and port.isdigit() else host).lower()


class ClientGone(Exception):
    pass


def watch_disconnect(sock: socket.socket, gone: threading.Event, stop: threading.Event):
    """Set `gone` once the client closes `sock`. The peek leaves pending request bytes unread."""
    while not stop.wait(WATCH_INTERVAL_S):
        try:
            if select.select([sock], [], [], 0)[0] and not sock.recv(1, socket.MSG_PEEK):
                gone.set()
        except (OSError, ValueError):  # ValueError: the handler closed the socket under us
            gone.set()
        if gone.is_set():
            return


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter logs: skip static assets
        if "/api/" in self.path:
            # the query string holds the selected text and paragraph from private PRs
            super().log_message(fmt, *(re.sub(r"\?\S*", "", a) if isinstance(a, str) else a for a in args))

    def do_GET(self):
        url = urlparse(self.path)
        path = unquote(url.path)
        # A DNS-rebinding page sends its own domain as Host, and any page can send cross-site API requests that start
        # paid Claude runs; the bookmarklet only navigates to a page, so it still works.
        if host_name(self.headers.get("Host", "")) not in LOCAL_HOSTS:
            return self.send_json(403, {"error": "Diffsplainer only answers requests to localhost"})
        if path.startswith("/api/") and self.headers.get("Sec-Fetch-Site", "none") not in ("same-origin", "none"):
            return self.send_json(403, {"error": "Diffsplainer only answers API requests from its own pages"})
        if path == "/api/version":
            try:
                ver = package_version("diffsplainer")
            except PackageNotFoundError:
                ver = None
            return self.send_json(200, {"version": ver, "commit": git_commit()})
        if m := PR_RE.match(path):
            return self.api_pr(m[1], m[2], int(m[3]))
        if m := EXPLAIN_RE.match(path):
            try:
                prs = int(parse_qs(url.query).get("prs", ["3"])[0])
            except ValueError:
                prs = 3
            return self.api_explain(m[1], m[2], int(m[3]), max(0, min(MAX_PRS, prs)))
        if m := TERM_RE.match(path):
            return self.api_explain_term(m[1], m[2], int(m[3]), parse_qs(url.query))
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
        except Exception as e:  # noqa: BLE001 - report any failure to the client
            return self.send_json(500, {"error": str(e)})
        self.send_json(200, {k: v for k, v in data.items() if k != "_raw"})

    def api_explain_term(self, owner: str, repo: str, num: int, query: dict):
        text = query.get("text", [""])[0].strip()
        around = query.get("around", [""])[0]
        chunk_id = query.get("chunk", [None])[0]
        if not text or len(text) > MAX_TERM_CHARS:
            return self.send_json(400, {"error": f"text must be 1 to {MAX_TERM_CHARS} characters"})
        if len(around) > MAX_AROUND_CHARS:
            return self.send_json(400, {"error": f"around must be at most {MAX_AROUND_CHARS} characters"})
        try:
            data = load_pr(owner, repo, num)
        except gh.GitHubError as e:
            return self.send_json(e.status, {"error": str(e)})
        except Exception as e:  # noqa: BLE001 - report any failure to the client
            return self.send_json(500, {"error": str(e)})
        chunk = None
        if chunk_id is not None:
            chunk = next((c for f in data["files"] for c in f["chunks"] if c["id"] == chunk_id), None)
            if chunk is None:
                return self.send_json(404, {"error": f"Unknown chunk {chunk_id}"})
        key = (owner, repo, num, data["pr"]["baseSha"], data["pr"]["headSha"], chunk_id or "", text, around)
        with _cache_lock:
            answer = _term_cache.get(key)
            if answer:
                _term_cache.move_to_end(key)
        if answer:
            return self.send_json(200, answer)

        gone, stop = threading.Event(), threading.Event()
        threading.Thread(target=watch_disconnect, args=(self.connection, gone, stop), daemon=True).start()
        try:
            answer = explain_term({"pr": data["pr"], "files": data["files"]}, chunk, text, around, gone)
        except ClaudeCancelled:
            return
        except ClaudeError as e:
            return self.send_json(502, {"error": str(e)})
        except Exception as e:  # noqa: BLE001 - report any failure to the client
            return self.send_json(502, {"error": str(e)})
        finally:
            stop.set()
        with _cache_lock:
            _term_cache[key] = answer
            while len(_term_cache) > TERM_CACHE_SIZE:
                _term_cache.popitem(last=False)
        try:
            self.send_json(200, answer)
        except (BrokenPipeError, ConnectionResetError):
            pass

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

        stop = threading.Event()
        threading.Thread(target=watch_disconnect, args=(self.connection, gone, stop), daemon=True).start()
        try:
            self._explain(owner, repo, num, pr_limit, send, gone)
        except ClientGone:
            pass
        except Exception as e:  # noqa: BLE001 - report any failure to the client
            try:
                send("error", {"scope": "fatal", "message": str(e)})
            except ClientGone:
                pass
        finally:
            stop.set()

    def _explain(self, owner, repo, num, pr_limit, send, gone):
        data = load_pr(owner, repo, num)
        changed = data["pr"]["additions"] + data["pr"]["deletions"]
        if changed > MAX_CHANGED_LINES:
            return send(
                "error",
                {
                    "scope": "limit",
                    "message": f"This PR changes {changed} lines. Diffsplainer explains PRs up to "
                    f"{MAX_CHANGED_LINES} changed lines, so the diff is shown without explanations.",
                },
            )
        key = f"{owner}/{repo}/{num}@{data['pr']['baseSha']}..{data['pr']['headSha']}:{pr_limit}"
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
        send(
            "status",
            {"message": f"Asking Claude Code to explain {len(work)} file batch(es)…", "done": 0, "total": total},
        )

        def overview_task():
            if gone.is_set():
                return
            try:
                emit("overview", explain_overview(ctx, data["files"], gone))
                return True
            except ClientGone:
                raise
            except ClaudeCancelled:
                return
            except Exception as e:  # noqa: BLE001 - report any failure to the client
                send("error", {"scope": "overview", "message": str(e)})
                return False

        def file_task(file):
            if gone.is_set():
                return
            try:
                out = explain_file(ctx, file, gone)
            except ClaudeCancelled:
                return
            except Exception as e:  # noqa: BLE001 - report any failure to the client
                send("error", {"scope": "file", "ids": [c["id"] for c in file["chunks"]], "message": str(e)})
                return False
            valid = {c["id"] for c in file["chunks"]}
            seen = set()
            for c in out.get("chunks", []):
                if c.get("id") in valid:
                    emit("chunk", c)
                    seen.add(c["id"])
            # Rows left out show "No explanation returned" on the page; the incomplete log must not be cached.
            return seen == valid

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


def pr_path(arg: str) -> str | None:
    m = CLI_PR_RE.search(arg)
    return m and f"/{m[1]}/{m[2]}/pull/{m[3]}"


def main():
    path = None
    if len(sys.argv) > 1:
        path = pr_path(sys.argv[1])
        if not path:
            sys.exit(
                f"Not a pull request URL: {sys.argv[1]}\nUsage: diffsplainer [https://github.com/owner/repo/pull/123]"
            )
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    print(f"PR review app on http://localhost:{PORT}")
    print(f"Open e.g. http://localhost:{PORT}/owner/repo/pull/123")
    if path:
        webbrowser.open(f"http://localhost:{PORT}{path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
