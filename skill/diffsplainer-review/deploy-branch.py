#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Exit 0 when <branch> is safe to push, or exit 1 and print the reason. Run it from inside the repo:
uv run skill/diffsplainer-review/deploy-branch.py <branch>"""

import re
import subprocess
import sys
from pathlib import Path

import yaml

DEPLOY_NAMES = {"production", "preprod", "staging", "develop", "master", "main"}
GLOB = re.compile(r"[*?\[+]")


def out(*cmd):
    done = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if done.returncode:
        raise RuntimeError(f"{' '.join(cmd[:2])} failed: {done.stderr.strip() or f'exit {done.returncode}'}")
    return done.stdout.strip()


def github_repo():
    urls = out("git", "remote", "get-url", "--push", "--all", "origin").splitlines()
    if len(urls) != 1:
        raise ValueError("origin has more than one push URL")
    url = urls[0]
    if not (m := re.search(r"(?:@|//)github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", url)):
        raise ValueError(f"origin is not a GitHub remote: {url}")
    return f"{m[1]}/{m[2]}"


def workflow_matching(branch):
    """The first workflow file whose `on.push.branches` can match `branch`: a glob or `!` pattern matches any."""
    workflows = Path(out("git", "rev-parse", "--show-toplevel")) / ".github" / "workflows"
    for path in sorted(p for p in workflows.glob("*") if p.suffix in (".yml", ".yaml")):
        try:
            doc = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
            if not isinstance(doc, dict):
                raise TypeError("not a mapping")
        except (yaml.YAMLError, TypeError) as e:
            raise ValueError(f"cannot read workflow {path.name}: {' '.join(str(e).split())}") from e
        on = doc.get("on")
        push = on.get("push") if isinstance(on, dict) else None
        patterns = push.get("branches") if isinstance(push, dict) else None
        for p in [patterns] if isinstance(patterns, str) else patterns or []:
            if str(p) == branch or GLOB.search(str(p)) or str(p).startswith("!"):
                return path.name


def refusal(branch):
    """Why `branch` must not be pushed, or None. Raises when a check cannot run."""
    if branch.lower() in DEPLOY_NAMES or branch.lower().endswith("-built"):
        return f"{branch} is a deploy branch name"
    override = Path(out("git", "rev-parse", "--git-common-dir")).resolve() / "info" / "deploy-branches"
    if override.is_file() and branch in {s.strip() for s in override.read_text().splitlines()}:
        return f"{branch} is in the override file {override}"
    repo = github_repo()
    if branch == out("gh", "api", f"repos/{repo}", "-q", ".default_branch"):
        return f"{branch} is the default branch"
    protected = out("gh", "api", f"repos/{repo}/branches?protected=true&per_page=100", "--paginate", "-q", ".[].name")
    if branch in protected.splitlines():
        return f"{branch} is a protected branch"
    if name := workflow_matching(branch):
        return f"workflow {name} runs on push to {branch}"


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: deploy-branch.py <branch>")
    try:
        reason = refusal(sys.argv[1])
    except Exception as e:  # noqa: BLE001 - any failure must refuse the push
        reason = f"error: {' '.join(str(e).split())}"
    if reason:
        print(reason, file=sys.stderr)
        sys.exit(1)
