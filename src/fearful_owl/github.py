"""Minimal GitHub REST client (stdlib only). Uses GITHUB_TOKEN / GH_TOKEN when present."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

API = os.environ.get("GITHUB_API", "https://api.github.com").rstrip("/")
TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
FILES_MAX_PAGES = 30  # GitHub lists at most 3000 files (30 pages of 100) per PR


class GitHubError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _request(path: str, raw: bool, token: bool):
    headers = {
        "User-Agent": "fearful-owl-review",
        "Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token and TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(API + path, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as res:
        body = res.read().decode("utf-8", "replace")
    return body if raw else json.loads(body)


def gh(path: str, raw: bool = False, allow_404: bool = False):
    try:
        try:
            return _request(path, raw, token=True)
        except urllib.error.HTTPError as e:
            # A bad token on a public repo: retry this request anonymously.
            if e.code == 401 and TOKEN:
                return _request(path, raw, token=False)
            raise
    except urllib.error.HTTPError as e:
        if allow_404 and e.code == 404:
            return None
        detail = e.read().decode("utf-8", "replace")[:200]
        raise GitHubError(e.code, f"GitHub {e.code} on {path}: {detail}") from None


def q(s: str) -> str:
    return urllib.parse.quote(s, safe="")


def get_pull(owner: str, repo: str, num: int) -> dict:
    return gh(f"/repos/{owner}/{repo}/pulls/{num}")


def get_pull_files(owner: str, repo: str, num: int) -> list[dict]:
    files: list[dict] = []
    for page in range(1, FILES_MAX_PAGES + 1):
        batch = gh(f"/repos/{owner}/{repo}/pulls/{num}/files?per_page=100&page={page}")
        files.extend(batch)
        if len(batch) < 100:
            break
    return files


def get_file(owner: str, repo: str, path: str, ref: str) -> str | None:
    return gh(
        f"/repos/{owner}/{repo}/contents/{urllib.parse.quote(path)}?ref={q(ref)}",
        raw=True,
        allow_404=True,
    )


def get_tree(owner: str, repo: str, ref: str) -> dict | None:
    return gh(f"/repos/{owner}/{repo}/git/trees/{q(ref)}?recursive=1", allow_404=True)


def commits_for_path(owner: str, repo: str, path: str, ref: str, n: int = 3) -> list[dict] | None:
    return gh(f"/repos/{owner}/{repo}/commits?path={q(path)}&sha={q(ref)}&per_page={n}", allow_404=True)


def pulls_for_commit(owner: str, repo: str, sha: str) -> list[dict] | None:
    return gh(f"/repos/{owner}/{repo}/commits/{sha}/pulls", allow_404=True)
