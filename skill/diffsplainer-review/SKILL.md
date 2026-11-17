---
name: diffsplainer-review
description: Start Diffsplainer so a person can review your unpushed commits, then answer the comments they leave. Use when you have listed unpushed commits for the person's review, when asked to address Diffsplainer review comments, or when the repo's diffsplainer/review.json has open entries.
---

# Answer Diffsplainer review comments

A person reviews your unpushed commits with `diffsplainer --local`. They request changes on a chunk or ask a question about selected text, then send the comments to you or approve all the commits. Diffsplainer writes these to a JSON file in the repo's git directory. You start Diffsplainer for the person, answer each open entry with a fixup commit or a reply, then wait for the next review.

## Find the review file

The file is `$(git rev-parse --git-dir)/diffsplainer/review.json`. Use `git rev-parse`, not a literal `.git/`, because in a linked worktree `.git` is a file.

If the file does not exist, there are no comments.

## Format

```json
{
  "reviews": [{"head": "<sha>", "at": "<ISO time>"}],
  "approved": ["<commit sha>"],
  "summary": {"title": "<what the commits do>", "ticket": "<key or URL, or null>", "body": "<the goal and the approach>"},
  "entries": [
    {
      "id": "r1",
      "kind": "change",
      "status": "open",
      "sha": "<commit sha>",
      "file": "src/x.py",
      "lines": "40-52",
      "diff": "<chunk diff>",
      "quote": null,
      "body": "<the person's comment>",
      "reply": null,
      "created": "<ISO time>"
    }
  ]
}
```

- `kind` is `change` (a change request) or `question`. A question has the selected text in `quote`.
- `status` is `open` or `resolved`.
- `sha` is the commit the entry is about.
- `lines` is a hint that goes out of date after the first fixup. Find the place by matching `diff`.
- `reply` is `null` until you answer.
- `summary` is yours, and it is optional. The page shows it at the top, and Claude reads it as the description of the work. Each field is a string or `null`.
- `reviews` lists the HEAD of each review round. `approved` lists approved commits. Diffsplainer fills it only when the person clicks "Approve all and finish", which adds every commit of that round at once and is possible only when no entry is open.

## Protocol

1. Read the file. The entries to answer have `status` `open` and `reply` `null`.
2. For a `change` entry, make the change and commit it with `git commit --fixup <sha>`, using the entry's `sha`. Run the tests first. Set `reply` to one or two sentences on what you changed. If you decide not to change it, make no commit and say why in the reply.
3. For a `question` entry, set `reply` to the answer.
4. Read the file again just before writing, because Diffsplainer may have added an entry or changed a status in the meantime. Change only `reply` on the entries you answered.
5. Write the whole file to a temporary file in the same directory, then rename it over `review.json`.
6. List the new commits with `git log --oneline <last reviews[].head>..HEAD`, and wait for the next review. The page shows them when the person reloads it.

## Start the review

When you list your unpushed commits for the person's review, write the summary, start Diffsplainer yourself in the background, then start the wait below straight away. Do not ask the person to start it, and do not wait for them to say they have reviewed.

Before you start the server, write `summary` into the review file, so the page and Claude say what the work is for:

- `title` is what the commits do, worded as a PR title.
- `ticket` is the ticket or issue key or URL, or `null`.
- `body` is two to five sentences: the goal, the approach, and anything the reviewer should check.

Write it like a reply: read the file, change only `summary`, write a temporary file in the same directory and rename it over `review.json`. If the file does not exist, create the `diffsplainer` directory in the git directory and the file as `{"reviews":[],"approved":[],"entries":[],"summary":{...}}`. Update the summary when a later round changes the scope. Claude also reads the plan for the branch, `approach/<branch>.md` in the git directory, when there is one. The person sees the summary checked against the plan and the diff, so write what the commits do, not what you intended.

```sh
gd="$(git rev-parse --absolute-git-dir)"; mkdir -p "$gd/diffsplainer"
if command -v diffsplainer >/dev/null; then app=diffsplainer; else app="uvx diffsplainer"; fi
for port in $(seq 3000 3020); do
  PORT=$port nohup $app --local >"$gd/diffsplainer/server.log" 2>&1 &
  sleep 3
  if kill -0 $! 2>/dev/null; then echo $! >"$gd/diffsplainer/server.pid"; echo "http://localhost:$port"; break; fi
done
```

The app opens the page in the browser. Give the person the URL as well. A port that another app uses makes the start fail, so the loop tries the next one. If no port works, show the person the end of `server.log`.

When the person approves, stop the server: `kill "$(cat "$gd/diffsplainer/server.pid")"`.

## Wait for the next review

The person ends a round with one button, "Send N comments to agent" or "Approve all and finish", and Diffsplainer then adds an item to `reviews`. Nothing else tells you, so watch the file instead of waiting for the person to tell you. The same wait applies before the first review.

Record how many items `reviews` has, then wait until it has more:

```sh
f="$(git rev-parse --absolute-git-dir)/diffsplainer/review.json"
count() { python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1])).get("reviews", [])))' "$f" 2>/dev/null || echo -1; }
n=$(count); [ "$n" -lt 0 ] && n=0
until [ "$(count)" -gt "$n" ]; do sleep 20; done
```

Run it with your harness's background wait, so you use nothing while you wait; in Claude Code, that is the Monitor tool. A file that is missing or mid-write counts as -1, so the wait continues. Comments the person adds before they end the round are part of that round: answer them after the wait returns, not as they appear.

When the wait returns, check for approval first. The round's commits are `git log --format=%H <previous reviews[].head>..<newest reviews[].head>`, or all unpushed commits in the first round. If the newest round has no entry with `status` `open` and every commit of the round is in `approved`, the person approved the work. Stop waiting, stop the server you started, and tell the person the commits are approved and ready for them to rebase and push. Otherwise start again at step 1.

## Rules

- Write only `reply` on the entries and the top-level `summary`. Never change `status`, `body`, `quote`, `diff`, `sha`, `lines`, `id`, `created`, `reviews` or `approved`. The person resolves entries and approves commits ("Approve all and finish") in Diffsplainer.
- Keep every field you do not know, on every entry and at the top level.
- Do not add, delete or reorder entries.
- If the file is not valid JSON, do not write to it. Tell the person.
- Never push, rebase or squash. Those happen only after the person approves and says so.
