# diffsplainer

Diff explanations for GitHub pull requests, powered by Claude Code.

Open a PR and the app shows its diff split into logical chunks on the right. On the left, next to each chunk, is Claude's explanation of **what** the change does, **why** it's made, and anything worth checking.

## Run

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/), and the [Claude Code](https://claude.com/claude-code) CLI (`claude`), logged in.

```sh
uvx diffsplainer
```

Then open <http://localhost:3000>.

Private repos need you to be logged in with the GitHub CLI (`gh auth login`), or to set `GITHUB_TOKEN`. If you log in with `gh` after starting the app, restart it.

Open a PR in either of these ways:

- paste the PR URL into the address field, or
- swap `github.com` for the app's host in the browser address bar:
  `http://localhost:3000/owner/repo/pull/123`

To open any GitHub PR with one click, drag the Diffsplain link from the start page to your bookmarks bar, then click it on a PR page.

The address bar always shows the PR you're reviewing, so links can be shared. To change how many related PRs Claude reads, add `?prs=N` (0–10, default 3) to the address.

## How it works

The app fetches the PR from GitHub and splits each file's diff into chunks. Claude Code explains each chunk, and the explanation appears next to the chunk as soon as it is ready.

Claude also gets the PR description, the project's instruction files (such as `CLAUDE.md` and `AGENTS.md`) and a few earlier merged PRs that touched the same files. The "Context given to Claude" panel lists the ones it used.

The app runs Claude Code without your own MCP servers, hooks, slash commands and `CLAUDE.md`, so each call stays small. Closing or reloading the page stops the Claude calls it started.

To look up a word or phrase in an explanation, select it and click **Explain**. A popup shows what it means in general and what it does in this PR.

Opening the same PR again reuses the explanations while its commits stay the same. The app drops them when it restarts, or when it makes room for newer PRs (it keeps 50). Editing the PR title or description does not refresh them.

Code in the diff is syntax-highlighted for common languages, chosen by file extension.

PRs over 1,000 changed lines show the diff without explanations. Set `MAX_CHANGED_LINES` to change the limit.

## Configuration

| Env var | Default | Purpose |
| --- | --- | --- |
| `PORT` | `3000` | HTTP port. The app listens on `127.0.0.1` only. |
| `GITHUB_TOKEN` / `GH_TOKEN` | GitHub CLI token | GitHub API auth |
| `GITHUB_API` | `https://api.github.com` | API base URL (for GitHub Enterprise) |
| `CLAUDE_BIN` | `claude` | Path to the Claude Code CLI |
| `CLAUDE_MODEL` | `sonnet` | Model passed to `--model`. The app ignores the model set in your Claude Code settings. |
| `CLAUDE_CONCURRENCY` | `6` | Parallel Claude calls |
| `CLAUDE_TIMEOUT_MS` | `240000` | Timeout for each Claude call |
| `MAX_CHANGED_LINES` | `1000` | Largest PR (additions plus deletions) that gets explanations |

The app uses only the Python standard library. The browser UI is plain HTML/CSS/JS in `src/diffsplainer/static/`.

## Development

To run the app from a clone of the repository:

```sh
git clone https://github.com/svandragt/diffsplainer
cd diffsplainer
uv run diffsplainer            # http://localhost:3000
```

Without uv: `PYTHONPATH=src python -m diffsplainer`.
