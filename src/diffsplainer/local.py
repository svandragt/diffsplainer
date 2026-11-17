"""Local commit review: a repository's unpushed commits as one diff of explained chunks.

Nothing here writes to the repository: it only runs read-only git commands.
"""

from __future__ import annotations

import re
import subprocess
from functools import lru_cache
from pathlib import Path

from . import github, review
from .diff import chunk_files, tree_order

DEFAULT_REFS = ("refs/remotes/origin/HEAD", "refs/heads/main", "refs/heads/master")
BLAME_RE = re.compile(
    rf"^({review.SHA_RE.pattern}) \d+ (\d+)", re.MULTILINE
)  # `--porcelain` entry line: sha, original line, final line
STATUS = {"A": "added", "D": "removed", "R": "renamed"}
# https://host/owner/repo.git, git@host:owner/repo.git or ssh://git@host/owner/repo.git
REMOTE_RE = re.compile(r"^(?:\w+://)?(?:[^@/]+@)?([^/:]+)[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")


def run(repo: Path, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", *args], cwd=repo, input=stdin, capture_output=True, encoding="utf-8", errors="replace", check=False
        )
    except OSError as e:
        raise ValueError(f"Cannot run git in {repo}: {e}") from None


def git(repo: Path, *args: str) -> str:
    r = run(repo, *args)
    if r.returncode:
        raise ValueError(f"git {args[0]} failed in {repo}: {r.stderr.strip()}")
    return r.stdout


def is_ancestor(repo: Path, a: str, b: str) -> bool:
    return run(repo, "merge-base", "--is-ancestor", a, b).returncode == 0


def origin_repo(repo: Path) -> tuple[str, str] | None:
    """The owner and name of the repository on GitHub that `origin` points to, or None."""
    r = run(repo, "remote", "get-url", "origin")
    m = REMOTE_RE.match(r.stdout.strip()) if r.returncode == 0 else None
    # `.` and `..` would turn the GitHub API path into a different endpoint
    if not m or m[1].lower() != github.host() or {m[2], m[3]} & {".", ".."}:
        return None
    return m[2], m[3]


def current_range(repo: Path) -> tuple[str, str]:
    """The base and HEAD shas of the commits under review."""
    _, base, head = ranges(repo)
    return base, head


def upstream_merge_base(repo: Path, tip: str = "HEAD") -> str:
    """The last commit of `tip` that the remote has, from the upstream or else the default branch."""
    for ref in ("@{upstream}", *DEFAULT_REFS):
        r = run(repo, "merge-base", tip, ref)
        if r.returncode == 0:
            return r.stdout.strip()
    raise ValueError("The branch has no upstream, and the repository has no default branch to compare it with")


def ranges(repo: Path) -> tuple[str, str, str]:
    """The merge-base with the upstream, the base of the commits under review, and HEAD.

    The merge-base is the last commit that is on the remote, which the base is not once a review round has been
    recorded for an unpushed head."""
    head = git(repo, "rev-parse", "HEAD").strip()
    merge_base = base = upstream_merge_base(repo)
    # The newest review round's head, if it is a commit of this range, is where the next round starts. The value is
    # from a file another program writes, so it must be a sha before it goes to git.
    reviewed = review.last_head(review.read(repo))
    if reviewed and is_ancestor(repo, base, reviewed) and is_ancestor(repo, reviewed, head):
        base = reviewed
    return merge_base, base, head


def pinned_range(repo: Path, base: str, head: str) -> tuple[str, str]:
    """The `base` and `head` a page showed, if they are still a range of this branch: full shas, `base` in the history of
    `head`, and `head` in the history of HEAD. Anything else is a ValueError that tells the page to reload. The values
    come from a query string, so only full shas go to git."""
    if not (review.SHA_RE.fullmatch(base) and review.SHA_RE.fullmatch(head)):
        raise ValueError(
            "The request must name the base and head of the commits on the page as full shas. Reload the page."
        )
    if not (is_ancestor(repo, base, head) and is_ancestor(repo, head, "HEAD")):
        raise ValueError("The commits changed since this page loaded. Reload the page.")
    return base, head


def list_commits(repo: Path, base: str, head: str) -> list[dict]:
    # --no-show-signature: with `log.showSignature` set, git log prints the signature check into the format
    out = git(repo, "log", "--reverse", "--no-show-signature", "--format=%H%x00%s%x00%b%x00", f"{base}..{head}")
    f = out.split("\0")
    return [{"sha": f[i].strip(), "subject": f[i + 1], "body": f[i + 2].strip()} for i in range(0, len(f) - 1, 3)]


def diff_files(repo: Path, base: str, head: str) -> list[dict]:
    """The combined diff as GitHub-style file dicts (`patch`, `status`, ...), in the order of the Files changed tab."""
    args = ["diff", "-M", "--no-color", "--no-ext-diff", "--no-textconv", "-U3"]
    tokens = git(repo, *args, "--name-status", "-z", base, head).split("\0")[:-1]
    patches = iter(("\n" + git(repo, *args, base, head)).split("\ndiff --git ")[1:])  # same order as the names
    files, i = [], 0
    while i < len(tokens):
        status = tokens[i]
        names = tokens[i + 1 : i + 3] if status[0] == "R" else tokens[i + 1 : i + 2]
        i += 1 + len(names)
        # A type change (file to symlink, say) prints a deletion and a creation for its one name-status entry.
        parts = [next(patches, "") for _ in range(2 if status[0] == "T" else 1)]
        starts = [(p, p.find("\n@@ ")) for p in parts]  # a binary file, a pure rename or a mode change has no hunks
        body = "\n".join(p[s + 1 :].rstrip("\n") for p, s in starts if s >= 0)
        lines = body.split("\n")
        files.append(
            {
                "filename": names[-1],
                "previous_filename": names[0] if status[0] == "R" else None,
                "status": STATUS.get(status[0], "modified"),
                "patch": body,
                "additions": sum(l.startswith("+") for l in lines),
                "deletions": sum(l.startswith("-") for l in lines),
            }
        )
    return sorted(files, key=lambda f: tree_order(f["filename"]))


def blame(repo: Path, rev_range: str, path: str, *flags: str) -> dict[int, str]:
    """Line number -> sha for one file, or {} when git cannot blame it (a submodule, for one)."""
    r = run(repo, "blame", "--porcelain", "--no-textconv", *flags, rev_range, "--", path)
    return {int(n): sha for sha, n in BLAME_RE.findall(r.stdout)} if r.returncode == 0 else {}


def tag_chunks(repo: Path, base: str, head: str, commits: list[dict], files: list[dict]) -> None:
    """Give each chunk the `sha` of the commit it belongs to, or None when git cannot tell.

    A chunk with added lines belongs to the newest commit that wrote one of them (`git blame`). A deletion-only chunk
    belongs to the newest commit that deleted one of its lines."""
    shas = [c["sha"] for c in commits]
    pos = {sha: i for i, sha in enumerate(shas)}
    pos[base] = -1
    rev_range = f"{base}..{head}"
    for f in files:
        written = deleted = None
        for c in f["chunks"]:
            adds = [l["n"] for l in c["lines"] if l["t"] == "add"]
            if adds:
                written = blame(repo, rev_range, f["filename"]) if written is None else written
                i = max((pos.get(written.get(n), -1) for n in adds), default=-1)
            else:
                # Reverse blame names the last commit a line existed in; the commit after it deleted the line.
                deleted = (
                    blame(repo, rev_range, f["previous"] or f["filename"], "--reverse") if deleted is None else deleted
                )
                last = (deleted.get(l["o"]) for l in c["lines"] if l["t"] == "del")
                i = max((pos[sha] + 1 for sha in last if sha in pos), default=-1)
            c["sha"] = shas[i] if 0 <= i < len(shas) else None


def generated_paths(repo: Path, names: list[str]) -> set[str]:
    """The `names` whose `linguist-generated` attribute is set or true, from one `git check-attr`. A failure gives none:
    the lockfile names still count, and the review goes on."""
    r = run(repo, "check-attr", "-z", "--stdin", "linguist-generated", stdin="".join(f"{n}\0" for n in names))
    out = r.stdout.split("\0") if r.returncode == 0 else []
    return {out[i] for i in range(0, len(out) - 2, 3) if out[i + 2] in ("set", "true")}  # path, attribute, value


@lru_cache(maxsize=8)
def changes(repo: Path, base: str, head: str) -> tuple[list[dict], list[dict]]:
    """The commits and tagged chunks of a range. Cached because the page, the explain stream and every term click load
    them, and blame runs once per file; shas fix the result. Callers must not modify it."""
    commits = list_commits(repo, base, head)
    changed = diff_files(repo, base, head)
    files = chunk_files(changed, generated_paths(repo, [f["filename"] for f in changed]))
    tag_chunks(repo, base, head, commits, files)
    return commits, files


def load(repo: Path, base: str | None = None, head: str | None = None) -> dict:
    """The commits under review, their combined diff chunked and tagged with commits, and the review file. `summary` is
    the agent's description of the work from that file, checked by `review.summary`. `base` and `head`, checked by
    `pinned_range`, are the range a page showed: chunk ids belong to one diff, so a later request must use the same."""
    if base is None or head is None:
        merge_base, base, head = ranges(repo)
    else:
        merge_base = upstream_merge_base(repo, head)
    commits, files = changes(repo, base, head)
    branch = git(repo, "branch", "--show-current").strip() or "HEAD"
    file = review.read(repo)
    return {
        "local": {
            "repo": repo.resolve().name,
            "branch": branch,
            "base": base,
            "head": head,
            "merge_base": merge_base,
            "commits": commits,
            "summary": review.summary(file),
        },
        "files": files,
        "review": file,
    }
