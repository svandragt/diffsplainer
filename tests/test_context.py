from pathlib import Path

import pytest

from diffsplainer import context, local
from diffsplainer import github as gh


def _repo(monkeypatch, files: dict[str, str]) -> list[str]:
    """Serve `files` as the repo; returns the list of paths that get fetched."""
    fetched: list[str] = []

    def get_file(owner, repo, path, ref):
        fetched.append(path)
        return files.get(path)

    tree = {"tree": [{"path": p, "type": "blob"} for p in files]}
    monkeypatch.setattr(gh, "get_tree", lambda *_: tree)
    monkeypatch.setattr(gh, "get_file", get_file)
    return fetched


def _paths(docs: list[dict]) -> list[str]:
    return [d["path"] for d in docs]


def test_readme_is_skipped_when_a_nested_instruction_file_exists(monkeypatch):
    fetched = _repo(monkeypatch, {"pkg/AGENTS.md": "nested rules", "README.md": "readme"})
    docs = context.project_instructions("o", "r", "main", ["pkg/mod.py"])
    assert _paths(docs) == ["pkg/AGENTS.md"]
    assert "README.md" not in fetched


def test_readme_is_skipped_when_a_root_instruction_file_exists(monkeypatch):
    _repo(monkeypatch, {"CLAUDE.md": "root rules", "README.md": "readme"})
    docs = context.project_instructions("o", "r", "main", ["pkg/mod.py"])
    assert _paths(docs) == ["CLAUDE.md"]


def test_readme_is_the_fallback_when_there_is_no_instruction_file(monkeypatch):
    _repo(monkeypatch, {"README.md": "readme"})
    docs = context.project_instructions("o", "r", "main", ["pkg/mod.py"])
    assert _paths(docs) == ["README.md"]


def test_related_pulls_caps_commits_round_robin_across_files(monkeypatch):
    commits = {
        "a.py": [{"sha": "a1"}, {"sha": "a2"}],
        "b.py": [{"sha": "a1"}, {"sha": "b2"}],  # shares its first commit with a.py
        "c.py": [{"sha": "c1"}, {"sha": "c2"}],
    }
    looked_up: list[str] = []

    def pulls_for_commit(owner, repo, sha):
        looked_up.append(sha)
        return []

    monkeypatch.setattr(context, "MAX_SHAS", 3)
    monkeypatch.setattr(gh, "commits_for_path", lambda o, r, path, ref: commits[path])
    monkeypatch.setattr(gh, "pulls_for_commit", pulls_for_commit)
    context.related_pulls("o", "r", {"number": 1, "base": {"sha": "base"}}, list(commits), limit=3)
    assert sorted(looked_up) == ["a1", "a2", "c1"]


def _disk(tmp_path, files: dict[str, str]):
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)


def test_local_instructions_read_root_and_nested_files_from_disk_without_the_readme(tmp_path):
    _disk(tmp_path, {"CLAUDE.md": "root", "pkg/AGENTS.md": "nested", "other/AGENTS.md": "unrelated", "README.md": "r"})
    docs = context.local_instructions(tmp_path, ["pkg/mod.py", "top.py"])
    assert docs == [{"path": "CLAUDE.md", "body": "root"}, {"path": "pkg/AGENTS.md", "body": "nested"}]


def test_local_instructions_fall_back_to_the_readme(tmp_path):
    _disk(tmp_path, {"README.md": "readme"})
    assert _paths(context.local_instructions(tmp_path, ["a.py"])) == ["README.md"]


def test_local_instructions_are_empty_without_any_file(tmp_path):
    assert context.local_instructions(tmp_path, ["a.py"]) == []


def test_local_instructions_clip_long_files(tmp_path):
    _disk(tmp_path, {"AGENTS.md": "x" * (context.MAX_DOC_CHARS + 10)})
    (doc,) = context.local_instructions(tmp_path, ["a.py"])
    assert doc["body"].endswith("…[truncated]") and len(doc["body"]) < context.MAX_DOC_CHARS + 20


def test_local_instructions_follow_a_symlink_inside_the_repo_but_not_one_that_leaves_it(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("SECRET")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("rules")
    (repo / "CLAUDE.md").symlink_to("AGENTS.md")
    (repo / "CONTRIBUTING.md").symlink_to(outside / "secret.txt")
    (repo / "pkg").symlink_to(outside)
    (outside / "AGENTS.md").write_text("SECRET")
    docs = context.local_instructions(repo, ["pkg/mod.py"])
    assert [(d["path"], d["body"]) for d in docs] == [("CLAUDE.md", "rules"), ("AGENTS.md", "rules")]


def test_local_instructions_skip_a_file_that_is_not_text_and_a_directory(tmp_path):
    (tmp_path / "AGENTS.md").write_bytes(b"\xff\xfe\x00")
    (tmp_path / "CONTRIBUTING.md").mkdir()
    assert context.local_instructions(tmp_path, ["a.py"]) == []


PULL = {
    "number": 7,
    "title": "Earlier",
    "html_url": "https://github.com/o/r/pull/7",
    "merged_at": "2024-01-02",
    "body": "Why.",
}


def _github(monkeypatch, commits_for_path=None):
    """Fake GitHub; returns the list of calls it receives."""
    calls: list[tuple] = []

    def default(owner, repo, path, ref):
        return [{"sha": "c1"}]

    def commits(*args):
        calls.append(args)
        return (commits_for_path or default)(*args)

    monkeypatch.setattr(gh, "commits_for_path", commits)
    monkeypatch.setattr(gh, "pulls_for_commit", lambda *_: [PULL])
    return calls


def test_local_related_pulls_asks_the_origin_repository_from_the_given_base(repo, monkeypatch):
    repo.git("remote", "set-url", "origin", "git@github.com:o/r.git")
    calls = _github(monkeypatch)
    related = context.local_related_pulls(repo.path, "base-sha", ["a.py"], 3)
    assert calls == [("o", "r", "a.py", "base-sha")]
    assert [(p["number"], p["files"]) for p in related] == [(7, ["a.py"])]


def test_local_related_pulls_is_empty_and_calls_nothing_without_a_github_origin(repo, monkeypatch):
    calls = _github(monkeypatch)
    assert context.local_related_pulls(repo.path, "b", ["a.py"], 3) == []  # the fixture's origin is a local path
    repo.git("remote", "remove", "origin")
    assert context.local_related_pulls(repo.path, "b", ["a.py"], 3) == []
    assert calls == []


def test_local_related_pulls_is_empty_when_github_fails(repo, monkeypatch):
    repo.git("remote", "set-url", "origin", "git@github.com:o/r.git")

    def fail(*_):
        raise gh.GitHubError(404, "Not Found")

    _github(monkeypatch, fail)
    assert context.local_related_pulls(repo.path, "b", ["a.py"], 3) == []


def test_local_related_pulls_is_empty_when_the_origin_cannot_be_read(repo, monkeypatch):
    calls = _github(monkeypatch)

    def fail(*_):
        raise ValueError("Cannot run git")

    monkeypatch.setattr(local, "run", fail)
    assert context.local_related_pulls(repo.path, "b", ["a.py"], 3) == []
    assert calls == []


def _plans(repo, files: dict[str, str]):
    for name, text in files.items():
        (repo.path / ".git" / "approach" / name).parent.mkdir(parents=True, exist_ok=True)
        (repo.path / ".git" / "approach" / name).write_text(text)


def test_local_plan_is_the_file_named_after_the_branch(repo):
    _plans(repo, {"feature-x.md": "The plan.", "other.md": "No."})
    assert context.local_plan(repo.path, "feature-x") == {"name": "feature-x.md", "body": "The plan."}


def test_local_plan_follows_a_branch_name_with_a_slash_into_a_directory(repo):
    _plans(repo, {"feature/cache.md": "Nested."})
    assert context.local_plan(repo.path, "feature/cache")["name"] == "feature/cache.md"


@pytest.mark.parametrize(
    "branch, key",
    [
        ("BH-740-add-cache", "BH-740"),
        ("BH-740", "BH-740"),
        ("1234-fix-it", "1234"),
        ("1234", "1234"),
        ("AB2-7-x", "AB2-7"),
    ],
)
def test_local_plan_falls_back_to_the_ticket_key_of_the_branch(repo, branch, key):
    _plans(repo, {f"{key}.md": "By key."})
    assert context.local_plan(repo.path, branch) == {"name": f"{key}.md", "body": "By key."}


def test_local_plan_prefers_the_branch_file_to_the_ticket_file(repo):
    _plans(repo, {"BH-740.md": "Key.", "BH-740-add-cache.md": "Branch."})
    assert context.local_plan(repo.path, "BH-740-add-cache")["body"] == "Branch."


@pytest.mark.parametrize("branch", ["12abc", "bh-740-x", "BH-740x", "x-BH-740", "feature-x"])
def test_local_plan_does_not_take_a_ticket_key_from_a_branch_that_does_not_start_with_one(repo, branch):
    _plans(repo, {"12.md": "n", "bh-740.md": "n", "BH-740.md": "n", "feature.md": "n"})
    assert context.local_plan(repo.path, branch) is None


def test_local_plan_is_none_without_an_approach_directory_and_for_a_detached_head(repo):
    assert context.local_plan(repo.path, "main") is None
    _plans(repo, {"HEAD.md": "n"})
    assert context.local_plan(repo.path, "HEAD") is None  # `git branch --show-current` is empty, so load says HEAD


@pytest.mark.parametrize("branch, name", [("feature-pr", "feature-pr.md"), ("feature.ticket", "feature.ticket.md")])
def test_local_plan_skips_the_ticket_and_pr_description_files(repo, branch, name):
    _plans(repo, {name: "Not a plan."})
    assert context.local_plan(repo.path, branch) is None


def test_local_plan_refuses_a_file_outside_the_approach_directory(repo, tmp_path):
    _plans(repo, {"x.md": "n"})
    (repo.path / ".git" / "secret.md").write_text("SECRET")
    (tmp_path / "outside.md").write_text("SECRET")
    (repo.path / ".git" / "approach" / "linked.md").symlink_to(tmp_path / "outside.md")
    assert context.local_plan(repo.path, "../secret") is None
    assert context.local_plan(repo.path, "linked") is None


def test_local_plan_clips_a_long_plan_and_skips_a_file_that_is_not_text(repo):
    _plans(repo, {"long.md": "x" * (context.MAX_PLAN_CHARS + 10)})
    (repo.path / ".git" / "approach" / "bin.md").write_bytes(b"\xff\xfe\x00")
    assert context.local_plan(repo.path, "long")["body"].endswith("…[truncated]")
    assert context.local_plan(repo.path, "bin") is None


def test_local_plan_is_found_from_a_linked_worktree(repo, tmp_path):
    repo.git("worktree", "add", "-q", "-b", "wt", str(tmp_path / "wt"))
    approach = Path(repo.git("rev-parse", "--absolute-git-dir", cwd=tmp_path / "wt")) / "approach"
    approach.mkdir()
    (approach / "wt.md").write_text("In the worktree's git dir.")
    assert context.local_plan(tmp_path / "wt", "wt")["body"] == "In the worktree's git dir."


def _shared_plan(tmp_path, text="Shared plan.", name="plan.md"):
    (tmp_path / "shared").mkdir(exist_ok=True)
    (tmp_path / "shared" / name).write_text(text)
    return tmp_path / "shared" / name


def test_local_plan_is_the_file_the_summary_names_before_the_branch_file(repo, tmp_path):
    _plans(repo, {"main.md": "Branch plan."})
    file = _shared_plan(tmp_path)
    assert context.local_plan(repo.path, "main", {"plan": str(file)}) == {"name": "plan.md", "body": "Shared plan."}


def test_local_plan_expands_a_tilde_in_the_summarys_plan(repo, tmp_path, monkeypatch):
    _shared_plan(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert context.local_plan(repo.path, "main", {"plan": "~/shared/plan.md"})["body"] == "Shared plan."


def test_local_plan_reads_a_relative_plan_path_from_the_work_tree_not_the_current_directory(
    repo, tmp_path, monkeypatch
):
    (repo.path / "docs").mkdir()
    (repo.path / "docs" / "plan.md").write_text("In the repo.")
    monkeypatch.chdir(tmp_path)
    assert context.local_plan(repo.path, "main", {"plan": "docs/plan.md"})["body"] == "In the repo."


def test_local_plan_names_the_summarys_plan_when_the_branch_is_detached(repo, tmp_path):
    file = _shared_plan(tmp_path)
    assert context.local_plan(repo.path, "HEAD", {"plan": str(file)})["name"] == "plan.md"


def test_local_plan_clips_the_summarys_plan(repo, tmp_path):
    file = _shared_plan(tmp_path, "x" * (context.MAX_PLAN_CHARS + 10))
    assert context.local_plan(repo.path, "main", {"plan": str(file)})["body"].endswith("…[truncated]")


def test_local_plan_refuses_a_summary_plan_that_is_not_a_markdown_file_and_uses_the_branch_file(repo, tmp_path):
    _plans(repo, {"main.md": "Branch plan."})
    secret = _shared_plan(tmp_path, "SECRET", "notes.txt")
    (tmp_path / "shared" / "link.md").symlink_to(secret)  # a .md name that points at a file that is not
    for plan in (secret, tmp_path / "shared" / "link.md"):
        assert context.local_plan(repo.path, "main", {"plan": str(plan)}) == {"name": "main.md", "body": "Branch plan."}


@pytest.fixture
def unreadable(tmp_path):
    """Values of `summary.plan` that name no readable plan."""
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "bin.md").write_bytes(b"\xff\xfe\x00")
    (shared / "empty.md").write_text("  \n")
    (shared / "dir.md").mkdir()
    return {
        "missing": str(shared / "missing.md"),
        "binary": str(shared / "bin.md"),
        "empty": str(shared / "empty.md"),
        "directory": str(shared / "dir.md"),
        "null byte": "a\0b.md",
        "unknown user": "~nosuchuser/plan.md",
        "blank": "",
    }


@pytest.mark.parametrize("case", ["missing", "binary", "empty", "directory", "null byte", "unknown user", "blank"])
def test_local_plan_falls_back_to_the_branch_file_when_the_summarys_plan_cannot_be_read(repo, unreadable, case):
    _plans(repo, {"main.md": "Branch plan."})
    assert context.local_plan(repo.path, "main", {"plan": unreadable[case]}) == {
        "name": "main.md",
        "body": "Branch plan.",
    }


@pytest.mark.parametrize("plan", [None, 12, ["/p.md"], {"a": 1}, True])
def test_local_plan_ignores_a_summary_plan_that_is_not_a_string(repo, plan):
    _plans(repo, {"main.md": "Branch plan."})
    assert context.local_plan(repo.path, "main", {"plan": plan})["name"] == "main.md"
    assert context.local_plan(repo.path, "main", {"title": "T"})["name"] == "main.md"


def test_local_plan_keeps_refusing_an_approach_symlink_that_leaves_the_directory_after_a_bad_summary_plan(
    repo, tmp_path
):
    _plans(repo, {"x.md": "n"})
    (tmp_path / "outside.md").write_text("SECRET")
    (repo.path / ".git" / "approach" / "main.md").symlink_to(tmp_path / "outside.md")
    assert context.local_plan(repo.path, "main", {"plan": str(tmp_path / "missing.md")}) is None
