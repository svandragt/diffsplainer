"""Gathers review context: project instructions and a limited set of related previous PRs."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from itertools import zip_longest
from pathlib import Path

from . import github as gh
from . import local

ROOT_DOCS = ["CLAUDE.md", "AGENTS.md", ".github/copilot-instructions.md", "CONTRIBUTING.md"]
NESTED_DOCS = {"CLAUDE.md", "AGENTS.md"}
MAX_DOC_CHARS = 6000
MAX_TOTAL_DOC_CHARS = 20000
MAX_PATHS = 8  # changed files we look up history for
MAX_SHAS = 12  # commits we map back to PRs
MAX_PLAN_CHARS = 8000
TICKET_KEY_RE = re.compile(r"(?:[A-Z][A-Z0-9]+-\d+|\d+)(?=-|$)")  # `BH-740` or `1234` at the start of a branch name


def clip(s: str | None, n: int) -> str:
    s = s or ""
    return s[:n] + "\n…[truncated]" if len(s) > n else s


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001 - any failure falls back
        return None


def _changed_dirs(changed: list[str]) -> set[str]:
    return {"/".join(p.split("/")[:i]) for p in changed for i in range(1, p.count("/") + 1)}


def _read_docs(wanted: list[str], read) -> list[dict]:
    """Read each of `wanted` with `read(path)` (None when missing), within the size caps. `wanted` ends with README.md."""
    docs: list[dict] = []
    total = 0
    for path in wanted:
        if total >= MAX_TOTAL_DOC_CHARS:
            break
        # README is only a fallback when there are no real instruction files.
        if path == "README.md" and docs:
            continue
        text = read(path)
        if not text:
            continue
        body = clip(text, min(MAX_DOC_CHARS, MAX_TOTAL_DOC_CHARS - total))
        total += len(body)
        docs.append({"path": path, "body": body})
    return docs


def project_instructions(owner: str, repo: str, ref: str, changed: list[str]) -> list[dict]:
    wanted = list(ROOT_DOCS)
    # Nested CLAUDE.md / AGENTS.md in directories that contain changed files.
    tree = _safe(gh.get_tree, owner, repo, ref)
    if tree and tree.get("tree"):
        dirs = _changed_dirs(changed)
        for node in tree["tree"]:
            d, _, base = node["path"].rpartition("/")
            if node["type"] == "blob" and base in NESTED_DOCS and d in dirs:
                wanted.append(node["path"])
    wanted.append("README.md")  # last, so the fallback check sees the nested files too
    return _read_docs(wanted, lambda path: _safe(gh.get_file, owner, repo, path, ref))


def local_instructions(repo: Path, changed: list[str]) -> list[dict]:
    """`project_instructions` for a local repository: the files are read from its work tree."""

    def read(path: str) -> str | None:
        file = (repo / path).resolve()
        if not file.is_relative_to(repo.resolve()) or not file.is_file():  # a symlink may point at any file
            return None
        try:
            return file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    nested = [f"{d}/{name}" for d in sorted(_changed_dirs(changed)) for name in sorted(NESTED_DOCS)]
    return _read_docs([*ROOT_DOCS, *nested, "README.md"], read)


def local_plan(repo: Path, branch: str) -> dict | None:
    """The plan for `branch` in the git directory's `approach/` folder, as `{name, body}` (body clipped), or None.

    The file is `<branch>.md`, else `<ticket key>.md` when the branch starts with a ticket key. A `*.ticket.md` or
    `*-pr.md` file is not a plan. A candidate whose real path is outside `approach/` is refused."""
    if branch == "HEAD":  # detached: `load` reports it so, and git refuses a branch with that name
        return None
    approach = (Path(local.git(repo, "rev-parse", "--absolute-git-dir").strip()) / "approach").resolve()
    key = TICKET_KEY_RE.match(branch)
    for name in [branch, *([key[0]] if key else [])]:
        file = (approach / f"{name}.md").resolve()
        if name.endswith(("-pr", ".ticket")) or not file.is_relative_to(approach) or not file.is_file():
            continue
        try:
            text = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if text.strip():
            return {"name": f"{name}.md", "body": clip(text, MAX_PLAN_CHARS)}
    return None


def _round_robin_shas(commit_lists: list[list[dict]], limit: int) -> list[str]:
    """First commit of each file, then the second of each, and so on, without repeats."""
    shas: list[str] = []
    for round_ in zip_longest(*commit_lists):
        for commit in round_:
            if commit and commit["sha"] not in shas:
                shas.append(commit["sha"])
    return shas[:limit]


def related_pulls(owner: str, repo: str, pr: dict, changed: list[str], limit: int) -> list[dict]:
    if not limit:
        return []
    paths = changed[:MAX_PATHS]
    with ThreadPoolExecutor(max_workers=6) as pool:
        commit_lists = list(
            pool.map(lambda p: _safe(gh.commits_for_path, owner, repo, p, pr["base"]["sha"]) or [], paths)
        )
        sha_files: dict[str, set[str]] = {}
        for p, commits in zip(paths, commit_lists):
            for c in commits:
                sha_files.setdefault(c["sha"], set()).add(p)
        shas = _round_robin_shas(commit_lists, MAX_SHAS)
        pull_lists = list(pool.map(lambda s: _safe(gh.pulls_for_commit, owner, repo, s) or [], shas))

    found: dict[int, dict] = {}
    for sha, pulls in zip(shas, pull_lists):
        for p in pulls:
            if p["number"] == pr["number"] or not p.get("merged_at"):
                continue
            entry = found.setdefault(p["number"], {"pr": p, "files": set()})
            entry["files"] |= sha_files[sha]

    ranked = sorted(found.values(), key=lambda e: (len(e["files"]), e["pr"]["merged_at"]), reverse=True)
    return [
        {
            "number": e["pr"]["number"],
            "title": e["pr"]["title"],
            "url": e["pr"]["html_url"],
            "merged_at": e["pr"]["merged_at"],
            "files": sorted(e["files"]),
            "body": clip(e["pr"].get("body"), 1200),
        }
        for e in ranked[:limit]
    ]


def local_related_pulls(repo: Path, base: str, changed: list[str], limit: int) -> list[dict]:
    """`related_pulls` for a local repository whose `origin` is on GitHub, as of the commit `base` that GitHub has.
    Any failure, such as no origin, no token or a private repository, gives no PRs and does not stop the review."""
    slug = _safe(local.origin_repo, repo)
    if not slug:
        return []
    return _safe(related_pulls, *slug, {"number": None, "base": {"sha": base}}, changed, limit) or []
