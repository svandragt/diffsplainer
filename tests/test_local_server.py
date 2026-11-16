import http.client
import json
import sys
import urllib.parse
from types import SimpleNamespace

import pytest

from diffsplainer import server

SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin", "Content-Type": "application/json"}


def send(serve, method, path, body=None, headers=None, length=None):
    """One request; `headers` replace the defaults. Returns the status and the parsed JSON (or text) body."""
    conn = http.client.HTTPConnection("127.0.0.1", serve.server_port, timeout=5)
    conn.putrequest(method, path, skip_host="Host" in (headers or {}))
    for k, v in (headers or {}).items():
        conn.putheader(k, v)
    data = body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else None
    if length is not None or data is not None:
        conn.putheader("Content-Length", str(len(data) if length is None else length))
    conn.endheaders(data)
    res = conn.getresponse()
    text = res.read().decode()
    conn.close()
    try:
        return res.status, json.loads(text)
    except ValueError:
        return res.status, text


def post(serve, body, length=None, **headers):
    return send(serve, "POST", "/api/review", body, {**SAME_ORIGIN, **headers}, length)


@pytest.fixture
def local_repo(repo, monkeypatch):
    monkeypatch.setattr(server, "LOCAL_REPO", repo.path)
    return repo


def review_file(repo):
    p = repo.path / ".git" / "diffsplainer" / "review.json"
    return json.loads(p.read_text()) if p.exists() else None


def test_api_local_returns_the_commits_the_tagged_chunks_and_the_review(serve, local_repo):
    status, data = send(serve, "GET", "/api/local")
    assert status == 200
    assert [c["sha"] for c in data["local"]["commits"]] == [local_repo.a, local_repo.b]
    assert {c["sha"] for f in data["files"] if f["filename"] == "b.py" for c in f["chunks"]} == {local_repo.b}
    assert data["review"] == {"reviews": [], "approved": [], "entries": []}


def test_api_local_reports_a_range_that_cannot_be_found(serve, local_repo):
    local_repo.git("switch", "-q", "-c", "feature")
    local_repo.git("remote", "remove", "origin")
    local_repo.git("branch", "-m", "main", "trunk")
    status, data = send(serve, "GET", "/api/local")
    assert status == 500 and "default branch" in data["error"]


@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "/api/local"),
        ("GET", "/api/local/explain"),
        ("GET", "/api/local/explain-term?text=x"),
        ("GET", "/api/review"),
    ],
)
def test_local_routes_are_404_in_pr_mode(serve, monkeypatch, method, path):
    monkeypatch.setattr(server, "LOCAL_REPO", None)
    assert send(serve, method, path)[0] == 404


def test_posting_a_review_is_404_in_pr_mode(serve, monkeypatch):
    monkeypatch.setattr(server, "LOCAL_REPO", None)
    assert post(serve, {"action": "start", "head": "a" * 40})[0] == 404


def test_get_on_the_review_endpoint_is_405(serve, local_repo):
    assert send(serve, "GET", "/api/review")[0] == 405


def test_the_approve_action_is_gone(serve, local_repo):
    assert post(serve, {"action": "approve", "sha": local_repo.a})[0] == 400
    assert review_file(local_repo) is None


def test_start_with_approve_over_http_records_one_round_and_approves_every_commit(serve, local_repo):
    status, data = post(serve, {"action": "start", "head": local_repo.b, "approve": True})
    assert status == 200 and sorted(data["review"]["approved"]) == sorted([local_repo.a, local_repo.b])
    got = review_file(local_repo)
    assert sorted(got["approved"]) == sorted([local_repo.a, local_repo.b])
    assert [r["head"] for r in got["reviews"]] == [local_repo.b]


def test_start_without_approve_over_http_approves_nothing(serve, local_repo):
    assert post(serve, {"action": "start", "head": local_repo.b})[0] == 200
    assert review_file(local_repo)["approved"] == []


def test_start_with_approve_is_a_400_while_an_entry_is_open_and_writes_nothing_more(serve, local_repo):
    chunk = {"sha": local_repo.a, "file": "a.py", "lines": "1", "diff": "+x", "body": "b"}
    assert post(serve, {"action": "change", **chunk})[0] == 200
    before = review_file(local_repo)
    status, data = post(serve, {"action": "start", "head": local_repo.b, "approve": True})
    assert status == 400 and "open" in data["error"]
    assert review_file(local_repo) == before


def test_request_changes_and_a_comment_over_http_add_entries(serve, local_repo):
    chunk = {"sha": local_repo.a, "file": "a.py", "lines": "11-12", "diff": "+added1\n+added2"}
    assert post(serve, {"action": "change", "body": "Rename it.", **chunk})[0] == 200
    assert post(serve, {"action": "question", "body": "Why?", "quote": "added1", **chunk})[0] == 200
    assert post(serve, {"action": "status", "id": "r1", "status": "resolved"})[0] == 200
    entries = review_file(local_repo)["entries"]
    assert [(e["id"], e["kind"], e["status"], e["quote"], e["sha"]) for e in entries] == [
        ("r1", "change", "resolved", None, local_repo.a),
        ("r2", "question", "open", "added1", local_repo.a),
    ]


def test_start_over_http_records_the_head_and_the_next_load_shows_only_new_commits(serve, local_repo):
    assert post(serve, {"action": "start", "head": local_repo.b})[0] == 200
    local_repo.dirty()
    local_repo.git("add", "-A")
    local_repo.git("commit", "-q", "-m", "fixup! Add greeting")
    data = send(serve, "GET", "/api/local")[1]
    assert [c["subject"] for c in data["local"]["commits"]] == ["fixup! Add greeting"]
    assert data["review"]["reviews"][0]["head"] == local_repo.b


def test_a_commit_outside_the_range_is_a_400_and_writes_nothing(serve, local_repo):
    assert post(serve, {"action": "start", "head": local_repo.base})[0] == 400
    assert post(serve, {"action": "start", "head": "--output=x", "approve": True})[0] == 400
    assert review_file(local_repo) is None


@pytest.mark.parametrize("body", [b"{nope", b"[]", b'"x"', b"\xff\xfe"])
def test_a_body_that_is_not_a_json_object_is_a_400(serve, local_repo, body):
    assert post(serve, body)[0] == 400
    assert review_file(local_repo) is None


START = {"action": "start", "head": "a" * 40}


@pytest.mark.parametrize(
    "headers, status",
    [
        ({"Host": "attacker.example"}, 403),
        ({"Host": "localhost.attacker.example:3000"}, 403),
        ({"Sec-Fetch-Site": "cross-site"}, 403),
        ({"Sec-Fetch-Site": "same-site"}, 403),
        ({"Sec-Fetch-Site": "none"}, 403),
        ({"Content-Type": "text/plain"}, 415),
        ({"Content-Type": "application/x-www-form-urlencoded"}, 415),
    ],
)
def test_a_post_that_is_not_from_the_apps_own_page_is_refused_and_writes_nothing(serve, local_repo, headers, status):
    assert post(serve, {**START, "head": local_repo.a}, **headers)[0] == status
    assert review_file(local_repo) is None


def test_a_post_without_a_sec_fetch_site_header_is_refused(serve, local_repo):
    body = {**START, "head": local_repo.a}
    assert send(serve, "POST", "/api/review", body, {"Content-Type": "application/json"})[0] == 403
    assert review_file(local_repo) is None


def test_a_post_without_a_content_type_is_refused(serve, local_repo):
    body = {**START, "head": local_repo.a}
    assert send(serve, "POST", "/api/review", body, {"Sec-Fetch-Site": "same-origin"})[0] == 415
    assert review_file(local_repo) is None


def test_a_post_with_a_foreign_host_is_refused_in_pr_mode_too(serve, monkeypatch):
    monkeypatch.setattr(server, "LOCAL_REPO", None)
    assert post(serve, START, Host="attacker.example")[0] == 403


def test_a_post_without_a_length_is_refused(serve, local_repo):
    status, _ = send(serve, "POST", "/api/review", None, SAME_ORIGIN)
    assert status == 411


def test_a_post_over_the_size_cap_is_refused_without_reading_the_body(serve, local_repo):
    assert post(serve, b"", length=server.MAX_REVIEW_BYTES + 1)[0] == 413
    assert review_file(local_repo) is None


def test_a_post_at_the_size_cap_is_read(serve, local_repo):
    pad = "x" * (server.MAX_REVIEW_BYTES - 200)
    body = json.dumps({"action": "change", "sha": local_repo.a, "file": "f", "lines": "1", "diff": pad, "body": "b"})
    assert len(body) <= server.MAX_REVIEW_BYTES
    assert post(serve, body.encode())[0] == 200


def test_a_post_to_another_path_is_404(serve, local_repo):
    assert send(serve, "POST", "/api/local", {}, SAME_ORIGIN)[0] == 404


def test_the_request_cannot_name_a_repository(serve, local_repo, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    post(serve, {**START, "head": local_repo.a, "repo": str(other), "path": str(other)})
    assert [r["head"] for r in review_file(local_repo)["reviews"]] == [local_repo.a]
    assert list(other.iterdir()) == []


def test_a_review_file_that_is_not_valid_json_is_a_409_and_stays_as_it_is(serve, local_repo):
    p = local_repo.path / ".git" / "diffsplainer"
    p.mkdir()
    (p / "review.json").write_text("{broken")
    status, data = post(serve, {**START, "head": local_repo.a})
    assert status == 409 and "not valid JSON" in data["error"]
    assert (p / "review.json").read_text() == "{broken"
    assert send(serve, "GET", "/api/local")[0] == 500


def test_no_write_endpoint_changes_the_work_tree_the_index_or_any_ref(serve, local_repo):
    local_repo.dirty()
    before = local_repo.state()
    chunk = {"sha": local_repo.a, "file": "a.py", "lines": "1", "diff": "+x", "body": "b"}
    for body in [
        {"action": "change", **chunk},
        {"action": "question", "quote": "x", **chunk},
        {"action": "status", "id": "r1", "status": "resolved"},
        {"action": "status", "id": "r2", "status": "resolved"},
        {"action": "start", "head": local_repo.b, "approve": True},
    ]:
        assert post(serve, body)[0] == 200
    send(serve, "GET", "/api/local")
    assert local_repo.state() == before


@pytest.fixture
def fake_claude(monkeypatch):
    server._explain_cache.clear()
    calls = SimpleNamespace(files=[], overviews=[])

    def explain_file(ctx, file, cancel=None):
        calls.files.append((ctx, file))
        return {"chunks": [{"id": c["id"], "title": "t", "what": "w", "why": "y"} for c in file["chunks"]]}

    def explain_overview(ctx, files, cancel=None):
        calls.overviews.append(ctx)
        return {"summary": "s", "approach": "a", "review_focus": []}

    monkeypatch.setattr(server, "explain_file", explain_file)
    monkeypatch.setattr(server, "explain_overview", explain_overview)
    return calls


def explain_events(serve):
    status, text = send(serve, "GET", "/api/local/explain")
    assert status == 200
    blocks = [b.split("\n", 1) for b in text.strip().split("\n\n")]
    return [(e.removeprefix("event: "), json.loads(d.removeprefix("data: "))) for e, d in blocks]


def test_explaining_local_commits_streams_the_chunks_with_the_commit_context(serve, local_repo, fake_claude):
    events = explain_events(serve)
    names = [e for e, _ in events]
    assert names.count("chunk") == 4 and names.count("overview") == 1 and names[-1] == "done"
    assert ("context", {"instructions": ["AGENTS.md"], "related": []}) in events
    ctx, _ = fake_claude.files[0]
    assert ctx["local"]["branch"] == "main"
    assert [c["subject"] for c in ctx["local"]["commits"]] == ["Add greeting", "Trim gone.txt and edit b.py"]
    assert ctx["instructions"] == [{"path": "AGENTS.md", "body": "Project rules.\n"}]


def test_a_second_explain_of_the_same_base_and_head_is_served_from_the_cache(serve, local_repo, fake_claude):
    def results():  # the stream order varies with the thread timing, and a replay has no progress events
        return sorted(json.dumps(e) for e in explain_events(serve) if e[0] != "status")

    first = results()
    assert results() == first
    assert len(fake_claude.files) == 4


def test_a_new_commit_gets_new_explanations(serve, local_repo, fake_claude):
    explain_events(serve)
    (local_repo.path / "new.txt").write_text("n\n")
    local_repo.commit("More")
    explain_events(serve)
    assert len(fake_claude.files) == 4 + 5  # the first run explained 4 files, the second 5


def test_an_empty_range_is_not_explained(serve, local_repo, fake_claude):
    local_repo.git("push", "-q")
    assert explain_events(serve) == [("done", {})]
    assert fake_claude.files == [] and fake_claude.overviews == []


def test_a_range_over_the_line_cap_skips_claude_and_says_why(serve, local_repo, fake_claude, monkeypatch):
    monkeypatch.setattr(server, "MAX_CHANGED_LINES", 3)
    ((event, payload),) = explain_events(serve)
    assert (event, payload["scope"]) == ("error", "limit") and "commits change" in payload["message"]
    assert fake_claude.files == []


@pytest.fixture
def term(serve, local_repo, monkeypatch):
    server._term_cache.clear()
    calls = []

    def explain_term(ctx, chunk, text, around, cancel=None):
        calls.append(SimpleNamespace(ctx=ctx, chunk=chunk, text=text, around=around))
        return {"meaning": "m", "here": "h"}

    monkeypatch.setattr(server, "explain_term", explain_term)

    def get(**params):
        return send(serve, "GET", "/api/local/explain-term?" + urllib.parse.urlencode(params))

    return SimpleNamespace(calls=calls, get=get)


def test_a_term_in_local_commits_is_explained_with_the_local_context_and_chunk(term, local_repo, serve):
    chunk = send(serve, "GET", "/api/local")[1]["files"][0]["chunks"][0]
    assert term.get(text="added1", around="Adds a line.", chunk=chunk["id"]) == (200, {"meaning": "m", "here": "h"})
    call = term.calls[0]
    assert (call.chunk["id"], call.text, call.around) == (chunk["id"], "added1", "Adds a line.")
    assert call.ctx["local"]["branch"] == "main" and "pr" not in call.ctx
    assert [c["sha"] for c in call.ctx["local"]["commits"]] == [local_repo.a, local_repo.b]


def test_a_local_term_answer_is_cached_until_the_head_changes(term, local_repo):
    assert term.get(text="t") == term.get(text="t")
    assert len(term.calls) == 1
    (local_repo.path / "new.txt").write_text("n\n")
    local_repo.commit("More")
    term.get(text="t")
    assert len(term.calls) == 2


@pytest.mark.parametrize(
    "params, status",
    [
        ({}, 400),
        ({"text": "x" * 301}, 400),
        ({"text": "t", "around": "x" * 4001}, 400),
        ({"text": "t", "chunk": "nope"}, 404),
    ],
)
def test_a_bad_local_term_request_is_refused_without_a_claude_call(term, params, status):
    assert term.get(**params)[0] == status
    assert not term.calls


def test_a_local_term_request_with_a_foreign_host_is_refused(serve, term):
    assert send(serve, "GET", "/api/local/explain-term?text=t", headers={"Host": "attacker.example"})[0] == 403
    assert not term.calls


@pytest.fixture
def start(monkeypatch):
    """Runs `main()` with the given arguments against a server that does not listen; returns the opened URLs."""
    opened = []

    class FakeServer:
        def __init__(self, *args):
            pass

        def serve_forever(self):
            raise KeyboardInterrupt

    monkeypatch.setattr(server, "ThreadingHTTPServer", FakeServer)
    monkeypatch.setattr(server.webbrowser, "open", opened.append)
    monkeypatch.setattr(server, "LOCAL_REPO", None)

    def run(*args):
        monkeypatch.setattr(sys, "argv", ["diffsplainer", *args])
        server.main()
        return opened

    return run


def test_local_flag_fixes_the_repository_and_opens_the_page(start, repo, capsys):
    assert start("--local", str(repo.path)) == [f"http://localhost:{server.PORT}/"]
    assert server.LOCAL_REPO == repo.path
    assert str(repo.path) in capsys.readouterr().out


def test_local_flag_without_a_path_reviews_the_current_directory(start, repo, monkeypatch):
    monkeypatch.chdir(repo.path)
    start("--local")
    assert server.LOCAL_REPO == repo.path


def test_local_flag_with_a_path_that_is_not_a_repository_exits_with_the_reason(start, tmp_path):
    with pytest.raises(SystemExit, match="not a git repository"):
        start("--local", str(tmp_path))


def test_local_flag_on_a_branch_without_a_base_exits_with_the_reason(start, repo):
    repo.git("switch", "-q", "-c", "feature")
    repo.git("remote", "remove", "origin")
    repo.git("branch", "-m", "main", "trunk")
    with pytest.raises(SystemExit, match="default branch"):
        start("--local", str(repo.path))


def test_a_pr_url_leaves_local_mode_off(start):
    assert start("https://github.com/o/r/pull/1") == [f"http://localhost:{server.PORT}/o/r/pull/1"]
    assert server.LOCAL_REPO is None


def test_a_git_failure_while_checking_the_range_is_a_500_and_writes_nothing(serve, local_repo, tmp_path, monkeypatch):
    (tmp_path / "gone").mkdir()
    monkeypatch.setattr(server, "LOCAL_REPO", tmp_path / "gone")
    status, data = post(serve, {"action": "start", "head": local_repo.a})
    assert (status, "git repository" in data["error"]) == (500, True)
    assert review_file(local_repo) is None
