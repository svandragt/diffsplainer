import json

import pytest

from diffsplainer import github as gh
from diffsplainer import local, review


def chunk_shas(data: dict) -> dict:
    return {f["filename"]: {c["sha"] for c in f["chunks"]} for f in data["files"]}


def test_load_lists_the_commits_after_the_upstream_and_tags_each_chunk_with_its_commit(repo):
    data = local.load(repo.path)
    info = data["local"]
    assert (info["branch"], info["base"], info["head"]) == ("main", repo.base, repo.b)
    assert info["commits"] == [
        {"sha": repo.a, "subject": "Add greeting", "body": "Body of a."},
        {"sha": repo.b, "subject": "Trim gone.txt and edit b.py", "body": ""},
    ]
    assert [f["filename"] for f in data["files"]] == ["a.py", "b.py", "drop.txt", "gone.txt"]
    shas = chunk_shas(data)
    assert shas["a.py"] == {repo.a}  # `b` did not touch a.py
    assert shas["b.py"] == {repo.b}  # `a` added the lines and `b` edited the last one: the newest wins
    assert shas["drop.txt"] == {repo.a}  # deletion-only chunk: the newest commit in the range that touched the file
    assert shas["gone.txt"] == {repo.b}


def test_load_gives_files_the_shape_chunk_files_makes(repo):
    files = {f["filename"]: f for f in local.load(repo.path)["files"]}
    assert (files["a.py"]["status"], files["a.py"]["additions"], files["a.py"]["deletions"]) == ("modified", 2, 0)
    assert files["b.py"]["status"] == "added" and files["drop.txt"]["status"] == "removed"
    chunk = files["a.py"]["chunks"][0]
    assert [line["s"] for line in chunk["lines"] if line["t"] == "add"] == ["added1", "added2"]
    assert not any(line["s"].startswith(("diff --git", "+++", "---")) for line in chunk["lines"])


def test_load_orders_files_like_the_files_changed_tab(repo):
    (repo.path / "sub").mkdir()
    (repo.path / "sub" / "z.py").write_text("z\n")
    (repo.path / "Zeta.py").write_text("z\n")
    repo.commit("More files")
    names = [f["filename"] for f in local.load(repo.path)["files"]]
    assert names == ["sub/z.py", "a.py", "b.py", "drop.txt", "gone.txt", "Zeta.py"]


def test_load_keeps_a_renamed_file_with_its_previous_name_and_a_binary_file_without_chunks(repo):
    repo.git("mv", "gone.txt", "renamed.txt")
    (repo.path / "img.bin").write_bytes(b"\x00\x01\x02")
    repo.commit("Rename and add binary")
    files = {f["filename"]: f for f in local.load(repo.path)["files"]}
    assert (files["renamed.txt"]["status"], files["renamed.txt"]["previous"]) == ("renamed", "gone.txt")
    assert files["img.bin"]["chunks"] == []


def test_load_keeps_a_file_that_became_a_symlink_as_one_file_and_the_files_after_it_in_line(repo):
    # git diff prints a deletion and a creation for a type change, but --name-status lists it once
    (repo.path / "a.py").unlink()
    (repo.path / "a.py").symlink_to("b.py")
    repo.commit("a.py becomes a symlink")
    files = {f["filename"]: f for f in local.load(repo.path)["files"]}
    assert sorted(files) == ["a.py", "b.py", "drop.txt", "gone.txt"]
    assert files["a.py"]["chunks"] and files["b.py"]["status"] == "added"


def test_load_ignores_uncommitted_changes(repo):
    repo.dirty()
    names = {f["filename"] for f in local.load(repo.path)["files"]}
    assert "staged.txt" not in names
    assert all(
        "unstaged edit" not in line["s"]
        for f in local.load(repo.path)["files"]
        for c in f["chunks"]
        for line in c["lines"]
    )


def test_load_without_commits_after_the_upstream_returns_nothing(repo):
    repo.git("push", "-q")
    data = local.load(repo.path)
    assert data["local"]["commits"] == [] and data["files"] == []
    assert data["local"]["head"] == repo.b


@pytest.fixture
def feature(repo):
    """The two commits on a branch with no upstream; `main` is back at the base."""
    repo.git("switch", "-q", "-c", "feature")
    repo.git("branch", "-f", "main", repo.base)
    return repo


def test_without_an_upstream_the_range_starts_at_the_merge_base_with_origin_head(feature):
    feature.git("remote", "set-head", "origin", "main")
    feature.git("branch", "-f", "main", feature.b)  # only origin/HEAD, not the local main, can give the base
    info = local.load(feature.path)["local"]
    assert info["base"] == feature.base
    assert [c["sha"] for c in info["commits"]] == [feature.a, feature.b]


@pytest.mark.parametrize("name", ["main", "master"])
def test_without_origin_the_range_starts_at_the_merge_base_with_main_or_master(feature, name):
    feature.git("remote", "remove", "origin")
    feature.git("branch", "-m", "main", name)
    info = local.load(feature.path)["local"]
    assert info["base"] == feature.base
    assert [c["sha"] for c in info["commits"]] == [feature.a, feature.b]


def test_without_an_upstream_or_default_branch_load_fails_with_a_message(feature):
    feature.git("remote", "remove", "origin")
    feature.git("branch", "-m", "main", "trunk")
    with pytest.raises(ValueError, match="default branch"):
        local.load(feature.path)


def test_a_diverged_upstream_does_not_put_its_commits_in_the_range(repo, tmp_path):
    other = tmp_path / "other"
    repo.git("clone", "-q", str(tmp_path / "upstream.git"), str(other), cwd=tmp_path)
    (other / "up.txt").write_text("upstream only\n")
    repo.git("add", "-A", cwd=other)
    repo.git("commit", "-q", "-m", "Upstream work", cwd=other)
    repo.git("push", "-q", cwd=other)
    repo.git("fetch", "-q")
    data = local.load(repo.path)
    assert [c["sha"] for c in data["local"]["commits"]] == [repo.a, repo.b]
    assert "up.txt" not in {f["filename"] for f in data["files"]}


def write_review(repo, **data):
    review.path(repo.path).parent.mkdir(exist_ok=True)
    review.path(repo.path).write_text(json.dumps(data))


def test_second_round_shows_only_the_commits_after_the_reviewed_head(repo):
    review.start_review(repo.path, repo.b)
    (repo.path / "gone.txt").write_text("x\n")
    fixup = repo.commit("fixup! Trim gone.txt and edit b.py")
    data = local.load(repo.path)
    assert data["local"]["base"] == repo.b
    assert [c["sha"] for c in data["local"]["commits"]] == [fixup]
    assert chunk_shas(data) == {"gone.txt": {fixup}}


def test_second_round_uses_the_newest_review(repo):
    review.start_review(repo.path, repo.a)
    review.start_review(repo.path, repo.b)
    assert local.load(repo.path)["local"]["commits"] == []  # the reviewed head is HEAD: nothing new


def test_a_reviewed_head_that_is_not_an_ancestor_is_ignored(repo):
    repo.git("switch", "-q", "-c", "other", repo.base)
    (repo.path / "other.txt").write_text("o\n")
    other = repo.commit("Other work")
    review.start_review(repo.path, other)
    repo.git("switch", "-q", "main")
    assert local.load(repo.path)["local"]["base"] == repo.base


def test_a_reviewed_head_that_is_not_a_sha_is_ignored(repo):
    write_review(repo, reviews=[{"head": "--output=x", "at": "now"}])
    assert local.load(repo.path)["local"]["base"] == repo.base


def test_a_deletion_only_chunk_is_tagged_with_the_commit_that_deleted_the_lines(repo):
    lines = [f"l{i}\n" for i in range(1, 21)]
    (repo.path / "long.txt").write_text("".join(lines))
    repo.commit("Add long.txt")
    repo.git("push", "-q")
    del lines[1]
    (repo.path / "long.txt").write_text("".join(lines))
    deleted = repo.commit("Delete l2")
    lines[-3] = "edited\n"  # far from the deletion, so the diff has two separate chunks
    (repo.path / "long.txt").write_text("".join(lines))
    edited = repo.commit("Edit near the end")
    files = {f["filename"]: f for f in local.load(repo.path)["files"]}
    chunks = files["long.txt"]["chunks"]
    assert [any(line["t"] == "add" for line in c["lines"]) for c in chunks] == [False, True]
    assert [c["sha"] for c in chunks] == [deleted, edited]


def test_load_does_not_change_the_work_tree_the_index_or_any_ref(repo):
    repo.dirty()
    before = repo.state()
    local.load(repo.path)
    assert repo.state() == before


def test_load_outside_a_repository_is_a_value_error(tmp_path):
    with pytest.raises(ValueError):
        local.load(tmp_path)
    with pytest.raises(ValueError):
        local.load(tmp_path / "missing")


def test_load_tags_chunks_in_a_sha256_repository(repo, tmp_path):
    path = tmp_path / "sha256"
    repo.git("init", "-q", "--object-format=sha256", "-b", "main", str(path), cwd=tmp_path)
    (path / "f.txt").write_text("one\n")
    repo.git("add", "-A", cwd=path)
    repo.git("commit", "-q", "-m", "Base", cwd=path)
    repo.git("checkout", "-q", "-b", "feature", cwd=path)  # main stays at Base, the default branch to compare with
    (path / "f.txt").write_text("one\ntwo\n")
    repo.git("commit", "-q", "-am", "Add two", cwd=path)
    head = repo.git("rev-parse", "HEAD", cwd=path)
    assert chunk_shas(local.load(path)) == {"f.txt": {head}}


def test_load_names_the_merge_base_with_the_upstream_even_after_a_review_round(repo):
    assert local.load(repo.path)["local"]["merge_base"] == repo.base
    review.start_review(repo.path, repo.b)
    info = local.load(repo.path)["local"]
    assert (info["base"], info["merge_base"]) == (repo.b, repo.base)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/owner/repo",
        "https://github.com/owner/repo.git",
        "https://github.com/owner/repo/",
        "https://x-access-token:tok@github.com/owner/repo.git",  # betterleaks:allow
        "git@github.com:owner/repo",
        "git@github.com:owner/repo.git",
        "ssh://git@github.com/owner/repo",
        "ssh://git@github.com/owner/repo.git",
    ],
)
def test_origin_repo_reads_owner_and_repo_from_a_github_remote(repo, url):
    repo.git("remote", "set-url", "origin", url)
    assert local.origin_repo(repo.path) == ("owner", "repo")


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.com/owner/repo.git",
        "git@bitbucket.org:owner/repo.git",
        "https://example.com/github.com/owner/repo",
        "../owner/repo",
        "/srv/git/owner/repo.git",
        "https://github.com/owner",
        "https://github.com/owner/repo/pull/1",
        "https://github.com/owner/..",
        "https://github.com/../repo",
    ],
)
def test_origin_repo_is_none_for_a_remote_that_is_not_a_github_repository(repo, url):
    repo.git("remote", "set-url", "origin", url)
    assert local.origin_repo(repo.path) is None


def test_origin_repo_is_none_without_an_origin(repo):
    repo.git("remote", "remove", "origin")
    assert local.origin_repo(repo.path) is None


def test_origin_repo_follows_the_github_api_host(repo, monkeypatch):
    monkeypatch.setattr(gh, "API", "https://ghe.example.com/api/v3")
    repo.git("remote", "set-url", "origin", "git@ghe.example.com:owner/repo.git")
    assert local.origin_repo(repo.path) == ("owner", "repo")
    repo.git("remote", "set-url", "origin", "git@github.com:owner/repo.git")
    assert local.origin_repo(repo.path) is None


def test_load_names_the_repository_directory_and_returns_the_agents_summary(repo):
    summary = {"title": "Cache feeds", "ticket": "BH-1", "body": "Why."}
    write_review(repo, summary=summary)
    info = local.load(repo.path)["local"]
    assert (info["repo"], info["summary"]) == ("work", summary)


@pytest.mark.parametrize("summary", [None, "text", [1], {"title": 3}])
def test_load_gives_no_summary_when_the_file_has_none_or_a_malformed_one(repo, summary):
    write_review(repo, summary=summary)
    assert local.load(repo.path)["local"]["summary"] is None
