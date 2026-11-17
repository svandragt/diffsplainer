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
