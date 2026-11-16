import os
import socket
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from diffsplainer import server


@pytest.fixture
def serve():
    """The app's HTTP server on a free port; yields the server, whose port is `server_port`."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()  # fast shutdown()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:  # shutdown() hangs if serve_forever() has not started yet
        try:
            socket.create_connection(("127.0.0.1", httpd.server_port), timeout=0.1).close()
            break
        except OSError:
            time.sleep(0.01)
    yield httpd
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A work repo on `main` with a bare upstream, and two commits (`a`, `b`) after the upstream's tip (`base`).

    Commit `a` adds lines to a.py, adds b.py and deletes drop.txt. Commit `b` deletes a line of gone.txt and edits the
    last line of b.py. `dirty()` leaves one staged and one unstaged change, so tests can show that nothing touches
    them; call it last, because `commit()` stages everything."""
    for k, v in {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "T",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "T",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }.items():
        monkeypatch.setenv(k, v)
    path = tmp_path / "work"

    def git(*args, cwd=path):
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()

    def commit(message):
        git("add", "-A")
        git("commit", "-q", "-m", message)
        return git("rev-parse", "HEAD")

    git("init", "-q", "--bare", "-b", "main", str(tmp_path / "upstream.git"), cwd=tmp_path)
    git("init", "-q", "-b", "main", str(path), cwd=tmp_path)
    git("remote", "add", "origin", str(tmp_path / "upstream.git"))
    (path / "a.py").write_text("".join(f"line{i}\n" for i in range(1, 11)))
    (path / "gone.txt").write_text("x\ny\nz\n")
    (path / "drop.txt").write_text("p\nq\n")
    (path / "AGENTS.md").write_text("Project rules.\n")
    base = commit("Base")
    git("push", "-q", "-u", "origin", "main")
    with (path / "a.py").open("a") as f:
        f.write("added1\nadded2\n")
    (path / "b.py").write_text("one\ntwo\nthree\n")
    (path / "drop.txt").unlink()
    a = commit("Add greeting\n\nBody of a.")
    (path / "gone.txt").write_text("x\nz\n")
    (path / "b.py").write_text("one\ntwo\nTHREE\n")
    b = commit("Trim gone.txt and edit b.py")

    def dirty():
        (path / "staged.txt").write_text("staged\n")
        git("add", "staged.txt")
        (path / "a.py").write_text("unstaged edit\n")

    def state():
        """What a read-only tool must leave as it is: status, index, refs and the unstaged file."""
        return {
            "status": git("status", "--porcelain=v2", "--branch"),
            "index": git("ls-files", "--stage"),
            "refs": git("for-each-ref"),
            "work tree": (path / "a.py").read_text(),
        }

    return SimpleNamespace(path=path, git=git, commit=commit, dirty=dirty, state=state, base=base, a=a, b=b)
