"""Gathers review context: project instructions and a limited set of related previous PRs."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from . import github as gh

ROOT_DOCS = ["CLAUDE.md", "AGENTS.md", ".github/copilot-instructions.md", "CONTRIBUTING.md", "README.md"]
NESTED_DOCS = {"CLAUDE.md", "AGENTS.md"}
MAX_DOC_CHARS = 6000
MAX_TOTAL_DOC_CHARS = 20000
MAX_PATHS = 8  # changed files we look up history for
MAX_SHAS = 12  # commits we map back to PRs


def clip(s: str | None, n: int) -> str:
    s = s or ""
    return s[:n] + "\n…[truncated]" if len(s) > n else s


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception:
        return None


def project_instructions(owner: str, repo: str, ref: str, changed: list[str]) -> list[dict]:
    wanted = list(ROOT_DOCS)
    # Nested CLAUDE.md / AGENTS.md in directories that contain changed files.
    tree = _safe(gh.get_tree, owner, repo, ref)
    if tree and tree.get("tree"):
        dirs = {"/".join(p.split("/")[:i]) for p in changed for i in range(1, p.count("/") + 1)}
        for node in tree["tree"]:
            d, _, base = node["path"].rpartition("/")
            if node["type"] == "blob" and base in NESTED_DOCS and d in dirs:
                wanted.append(node["path"])

    docs: list[dict] = []
    total = 0
    for path in wanted:
        if total >= MAX_TOTAL_DOC_CHARS:
            break
        # README is only a fallback when there are no real instruction files.
        if path == "README.md" and docs:
            continue
        text = _safe(gh.get_file, owner, repo, path, ref)
        if not text:
            continue
        body = clip(text, min(MAX_DOC_CHARS, MAX_TOTAL_DOC_CHARS - total))
        total += len(body)
        docs.append({"path": path, "body": body})
    return docs


def related_pulls(owner: str, repo: str, pr: dict, changed: list[str], limit: int) -> list[dict]:
    if not limit:
        return []
    paths = changed[:MAX_PATHS]
    with ThreadPoolExecutor(max_workers=6) as pool:
        commit_lists = list(pool.map(lambda p: _safe(gh.commits_for_path, owner, repo, p, pr["base"]["sha"]) or [], paths))
        sha_files: dict[str, set[str]] = {}
        for p, commits in zip(paths, commit_lists):
            for c in commits:
                sha_files.setdefault(c["sha"], set()).add(p)
        shas = list(sha_files)[:MAX_SHAS]
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
