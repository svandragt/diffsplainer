import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "skill" / "diffsplainer-review" / "deploy-branch.py"

# Stands in for `gh api`: FAKE_GH_FAIL makes it fail, FAKE_GH_PROTECTED is the protected branch list and
# FAKE_GH_DEFAULT the default branch.
FAKE_GH = """#!/bin/sh
[ -n "$FAKE_GH_FAIL" ] && { echo "HTTP 401: Bad credentials" >&2; exit 1; }
case "$*" in
  *protected=true*) printf '%s' "$FAKE_GH_PROTECTED" ;;
  *) echo "$FAKE_GH_DEFAULT" ;;
esac
"""


@pytest.fixture
def check(tmp_path, monkeypatch):
    """Runs deploy-branch.py for a branch in a temp repo and returns the finished process.

    The repo's `origin` is `remote`. GitHub (a fake `gh` first on PATH) reports `default` as the default branch and
    `protected` as the protected branches, or fails with `gh_fails`. `workflows` maps file names in
    `.github/workflows/` to their text, and `override` is the text of `info/deploy-branches`."""
    for k, v in {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}.items():
        monkeypatch.setenv(k, v)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)
    path = tmp_path / "work"

    def run(
        branch,
        remote="git@github.com:o/r.git",
        default="main",
        protected=(),
        gh_fails=False,
        workflows=None,
        override=None,
        push_urls=(),
    ):
        if not path.exists():  # a test may run the script for several branches in one repo
            subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
            subprocess.run(["git", "remote", "add", "origin", remote], cwd=path, check=True)
            for url in push_urls:
                subprocess.run(["git", "remote", "set-url", "--add", "--push", "origin", url], cwd=path, check=True)
            for name, text in (workflows or {}).items():
                (path / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
                (path / ".github" / "workflows" / name).write_text(text)
        if override is not None:
            (path / ".git" / "info").mkdir(exist_ok=True)
            (path / ".git" / "info" / "deploy-branches").write_text(override)
        env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_GH_DEFAULT": default,
            "FAKE_GH_PROTECTED": "\n".join(protected),
            "FAKE_GH_FAIL": "1" if gh_fails else "",
        }
        return subprocess.run(
            [sys.executable, str(SCRIPT), branch], check=False, cwd=path, env=env, capture_output=True, text=True
        )

    return run


def push_on(branches_line):
    return f"on:\n  push:\n    {branches_line}\njobs: {{}}\n"


def test_a_feature_branch_with_no_workflows_protection_or_override_passes(check):
    assert check("1326-fix-things").returncode == 0


def test_the_default_branch_is_refused(check):
    done = check("trunk", default="trunk")
    assert done.returncode == 1
    assert "default branch" in done.stderr
    assert len(done.stderr.splitlines()) == 1


def test_a_protected_branch_is_refused(check):
    done = check("release", protected=["release", "other"])
    assert done.returncode == 1
    assert "protected" in done.stderr


def test_an_unprotected_branch_passes_when_others_are_protected(check):
    assert check("feature", protected=["release"]).returncode == 0


@pytest.mark.parametrize(
    "branches_line",
    ["branches: [live]", "branches:\n      - live", "branches: live", "branches: ['live', 'x']"],
)
def test_a_workflow_that_names_the_branch_in_on_push_branches_refuses_it(check, branches_line):
    done = check("live", workflows={"deploy.yml": push_on(branches_line)})
    assert done.returncode == 1
    assert "workflow deploy.yml" in done.stderr


def test_a_workflow_that_names_other_branches_does_not_refuse_it(check):
    assert check("feature", workflows={"deploy.yml": push_on("branches: [live]")}).returncode == 0


@pytest.mark.parametrize("pattern", ["release/**", "v?", "v[0-9]", "v1+", "'!live'", "'*'"])
def test_a_glob_or_negated_pattern_in_on_push_branches_refuses_any_branch(check, pattern):
    done = check("fix-x", workflows={"w.yaml": push_on(f"branches: [{pattern}]")})
    assert done.returncode == 1
    assert "workflow w.yaml" in done.stderr


def test_branches_ignore_does_not_count(check):
    assert check("fix-x", workflows={"w.yml": push_on("branches-ignore: ['*']")}).returncode == 0


@pytest.mark.parametrize(
    "text",
    [
        "on: push\njobs: {}\n",
        "on: [push, pull_request]\njobs: {}\n",
        "on:\n  push:\njobs: {}\n",
        "on:\n  push:\n    tags: ['v*']\njobs: {}\n",
        "on:\n  pull_request:\n    branches: [fix-x]\njobs: {}\n",
        "name: x\njobs: {}\n",
    ],
)
def test_a_push_trigger_without_a_branch_filter_does_not_count(check, text):
    assert check("fix-x", workflows={"w.yml": text}).returncode == 0


def test_a_quoted_on_key_is_read_like_a_bare_one(check):
    done = check("live", workflows={"w.yml": '"on":\n  push:\n    branches: [live]\n'})
    assert done.returncode == 1


def test_workflow_files_that_are_not_yml_or_yaml_are_ignored(check):
    assert check("live", workflows={"notes.txt": push_on("branches: [live]")}).returncode == 0


def test_a_workflow_that_does_not_parse_refuses_the_branch(check):
    done = check("fix-x", workflows={"w.yml": "on: [unclosed\n  push: {{\n"})
    assert done.returncode == 1
    assert "w.yml" in done.stderr
    assert len(done.stderr.splitlines()) == 1


@pytest.mark.parametrize(
    "kwargs", [{"gh_fails": True}, {"remote": "https://example.com/o/r.git"}, {"remote": "/srv/r.git"}]
)
def test_a_github_failure_or_a_remote_that_is_not_github_refuses_the_branch(check, kwargs):
    done = check("fix-x", **kwargs)
    assert done.returncode == 1
    assert done.stderr.startswith("error")


@pytest.mark.parametrize(
    "remote",
    ["git@github.com:o/r.git", "https://github.com/o/r.git", "https://github.com/o/r", "ssh://git@github.com/o/r.git"],
)
def test_the_github_remote_forms_are_read(check, remote):
    assert check("fix-x", remote=remote).returncode == 0
    assert check("main", remote=remote).returncode == 1


def test_a_branch_in_the_override_file_is_refused(check):
    done = check("client-live", override="# deploys through the app\n\nother\n  client-live  \n")
    assert done.returncode == 1
    assert "override" in done.stderr


def test_the_override_file_adds_refusals_only(check):
    assert check("fix-x", override="# nothing\n\n").returncode == 0
    assert check("main", override="other\n").returncode == 1
    assert check("other", override="other\n").returncode == 1


def test_a_github_failure_shows_the_reason_gh_gave(check):
    assert "Bad credentials" in check("fix-x", gh_fails=True).stderr


@pytest.mark.parametrize(
    "branch",
    [
        "production",
        "preprod",
        "staging",
        "develop",
        "master",
        "main",
        "develop-built",
        "x-built",
        "Production",
        "STAGING",
        "Develop-Built",
    ],
)
def test_a_deploy_name_is_refused_even_when_github_reports_nothing(check, branch):
    done = check(branch, default="trunk")
    assert done.returncode == 1
    assert "deploy" in done.stderr


@pytest.mark.parametrize("branch", ["development", "1326-fix-development", "built", "built-x", "main2"])
def test_a_branch_that_only_resembles_a_deploy_name_passes(check, branch):
    assert check(branch, default="trunk").returncode == 0


@pytest.mark.parametrize("name", ["on", "yes", "off", "0123"])
def test_a_yaml_1_1_scalar_in_on_push_branches_still_refuses_the_branch(check, name):
    done = check(name, workflows={"w.yml": push_on(f"branches: [{name}]")})
    assert done.returncode == 1
    assert "workflow w.yml" in done.stderr


def test_a_push_url_that_is_not_github_refuses_the_branch(check):
    done = check("fix-x", push_urls=["https://example.com/x/y.git"])
    assert done.returncode == 1
    assert done.stderr.startswith("error")


def test_a_github_push_url_is_read(check):
    assert check("fix-x", push_urls=["https://github.com/o/r.git"]).returncode == 0


def test_more_than_one_push_url_refuses_the_branch(check):
    done = check("fix-x", push_urls=["https://github.com/o/r.git", "https://github.com/o/s.git"])
    assert done.returncode == 1
    assert "more than one push URL" in done.stderr
