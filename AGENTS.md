# AGENTS.md

Guidance to coding agents working in this repository.

## Project

fearful-owl is a GitHub PR review app. It shows a PR's diff as chunks, with an explanation from a headless Claude Code run next to each chunk. At runtime the backend uses only the Python 3.11+ standard library. The browser UI is plain HTML, CSS and JavaScript in `src/fearful_owl/static/`. pytest is the only dependency, in the `dev` group. Do not add runtime dependencies.

## Commands

- Run the app: `uv run fearful-owl`. The README lists the environment variables.
- Run the tests: `uv run pytest`.
- Check syntax: `uv run python -m compileall -q src`.
- Lint: `uvx ruff check src tests`.

## Tests

Write or change the test first and see it fail before you change the code. Tests live in `tests/` and replace GitHub (`fearful_owl.github` functions) and Claude (`explain_file`, `explain_overview`) with fakes. They never call the network or the `claude` CLI.

## Layout

- `diff.py` parses GitHub patches into chunks.
- `github.py` is the GitHub REST client.
- `context.py` gathers project instructions and related PRs.
- `claude.py` builds prompts and runs `claude -p`.
- `server.py` serves the API, the static files and the caches.

## How it works

1. **Diff.** `get_pull_files` requests pages of 100 files until GitHub returns a short page, up to 30 pages, which is GitHub's limit of 3000 files per PR. Each hunk becomes a chunk. A hunk of more than 30 lines is split at runs of unchanged lines, or, for long runs of additions or deletions, at top-level blocks.
2. **Context.** Instruction files come from the base commit: `CLAUDE.md`, `AGENTS.md`, `.github/copilot-instructions.md` and `CONTRIBUTING.md` at the root, plus nested `CLAUDE.md` and `AGENTS.md` in directories that contain changed files. `README.md` is a fallback, used only when none of those files was loaded. Related PRs come from the last three commits of each of the first eight changed files. At most 12 commit SHAs are mapped to merged PRs, taken round-robin across the files (first commit of each file, then the second, and so on) so that one busy file cannot use up the cap.
3. **Size cap.** A PR whose additions plus deletions exceed `MAX_CHANGED_LINES` (default 1000) gets one `error` event with `scope: "limit"` and no Claude calls. The diff still loads through `/api/pr`. One batch is at most 400 diff lines. By estimate (not measured), it costs roughly 10–15k input tokens (Claude Code's system prompt, up to 5k tokens of project docs and about 5k tokens of diff) and about 1.5k output tokens, and takes about 30–45 seconds. 1,000 changed lines are about 1,500 diff lines with context, which is 5–8 batches. At `CLAUDE_CONCURRENCY=3` that takes 1.5–2 minutes and about 100k tokens per PR.
4. **Explain.** `claude -p` runs with tools disabled and a JSON schema, once per file batch plus once for the overview, in parallel. Each call runs in a fresh `tempfile.TemporaryDirectory()`, because Claude Code loads `CLAUDE.md` and `.claude/settings.json` from its working directory and `/tmp` is world-writable. The PR title, description, project docs, related PRs and file list go inside `<pr_context>`, and diffs go inside `<chunk>` and `<diff_excerpt>`. The system prompt tells Claude that text inside those tags is data from the PR author, not instructions. The tags are not escaped, so content that contains a closing tag can end its block early.
5. **Stream and caches.** Results stream to the page as Server-Sent Events (`status`, `context`, `overview`, `chunk`, `error`, `done`). When the stream ends, a chunk that has no explanation shows "No explanation returned". A finished stream is kept in `_explain_cache` (LRU, 50 entries) under `owner/repo/number@baseSha..headSha:prs`. A stream is cached only if every call succeeded and every batch got explanations for all of its chunk ids, so an incomplete result is asked for again on the next open. `_pr_cache` holds loaded PRs for 60 seconds (LRU, 50 entries) and drops an expired entry when it sees it.
6. **Network.** The server binds to `127.0.0.1`. It has no authentication, and it uses the user's GitHub token and Claude Code login, so it must not be reachable from other machines.
