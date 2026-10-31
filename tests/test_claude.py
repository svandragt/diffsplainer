import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from diffsplainer import claude

FAKE = Path(__file__).with_name("fake_claude.py")
LINE = {"t": "add", "o": None, "n": 1, "s": "x"}
CTX = {
    "pr": {"number": 1, "title": "t", "user": "u", "base": "main", "head": "x", "body": ""},
    "instructions": [],
    "related": [],
    "files": [],
}
FILE = {"filename": "a.py", "status": "added", "chunks": [{"id": "c0", "scope": "", "lines": [LINE]}]}


@pytest.fixture
def fake(monkeypatch, tmp_path):
    record = tmp_path / "calls.jsonl"
    monkeypatch.setattr(claude, "CLAUDE_BIN", str(FAKE))
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(record))
    monkeypatch.setenv("ENABLE_CLAUDEAI_MCP_SERVERS", "true")
    return lambda: [json.loads(line) for line in record.read_text().splitlines()] if record.exists() else []


def test_call_isolates_claude_from_the_user_config(fake):
    claude.run_claude("prompt", {})
    call = fake()[0]
    assert {"--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"} <= set(call["argv"])
    assert call["argv"][call["argv"].index("--setting-sources") + 1] == ""
    assert call["mcp"] == "false"


def test_model_defaults_to_sonnet(fake):
    claude.run_claude("prompt", {})
    argv = fake()[0]["argv"]
    assert argv[argv.index("--model") + 1] == claude.MODEL
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_MODEL"}
    out = subprocess.run(
        [sys.executable, "-c", "from diffsplainer import claude; print(claude.MODEL)"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "sonnet"


def test_calls_share_one_private_directory(fake):
    claude.run_claude("one", {})
    claude.run_claude("two", {})
    first, second = fake()
    cwd = Path(first["cwd"])
    assert first["cwd"] == second["cwd"]
    assert cwd != Path(tempfile.gettempdir()).resolve()
    assert stat.S_IMODE(cwd.stat().st_mode) == 0o700


def test_cancel_kills_claude_and_raises(fake, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")
    cancel, errors = threading.Event(), []

    def call():
        try:
            claude.run_claude("prompt", {}, cancel)
        except claude.ClaudeError as e:
            errors.append(e)

    thread = threading.Thread(target=call)
    thread.start()
    deadline = time.monotonic() + 5
    while not fake() and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    cancel.set()
    thread.join(2)
    assert not thread.is_alive()
    assert isinstance(errors[0], claude.ClaudeCancelled)
    pid = fake()[0]["pid"]
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_timeout_raises_a_claude_error(fake, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")
    monkeypatch.setattr(claude, "TIMEOUT_S", 0.6)
    with pytest.raises(claude.ClaudeError, match="timed out after"):
        claude.run_claude("prompt", {})


def test_missing_binary_raises_a_claude_error(monkeypatch):
    monkeypatch.setattr(claude, "CLAUDE_BIN", "/nonexistent/claude")
    with pytest.raises(claude.ClaudeError, match="Could not start Claude Code"):
        claude.run_claude("prompt", {})


def test_explain_calls_pass_cancel_to_run_claude(monkeypatch):
    seen = []
    monkeypatch.setattr(claude, "run_claude", lambda prompt, schema, cancel=None: seen.append(cancel))
    cancel = threading.Event()
    claude.explain_file(CTX, FILE, cancel)
    claude.explain_overview(CTX, [FILE], cancel)
    assert seen == [cancel, cancel]


def test_term_schema_asks_for_general_meaning_and_meaning_here():
    props = claude.TERM_SCHEMA["properties"]
    assert claude.TERM_SCHEMA["required"] == ["meaning", "here"]
    assert props["meaning"] == {
        "type": "string",
        "description": (
            "What the selected term or phrase means in general, for someone who has never met it. "
            "One or two sentences, plain language."
        ),
    }
    assert props["here"] == {
        "type": "string",
        "description": "What it does in this pull request, in one sentence.",
    }


def _capture_prompt(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        claude,
        "run_claude",
        lambda prompt, schema, cancel=None: seen.update(prompt=prompt, schema=schema, cancel=cancel),
    )
    return seen


def test_explain_term_prompt_holds_the_chunk_paragraph_and_selection_but_no_project_context(monkeypatch):
    seen = _capture_prompt(monkeypatch)
    ctx = {
        **CTX,
        "pr": {**CTX["pr"], "title": "Cache feeds", "body": "Send conditional requests."},
        "instructions": [{"path": "AGENTS.md", "body": "SECRET-INSTRUCTIONS"}],
        "related": [{"number": 9, "title": "SECRET-RELATED", "merged_at": "", "files": [], "body": ""}],
    }
    cancel = threading.Event()
    claude.explain_term(ctx, FILE["chunks"][0], "conditional GET", "Sends a conditional GET on refresh.", cancel)
    prompt = seen["prompt"]
    assert prompt.startswith(claude.SYSTEM)
    assert "Cache feeds" in prompt and "Send conditional requests." in prompt
    assert '<chunk id="c0">' in prompt and claude.to_text([LINE]) in prompt
    assert "<paragraph>\nSends a conditional GET on refresh.\n</paragraph>" in prompt
    assert "<selection>\nconditional GET\n</selection>" in prompt
    assert "SECRET-INSTRUCTIONS" not in prompt and "SECRET-RELATED" not in prompt
    assert seen["schema"] is claude.TERM_SCHEMA
    assert seen["cancel"] is cancel


def test_explain_term_without_a_chunk_leaves_out_the_diff(monkeypatch):
    seen = _capture_prompt(monkeypatch)
    claude.explain_term(CTX, None, "term", "paragraph")
    assert "<chunk id=" not in seen["prompt"]
    assert "<selection>\nterm\n</selection>" in seen["prompt"]


def test_fake_claude_answers_each_schema_the_app_sends(fake):
    term = claude.explain_term(CTX, None, "term", "paragraph")
    chunks = claude.explain_file(CTX, FILE)
    overview = claude.explain_overview(CTX, [FILE])
    assert term == {"meaning": "Fake meaning.", "here": "Fake here."}
    assert [c["id"] for c in chunks["chunks"]] == ["c0"]
    assert {"title", "what", "why", "concern"} <= set(chunks["chunks"][0])
    assert set(overview) == {"summary", "approach", "review_focus"} and len(overview["review_focus"]) == 1


def test_fake_claude_runs_without_a_record_file(monkeypatch):
    monkeypatch.setattr(claude, "CLAUDE_BIN", str(FAKE))
    monkeypatch.delenv("FAKE_CLAUDE_RECORD", raising=False)
    assert claude.explain_term(CTX, None, "term", "paragraph") == {"meaning": "Fake meaning.", "here": "Fake here."}


def test_private_cwd_is_recreated_after_it_is_deleted(monkeypatch):
    monkeypatch.setattr(claude, "_cwd", None)
    first = claude._private_cwd()
    shutil.rmtree(first)
    second = claude._private_cwd()
    assert os.path.isdir(second)
    assert stat.S_IMODE(os.stat(second).st_mode) == 0o700
    shutil.rmtree(second)


def test_chunk_schema_asks_for_high_level_what_stated_why_and_specific_concern():
    props = claude.CHUNK_SCHEMA["properties"]["chunks"]["items"]["properties"]
    assert props["what"]["description"] == (
        "One sentence, under about 20 words, on the effect of the change: what it lets the code or the user do "
        "differently. No line-by-line narration. Leave identifiers to the diff unless one is the point."
    )
    assert props["why"]["description"] == (
        "One or two sentences on the reason: the problem it solves or the PR goal it serves. Cite the PR description, "
        "project instructions or a related PR when they give the reason. If none does, write 'Reason not stated in "
        "the PR.' Don't guess."
    )
    assert props["concern"]["description"] == (
        "Only a specific risk a reviewer can verify in this chunk: a likely bug, an unhandled edge case, a security "
        "or data issue, or a mismatch with the PR description or project instructions. Name the line or input that "
        "triggers it. Leave it empty rather than give generic advice."
    )
    assert 'Keep "what" high-level; the diff beside it shows the detail.' in claude.SYSTEM


def test_overview_schema_asks_for_outcome_design_choice_and_specific_risks():
    props = claude.OVERVIEW_SCHEMA["properties"]
    assert props["summary"]["description"] == (
        "Two sentences at most: the outcome the PR delivers for users or the codebase, and its reason as the PR "
        "description or context states it. No file-by-file tour. "
        "If nothing states the reason, say 'Reason not stated in the PR.'"
    )
    assert props["approach"]["description"] == (
        "One or two sentences on the shape of the solution: the main design choice, and any alternative the PR "
        "mentions. Skip mechanics the diff shows."
    )
    assert props["review_focus"]["description"] == (
        "Up to 3 specific risks a reviewer can verify across the PR, such as a cross-file interaction, an untested "
        "named case, or a mismatch with the PR description. An empty list rather than generic advice."
    )


def test_system_asks_for_plain_language():
    line = 'Write in plain language: short sentences, common words, active voice. Name the thing that acts. No filler, hedging or marketing words such as "robust", "seamless" or "leverage".'
    assert line in claude.SYSTEM
