import json
import os
import re

import pytest

from diffsplainer import review

SHA = "a" * 40


def read(repo):
    return json.loads(review.path(repo.path).read_text())


def entry(repo, **over):
    req = {"action": "change", "sha": repo.a, "file": "a.py", "lines": "11-12", "diff": "+added1", "body": "Why?"}
    return review.apply(repo.path, {**req, **over}, {repo.a, repo.b})


def test_the_review_file_is_in_the_git_dir(repo):
    assert review.path(repo.path) == repo.path / ".git" / "diffsplainer" / "review.json"


def test_the_review_file_is_in_the_git_dir_of_a_linked_worktree(repo, tmp_path):
    repo.git("worktree", "add", "-q", str(tmp_path / "wt"), "-b", "wt")
    assert (tmp_path / "wt" / ".git").is_file()
    got = review.path(tmp_path / "wt")
    assert got == repo.path / ".git" / "worktrees" / "wt" / "diffsplainer" / "review.json"
    review.start_review(tmp_path / "wt", repo.b)
    assert got.is_file()


def test_path_outside_a_repository_is_a_value_error(tmp_path):
    with pytest.raises(ValueError):
        review.path(tmp_path / "missing")
    with pytest.raises(ValueError):
        review.path(tmp_path)


def test_a_missing_file_reads_as_an_empty_review(repo):
    assert review.read(repo.path) == {"reviews": [], "approved": [], "entries": []}


def test_start_review_records_the_head_and_the_time(repo):
    review.start_review(repo.path, repo.b)
    review.start_review(repo.path, repo.a)
    got = read(repo)["reviews"]
    assert [r["head"] for r in got] == [repo.b, repo.a]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d.*", got[0]["at"])


def test_start_with_approve_records_the_round_and_approves_every_commit_once(repo):
    shas = {repo.a, repo.b}
    review.apply(repo.path, {"action": "start", "head": repo.b}, shas)
    assert read(repo)["approved"] == []  # a plain start approves nothing
    review.apply(repo.path, {"action": "start", "head": repo.b, "approve": True}, shas)
    review.apply(repo.path, {"action": "start", "head": repo.b, "approve": True}, shas)
    got = read(repo)
    assert sorted(got["approved"]) == sorted(shas)
    assert [r["head"] for r in got["reviews"]] == [repo.b] * 3


def test_start_with_approve_keeps_what_was_approved_before(repo):
    review.update(repo.path, lambda d: d["approved"].append(repo.base))
    review.apply(repo.path, {"action": "start", "head": repo.b, "approve": True}, {repo.a, repo.b})
    assert read(repo)["approved"][0] == repo.base


def test_start_with_approve_is_refused_while_an_entry_is_open_and_writes_nothing(repo):
    entry(repo)
    before = review.path(repo.path).read_text()
    with pytest.raises(ValueError, match="open"):
        review.apply(repo.path, {"action": "start", "head": repo.b, "approve": True}, {repo.a, repo.b})
    assert review.path(repo.path).read_text() == before


def test_start_with_approve_is_allowed_when_every_entry_is_resolved(repo):
    entry(repo)
    review.apply(repo.path, {"action": "status", "id": "r1", "status": "resolved"}, set())
    review.apply(repo.path, {"action": "start", "head": repo.b, "approve": True}, {repo.a, repo.b})
    assert sorted(read(repo)["approved"]) == sorted([repo.a, repo.b])


def test_start_without_approve_is_allowed_while_an_entry_is_open(repo):
    entry(repo)
    review.apply(repo.path, {"action": "start", "head": repo.b}, {repo.a, repo.b})
    assert len(read(repo)["reviews"]) == 1


def test_request_changes_adds_an_open_entry_with_the_chunk_and_the_comment(repo):
    entry(repo)
    got = read(repo)["entries"]
    assert len(got) == 1
    assert {k: got[0][k] for k in got[0] if k != "created"} == {
        "id": "r1",
        "kind": "change",
        "status": "open",
        "sha": repo.a,
        "file": "a.py",
        "lines": "11-12",
        "diff": "+added1",
        "quote": None,
        "body": "Why?",
        "reply": None,
    }
    assert got[0]["created"]


def test_a_comment_adds_an_open_question_with_the_selected_text(repo):
    entry(repo, action="question", quote="added1", body="What is this?")
    got = read(repo)["entries"][0]
    assert (got["kind"], got["status"], got["quote"], got["body"]) == ("question", "open", "added1", "What is this?")


def test_a_general_comment_is_an_open_change_entry_with_no_file_lines_or_diff(repo):
    entry(repo, sha=repo.b, file="", lines="", diff="", body="Split this into two PRs.")
    got = read(repo)["entries"][0]
    assert (got["kind"], got["status"], got["sha"], got["file"], got["lines"], got["diff"], got["body"]) == (
        "change",
        "open",
        repo.b,
        "",
        "",
        "",
        "Split this into two PRs.",
    )


def test_a_general_comment_still_needs_a_commit_of_the_range_and_a_body(repo):
    for over in ({"sha": "f" * 40}, {"sha": None}, {"body": " "}):
        with pytest.raises(ValueError):
            entry(repo, file="", lines="", diff="", **over)
    assert not review.path(repo.path).exists()


def test_entry_ids_count_up_and_skip_ids_that_exist(repo):
    entry(repo)
    entry(repo)
    review.update(repo.path, lambda d: d["entries"].append({"id": "r7"}))
    entry(repo)
    assert [e["id"] for e in read(repo)["entries"]] == ["r1", "r2", "r7", "r8"]


def test_resolving_an_entry_sets_its_status_and_leaves_the_rest(repo):
    entry(repo)
    review.apply(repo.path, {"action": "status", "id": "r1", "status": "resolved"}, set())
    got = read(repo)["entries"][0]
    assert (got["status"], got["body"]) == ("resolved", "Why?")
    review.apply(repo.path, {"action": "status", "id": "r1", "status": "open"}, set())
    assert read(repo)["entries"][0]["status"] == "open"


@pytest.mark.parametrize(
    "req",
    [
        [],
        {"action": "push"},
        {"action": "approve", "sha": "a" * 40},  # the page no longer approves one commit
        {"action": "start", "head": "f" * 40},  # not in the range
        {"action": "start", "head": ["x"]},
        {"action": "start"},
        {"action": "start", "head": "a" * 40, "approve": "yes"},  # approve is a boolean
        {"action": "start", "sha": "a" * 40},  # start names `head`
        {"action": "change", "sha": "a" * 40, "file": "f", "lines": "1", "diff": "d", "body": ""},
        {"action": "change", "sha": "a" * 40, "file": "f", "lines": 1, "diff": "d", "body": "x"},
        {"action": "change", "sha": "a" * 40, "file": "f", "lines": "1", "diff": "d", "body": "  "},
        {"action": "question", "sha": "a" * 40, "file": "f", "lines": "1", "diff": "d", "body": "x"},  # no quote
        {"action": "question", "sha": "a" * 40, "file": "f", "lines": "1", "diff": "d", "body": "x", "quote": 3},
        {"action": "status", "id": "r1", "status": "done"},
        {"action": "status", "id": "r9", "status": "resolved"},
    ],
)
def test_a_bad_request_is_a_value_error_and_writes_nothing(repo, req):
    review.start_review(repo.path, repo.a)
    before = review.path(repo.path).read_text()
    with pytest.raises(ValueError):
        review.apply(repo.path, req, {"a" * 40})
    assert review.path(repo.path).read_text() == before


def test_start_names_the_head_and_the_head_must_be_in_the_range(repo):
    review.apply(repo.path, {"action": "start", "head": repo.b}, {repo.a, repo.b})
    assert [r["head"] for r in read(repo)["reviews"]] == [repo.b]
    with pytest.raises(ValueError):
        review.apply(repo.path, {"action": "start", "head": "--output=x"}, {repo.a, repo.b})


def test_a_write_keeps_a_reply_the_agent_wrote_after_the_page_loaded(repo):
    entry(repo)
    review.read(repo.path)  # the page loads the review here
    data = read(repo)
    data["entries"][0]["reply"] = "Fixed in a fixup."
    review.path(repo.path).write_text(json.dumps(data))  # the agent answers
    review.apply(repo.path, {"action": "start", "head": repo.b}, {repo.b})
    got = read(repo)
    assert got["entries"][0]["reply"] == "Fixed in a fixup."
    assert [r["head"] for r in got["reviews"]] == [repo.b]


def test_a_write_keeps_fields_it_does_not_know(repo):
    entry(repo)
    data = read(repo)
    data["extra"] = {"keep": True}
    data["entries"][0]["note"] = "agent note"
    review.path(repo.path).write_text(json.dumps(data))
    review.apply(repo.path, {"action": "status", "id": "r1", "status": "resolved"}, set())
    got = read(repo)
    assert got["extra"] == {"keep": True}
    assert got["entries"][0]["note"] == "agent note"


@pytest.mark.parametrize("text", ["{not json", "[]", '{"entries": {}}', '{"reviews": "x"}'])
def test_a_file_that_is_not_a_review_is_an_error_and_is_left_alone(repo, text):
    review.path(repo.path).parent.mkdir()
    review.path(repo.path).write_text(text)
    with pytest.raises(review.ReviewError):
        review.apply(repo.path, {"action": "start", "head": repo.a, "approve": True}, {repo.a})
    with pytest.raises(review.ReviewError):
        review.start_review(repo.path, repo.a)
    assert review.path(repo.path).read_text() == text


def test_a_write_goes_through_a_temporary_file_that_replaces_the_review(repo, monkeypatch):
    review.start_review(repo.path, repo.a)
    seen = []
    real = os.replace
    monkeypatch.setattr(review.os, "replace", lambda src, dst: seen.append((src, dst, read(repo))) or real(src, dst))
    review.start_review(repo.path, repo.b)
    ((src, dst, during),) = seen
    assert dst == review.path(repo.path) and src != dst and src.parent == dst.parent
    assert len(during["reviews"]) == 1  # the file still held the old content at the swap
    assert len(read(repo)["reviews"]) == 2
    assert os.listdir(dst.parent) == ["review.json"]


def test_a_failed_write_leaves_no_temporary_file(repo, monkeypatch):
    def fail(*_):
        raise OSError("disk full")

    monkeypatch.setattr(review.os, "replace", fail)
    with pytest.raises(OSError):
        review.start_review(repo.path, repo.a)
    assert list(review.path(repo.path).parent.iterdir()) == []


def agent_writes_once_during(repo, change, rewrite):
    """A `change` that runs `rewrite(data)` on the file once, as the agent would after Diffsplainer's read."""
    calls = []

    def wrapped(d):
        calls.append(None)
        if len(calls) == 1:
            p = review.path(repo.path)
            data = json.loads(p.read_text()) if p.exists() else {}
            rewrite(data)
            p.parent.mkdir(exist_ok=True)
            p.write_text(json.dumps(data))
        change(d)

    return wrapped, calls


def test_a_reply_written_between_the_read_and_the_replace_is_kept_and_the_change_is_applied(repo):
    entry(repo)

    def reply(data):
        data["entries"][0]["reply"] = "Fixed in a fixup."

    change, calls = agent_writes_once_during(repo, lambda d: d["approved"].append(repo.b), reply)
    review.update(repo.path, change)
    got = read(repo)
    assert got["entries"][0]["reply"] == "Fixed in a fixup."
    assert got["approved"] == [repo.b]
    assert len(calls) == 2


def test_a_file_the_agent_creates_between_the_read_and_the_replace_is_kept(repo):
    change, _ = agent_writes_once_during(
        repo, lambda d: d["approved"].append(repo.b), lambda data: data.update(reviews=[{"head": repo.a}])
    )
    review.update(repo.path, change)
    got = read(repo)
    assert got["reviews"] == [{"head": repo.a}]
    assert got["approved"] == [repo.b]


def test_a_file_that_keeps_changing_is_a_review_error_and_keeps_the_agents_write(repo):
    calls = []

    def change(d):
        calls.append(None)
        review.path(repo.path).parent.mkdir(exist_ok=True)
        review.path(repo.path).write_text(json.dumps({"agent": len(calls)}))

    with pytest.raises(review.ReviewError, match="keeps changing"):
        review.update(repo.path, change)
    assert read(repo)["agent"] == len(calls)
    assert os.listdir(review.path(repo.path).parent) == ["review.json"]


def test_summary_returns_the_string_fields_the_agent_wrote():
    given = {"title": "Cache feeds", "ticket": "BH-1", "body": "Why.\nHow.", "plan": "/shared/plan.md"}
    assert review.summary({"summary": given}) == given
    assert review.summary({"summary": {"title": "T"}}) == {"title": "T", "ticket": None, "body": None, "plan": None}


def test_summary_with_only_a_plan_path_is_none_because_it_has_nothing_to_show_or_check():
    assert review.summary({"summary": {"plan": "/shared/plan.md"}}) is None


def test_summary_ignores_a_field_that_is_not_a_string_and_blank_text():
    got = review.summary({"summary": {"title": "T", "ticket": 12, "body": "  "}})
    assert got == {"title": "T", "ticket": None, "body": None, "plan": None}


def test_summary_ignores_a_plan_that_is_not_a_string():
    assert review.summary({"summary": {"title": "T", "plan": ["/p.md"]}})["plan"] is None


def test_summary_clips_long_fields_because_they_go_into_every_prompt():
    got = review.summary(
        {"summary": {"title": "t" * 1000, "ticket": "k" * 1000, "body": "b" * 10000, "plan": "p" * 5000}}
    )
    assert len(got["title"]) <= 200 and len(got["ticket"]) <= 200 and len(got["body"]) <= 3000
    assert len(got["plan"]) <= 1000


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"summary": None},
        {"summary": "text"},
        {"summary": ["a"]},
        {"summary": {}},
        {"summary": {"title": 5, "body": []}},
    ],
)
def test_a_missing_or_malformed_summary_is_none(data):
    assert review.summary(data) is None


def test_a_write_keeps_the_agents_summary(repo):
    summary = {"title": "Cache feeds", "ticket": None, "body": "Why."}
    review.path(repo.path).parent.mkdir()
    review.path(repo.path).write_text(json.dumps({"summary": summary}))
    entry(repo)
    review.apply(repo.path, {"action": "start", "head": repo.b}, {repo.b})
    assert read(repo)["summary"] == summary
