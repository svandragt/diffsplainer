# fearful-owl

Diff explanations for GitHub pull requests, powered by Claude Code.

Open a PR and the app shows its diff split into logical chunks on the right. On the left, next to each chunk, is Claude's explanation of **what** the change does, **why** it's made, and anything worth checking.

## Run

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/), and the [Claude Code](https://claude.com/claude-code) CLI (`claude`), logged in.

```sh
export GITHUB_TOKEN=ghp_...   # optional for public repos, required for private ones
uv run fearful-owl            # http://localhost:3000
```

Without uv: `PYTHONPATH=src python -m fearful_owl`.

Open a PR in either of these ways:

- paste the PR URL into the address field, or
- swap `github.com` for the app's host in the browser address bar:
  `http://localhost:3000/owner/repo/pull/123`

The address bar always shows the PR you're reviewing, so links can be shared. Add `?prs=N` to choose how many previous PRs go into the context (0–10, default 3).

## How it works

1. **Diff**: fetches the PR and its files from the GitHub API. Each hunk becomes a chunk. A long hunk is split again at gaps of unchanged lines, or at top-level blocks for new code.
2. **Context**:
   - Project instructions from the base branch: `CLAUDE.md`, `AGENTS.md`, `.github/copilot-instructions.md` and `CONTRIBUTING.md`, plus any nested `CLAUDE.md`/`AGENTS.md` in folders that contain changed files. `README.md` is used only when none of these exist.
   - A limited set of previous merged PRs that touched the same files, found through the recent commits on those files.
3. **Explain**: runs `claude -p` headlessly with tools disabled and a JSON schema. There is one call per file batch plus one for the overview, run in parallel. Results stream to the page over Server-Sent Events and fill in each chunk's row. Explanations are cached in memory, keyed by the PR's head commit.

## Configuration

| Env var | Default | Purpose |
| --- | --- | --- |
| `PORT` | `3000` | HTTP port |
| `GITHUB_TOKEN` / `GH_TOKEN` | – | GitHub API auth |
| `GITHUB_API` | `https://api.github.com` | API base URL (for GitHub Enterprise) |
| `CLAUDE_BIN` | `claude` | Path to the Claude Code CLI |
| `CLAUDE_MODEL` | CLI default | Model passed to `--model` |
| `CLAUDE_CONCURRENCY` | `3` | Parallel Claude calls |
| `CLAUDE_TIMEOUT_MS` | `240000` | Timeout for each Claude call |

The app uses only the Python standard library. The browser UI is plain HTML/CSS/JS in `src/fearful_owl/static/`.
