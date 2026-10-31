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

The app fetches the PR from GitHub and splits each file's diff into chunks. Claude Code explains each chunk, and the explanation appears next to the chunk as soon as it is ready.

Claude also gets the PR description, the project's instruction files (such as `CLAUDE.md` and `AGENTS.md`) and a few earlier merged PRs that touched the same files. The "Context given to Claude" panel lists the ones it used.

Opening the same PR again reuses the explanations until the PR changes or you stop the app.

PRs over 1,000 changed lines show the diff without explanations. Set `MAX_CHANGED_LINES` to change the limit.

## Configuration

| Env var | Default | Purpose |
| --- | --- | --- |
| `PORT` | `3000` | HTTP port. The app listens on `127.0.0.1` only. |
| `GITHUB_TOKEN` / `GH_TOKEN` | – | GitHub API auth |
| `GITHUB_API` | `https://api.github.com` | API base URL (for GitHub Enterprise) |
| `CLAUDE_BIN` | `claude` | Path to the Claude Code CLI |
| `CLAUDE_MODEL` | CLI default | Model passed to `--model` |
| `CLAUDE_CONCURRENCY` | `3` | Parallel Claude calls |
| `CLAUDE_TIMEOUT_MS` | `240000` | Timeout for each Claude call |
| `MAX_CHANGED_LINES` | `1000` | Largest PR (additions plus deletions) that gets explanations |

The app uses only the Python standard library. The browser UI is plain HTML/CSS/JS in `src/fearful_owl/static/`.
