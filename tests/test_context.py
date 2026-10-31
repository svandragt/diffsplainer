from diffsplainer import context
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
