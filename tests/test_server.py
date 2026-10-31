import socket
import threading
from types import SimpleNamespace

import pytest

from fearful_owl import claude, server

CHUNKS = [{"id": f"f0c{i}", "scope": "", "lines": [{"t": "add", "o": None, "n": i, "s": "x"}]} for i in range(2)]


def make_pr(base: str = "b1", head: str = "h1", additions: int = 10, deletions: int = 5) -> dict:
    return {
        "pr": {"number": 1, "baseSha": base, "headSha": head, "additions": additions, "deletions": deletions},
        "files": [{"filename": "a.py", "chunks": CHUNKS}],
        "_raw": {},
    }


@pytest.fixture
def explain(monkeypatch):
    server._explain_cache.clear()
    fake = SimpleNamespace(
        pr=make_pr(),
        ids=[c["id"] for c in CHUNKS],
        calls=[],
        overviews=[],
        cancels=[],
        overview_cancels=[],
        gone=threading.Event(),
        disconnect=False,
    )

    def explain_file(ctx, file, cancel=None):
        fake.calls.append(file)
        fake.cancels.append(cancel)
        if fake.disconnect:
            cancel.set()
            raise claude.ClaudeCancelled
        return {"chunks": [{"id": i, "title": "t", "what": "w", "why": "y"} for i in fake.ids]}

    def explain_overview(ctx, files, cancel=None):
        fake.overviews.append(files)
        fake.overview_cancels.append(cancel)
        return {"summary": "s", "approach": "a", "review_focus": []}

    def run():
        events = []
        server.Handler._explain(None, "o", "r", 1, 3, lambda e, p: events.append((e, p)), fake.gone)
        return events

    monkeypatch.setattr(server, "load_pr", lambda *_: fake.pr)
    monkeypatch.setattr(server, "project_instructions", lambda *_: [])
    monkeypatch.setattr(server, "related_pulls", lambda *_: [])
    monkeypatch.setattr(server, "explain_overview", explain_overview)
    monkeypatch.setattr(server, "explain_file", explain_file)
    fake.run = run
    return fake


def test_complete_result_is_cached(explain):
    explain.run()
    explain.run()
    assert len(explain.calls) == 1
    assert len(server._explain_cache) == 1


def test_result_missing_a_chunk_id_is_streamed_but_not_cached(explain):
    explain.ids = ["f0c0"]
    events = explain.run()
    assert [p["id"] for e, p in events if e == "chunk"] == ["f0c0"]
    assert events[-1][0] == "done"
    assert not server._explain_cache


def test_cache_key_includes_base_commit(explain):
    explain.run()
    explain.pr = make_pr(base="b2")
    explain.run()
    assert len(explain.calls) == 2


def test_pr_over_the_line_cap_skips_claude_and_reports_why(explain, monkeypatch):
    monkeypatch.setattr(server, "MAX_CHANGED_LINES", 1000)
    explain.pr = make_pr(additions=800, deletions=300)
    message = (
        "This PR changes 1100 lines. Fearful Owl explains PRs up to 1000 changed lines, "
        "so the diff is shown without explanations."
    )
    assert explain.run() == [("error", {"scope": "limit", "message": message})]
    assert not explain.calls and not explain.overviews
    assert not server._explain_cache


def test_pr_at_the_line_cap_is_explained(explain, monkeypatch):
    monkeypatch.setattr(server, "MAX_CHANGED_LINES", 1000)
    explain.pr = make_pr(additions=600, deletions=400)
    assert explain.run()[-1][0] == "done"
    assert len(explain.calls) == 1


def test_explain_calls_receive_the_disconnect_event(explain):
    explain.run()
    assert explain.cancels == [explain.gone]
    assert explain.overview_cancels == [explain.gone]


def test_disconnect_stops_queued_batches_without_error_events_or_caching(explain, monkeypatch):
    monkeypatch.setattr(server, "CONCURRENCY", 1)
    explain.pr["files"] = [{"filename": n, "chunks": [{**CHUNKS[0], "id": n}]} for n in ("a.py", "b.py")]
    explain.disconnect = True
    events = explain.run()
    assert len(explain.calls) == 1
    assert not [e for e, _ in events if e == "error"]
    assert not server._explain_cache


def test_watcher_sets_gone_when_the_client_closes_the_socket(monkeypatch):
    monkeypatch.setattr(server, "WATCH_INTERVAL_S", 0.05)
    ours, theirs = socket.socketpair()
    gone, stop = threading.Event(), threading.Event()
    threading.Thread(target=server.watch_disconnect, args=(ours, gone, stop), daemon=True).start()
    theirs.close()
    assert gone.wait(1)
    stop.set()
    ours.close()


def test_watcher_leaves_pending_request_bytes_unread(monkeypatch):
    monkeypatch.setattr(server, "WATCH_INTERVAL_S", 0.05)
    ours, theirs = socket.socketpair()
    gone, stop = threading.Event(), threading.Event()
    watcher = threading.Thread(target=server.watch_disconnect, args=(ours, gone, stop), daemon=True)
    watcher.start()
    theirs.send(b"x")
    assert not gone.wait(0.4)
    assert ours.recv(1) == b"x"
    stop.set()
    watcher.join(1)
    assert not watcher.is_alive()
    ours.close()
    theirs.close()


def _stub_github(monkeypatch):
    pull = {
        "number": 1,
        "title": "t",
        "html_url": "u",
        "state": "open",
        "base": {"ref": "main", "sha": "b"},
        "head": {"ref": "x", "sha": "h"},
    }
    monkeypatch.setattr(server.gh, "get_pull", lambda *_: pull)
    monkeypatch.setattr(server.gh, "get_pull_files", lambda *_: [])


def test_pr_cache_drops_least_recently_used_entry(monkeypatch):
    server._pr_cache.clear()
    _stub_github(monkeypatch)
    monkeypatch.setattr(server, "PR_CACHE_SIZE", 2)
    for num in (1, 2):
        server.load_pr("o", "r", num)
    server.load_pr("o", "r", 1)  # cache hit moves PR 1 to the end
    server.load_pr("o", "r", 3)
    assert list(server._pr_cache) == ["o/r/1", "o/r/3"]


def test_pr_cache_drops_expired_entry_when_seen(monkeypatch):
    server._pr_cache.clear()
    _stub_github(monkeypatch)
    server.load_pr("o", "r", 1)
    monkeypatch.setattr(server, "PR_TTL_S", 0)
    monkeypatch.setattr(server.gh, "get_pull", lambda *_: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        server.load_pr("o", "r", 1)
    assert not server._pr_cache
