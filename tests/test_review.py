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


def test_approve_adds_the_sha_once(repo):
    review.apply(repo.path, {"action": "approve", "sha": repo.a}, {repo.a})
    review.apply(repo.path, {"action": "approve", "sha": repo.a}, {repo.a})
    assert read(repo)["approved"] == [repo.a]


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
        {"action": "approve", "sha": "f" * 40},  # not in the range
        {"action": "approve", "sha": ["x"]},
        {"action": "approve"},
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
    review.apply(repo.path, {"action": "approve", "sha": repo.b}, {repo.b})
    got = read(repo)
    assert got["entries"][0]["reply"] == "Fixed in a fixup."
    assert got["approved"] == [repo.b]


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
        review.apply(repo.path, {"action": "approve", "sha": repo.a}, {repo.a})
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
