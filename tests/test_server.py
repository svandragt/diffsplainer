import subprocess
import json
import socket
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from types import SimpleNamespace

import pytest

from diffsplainer import claude, server

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
        "This PR changes 1100 lines. Diffsplainer explains PRs up to 1000 changed lines, "
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


@pytest.fixture
def term(monkeypatch, serve):
    server._term_cache.clear()
    fake = SimpleNamespace(pr=make_pr(), calls=[], error=None, unhandled=[])

    def explain_term(ctx, chunk, text, around, cancel=None):
        fake.calls.append(SimpleNamespace(ctx=ctx, chunk=chunk, text=text, around=around, cancel=cancel))
        if fake.error:
            raise fake.error
        return {"meaning": "m", "here": "h"}

    monkeypatch.setattr(server, "load_pr", lambda *_: fake.pr)
    monkeypatch.setattr(server, "explain_term", explain_term)
    serve.handle_error = lambda *_: fake.unhandled.append(sys.exc_info()[1])

    def get(**params):
        url = f"http://127.0.0.1:{serve.server_port}/api/explain-term/o/r/1?{urllib.parse.urlencode(params)}"
        try:
            with urllib.request.urlopen(url) as res:
                return res.status, json.load(res)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    fake.get = get
    return fake


@pytest.mark.parametrize(
    "params",
    [{}, {"text": ""}, {"text": "x" * 301}, {"text": "term", "around": "x" * 4001}],
    ids=["missing text", "empty text", "text over 300 chars", "around over 4000 chars"],
)
def test_term_request_with_bad_text_or_paragraph_is_a_400_without_a_claude_call(term, params):
    status, body = term.get(**params)
    assert status == 400 and body["error"]
    assert not term.calls


def test_term_text_of_300_chars_and_paragraph_of_4000_chars_are_accepted(term):
    assert term.get(text="x" * 300, around="y" * 4000)[0] == 200


def test_term_for_an_unknown_chunk_is_a_404_without_a_claude_call(term):
    status, body = term.get(text="term", chunk="nope")
    assert status == 404 and body["error"]
    assert not term.calls


def test_term_overview_request_without_a_chunk_explains_with_chunk_none(term):
    assert term.get(text="conditional GET", around="Sends one.") == (200, {"meaning": "m", "here": "h"})
    call = term.calls[0]
    assert (call.chunk, call.text, call.around) == (None, "conditional GET", "Sends one.")
    assert call.ctx["pr"] is term.pr["pr"]
    assert isinstance(call.cancel, threading.Event)


def test_term_request_with_a_chunk_passes_that_chunk(term):
    assert term.get(text="term", chunk="f0c1")[0] == 200
    assert term.calls[0].chunk is CHUNKS[1]
    assert term.calls[0].around == ""


def test_term_answer_is_cached_per_head_chunk_text_and_paragraph(term):
    first = term.get(text="term", chunk="f0c0", around="p")
    assert term.get(text="term", chunk="f0c0", around="p") == first
    assert len(term.calls) == 1
    term.get(text="term", chunk="f0c1", around="p")
    term.get(text="term", chunk="f0c0", around="q")
    term.get(text="other", chunk="f0c0", around="p")
    term.pr = make_pr(head="h2")
    term.get(text="term", chunk="f0c0", around="p")
    assert len(term.calls) == 5


def test_term_cache_does_not_mix_up_requests_whose_text_and_paragraph_join_to_the_same_string(term):
    term.get(text="a:b", around="c")
    term.get(text="a", around="b:c")
    assert len(term.calls) == 2


def test_term_answer_is_not_reused_for_the_same_head_on_a_different_base(term):
    term.get(text="term")
    term.pr = make_pr(base="b2")
    term.get(text="term")
    assert len(term.calls) == 2


def test_term_cache_drops_least_recently_used_answer(term, monkeypatch):
    monkeypatch.setattr(server, "TERM_CACHE_SIZE", 2)
    for text in ("a", "b"):
        term.get(text=text)
    term.get(text="a")  # cache hit moves a to the end
    term.get(text="c")
    assert [k[-2] for k in server._term_cache] == ["a", "c"]


def test_term_claude_error_is_a_502_and_is_not_cached(term):
    term.error = claude.ClaudeError("boom")
    assert term.get(text="term") == (502, {"error": "boom"})
    term.error = None
    assert term.get(text="term")[0] == 200
    assert len(term.calls) == 2


def test_term_unexpected_claude_failure_is_a_502_with_an_error_message(term):
    term.error = KeyError("result")
    status, body = term.get(text="term")
    assert status == 502 and body["error"]
    assert not term.unhandled


@pytest.mark.parametrize("error", [BrokenPipeError, ConnectionResetError])
def test_term_client_gone_before_the_reply_is_sent_is_not_an_error(term, monkeypatch, error):
    def gone(*_):
        raise error

    monkeypatch.setattr(server.Handler, "send_body", gone)
    with pytest.raises(ConnectionError):  # the handler wrote nothing, so the client sees a closed connection
        term.get(text="term")
    assert not term.unhandled


def test_term_load_failure_is_a_500_with_an_error_message(term, monkeypatch):
    def fail(*_):
        raise RuntimeError("boom")

    monkeypatch.setattr(server, "load_pr", fail)
    assert term.get(text="term") == (500, {"error": "boom"})
    assert not term.calls


def test_log_line_omits_the_query_string(capsys):
    h = object.__new__(server.Handler)
    h.client_address = ("127.0.0.1", 1)
    h.path = "/api/explain-term/o/r/1?text=secret&around=private"
    h.requestline = f"GET {h.path} HTTP/1.1"
    h.log_request(200)
    err = capsys.readouterr().err
    assert "/api/explain-term/o/r/1" in err
    assert "secret" not in err and "private" not in err


def get_version(httpd) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_port}/api/version") as r:
        assert r.status == 200
        return json.load(r)


def test_version_reports_package_version_and_commit(serve):
    from importlib.metadata import version

    server.git_commit.cache_clear()
    data = get_version(serve)
    assert data["version"] == version("diffsplainer")
    assert isinstance(data["commit"], str) and data["commit"]


def test_version_commit_is_null_when_git_fails(serve, monkeypatch):
    def no_git(*a, **k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(server.subprocess, "run", no_git)
    server.git_commit.cache_clear()
    try:
        assert get_version(serve)["commit"] is None
    finally:
        server.git_commit.cache_clear()


def test_version_is_null_when_package_is_not_installed(serve, monkeypatch):
    from importlib.metadata import PackageNotFoundError

    def missing(_):
        raise PackageNotFoundError("diffsplainer")

    monkeypatch.setattr(server, "package_version", missing)
    assert get_version(serve)["version"] is None


def test_commit_is_null_when_checkout_is_another_repo(monkeypatch):
    def other_repo(*a, **k):
        return subprocess.CompletedProcess(a, 0, stdout="/somewhere/else\nabc1234\n", stderr="")

    monkeypatch.setattr(server.subprocess, "run", other_repo)
    server.git_commit.cache_clear()
    try:
        assert server.git_commit() is None
    finally:
        server.git_commit.cache_clear()
