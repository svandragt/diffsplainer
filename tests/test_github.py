import subprocess

import pytest

from diffsplainer import github


@pytest.fixture(autouse=True)
def _fresh_token_cache():
    github._token.cache_clear()
    yield
    github._token.cache_clear()


def _env(monkeypatch, **env):
    for k in ("GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def _gh_cli(monkeypatch, result):
    def fake(*a, **kw):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(subprocess, "run", fake)


def _done(code, out):
    return subprocess.CompletedProcess(["gh"], code, stdout=out, stderr="")


def test_github_token_wins(monkeypatch):
    _env(monkeypatch, GITHUB_TOKEN="a", GH_TOKEN="b")
    _gh_cli(monkeypatch, _done(0, "c\n"))
    assert github._token() == "a"


def test_gh_token_used_when_github_token_unset(monkeypatch):
    _env(monkeypatch, GH_TOKEN="b")
    _gh_cli(monkeypatch, _done(0, "c\n"))
    assert github._token() == "b"


def test_cli_token_used_when_no_env(monkeypatch):
    _env(monkeypatch)
    _gh_cli(monkeypatch, _done(0, "c\n"))
    assert github._token() == "c"


@pytest.mark.parametrize(
    "result",
    [FileNotFoundError(), subprocess.TimeoutExpired("gh", 5), _done(1, ""), _done(0, "\n")],
)
def test_no_token_when_cli_unavailable_or_fails(monkeypatch, result):
    _env(monkeypatch)
    _gh_cli(monkeypatch, result)
    assert github._token() == ""


@pytest.mark.parametrize(
    "api, host",
    [
        ("https://api.github.com", "github.com"),
        ("https://ghe.example.com/api/v3", "ghe.example.com"),
    ],
)
def test_cli_is_asked_for_the_token_of_the_api_host(monkeypatch, api, host):
    _env(monkeypatch)
    monkeypatch.setattr(github, "API", api)
    argv = []
    monkeypatch.setattr(subprocess, "run", lambda a, **k: argv.append(a) or _done(0, "c"))
    github._token()
    assert argv == [["gh", "auth", "token", "--hostname", host]]


def test_cli_runs_once(monkeypatch):
    _env(monkeypatch)
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(1) or _done(0, "c"))
    github._token()
    github._token()
    assert len(calls) == 1


def _pages(monkeypatch, sizes: list[int]) -> list[str]:
    """Serve one page per entry in `sizes`; returns the requested paths."""
    requested: list[str] = []

    def fake(path, **_):
        requested.append(path)
        page = int(path.rsplit("page=", 1)[1])
        return [{"filename": f"f{page}-{i}"} for i in range(sizes[page - 1])]

    monkeypatch.setattr(github, "gh", fake)
    return requested


def test_get_pull_files_reads_past_ten_pages_until_a_short_page(monkeypatch):
    requested = _pages(monkeypatch, [100] * 25 + [40])
    assert len(github.get_pull_files("o", "r", 1)) == 2540
    assert len(requested) == 26


def test_get_pull_files_stops_at_githubs_3000_file_limit(monkeypatch):
    requested = _pages(monkeypatch, [100] * 40)
    assert len(github.get_pull_files("o", "r", 1)) == 3000
    assert len(requested) == 30
