---
name: diffsplainer-review
description: Start Diffsplainer so a person can review your unpushed commits, then answer the comments they leave. Use when you have listed unpushed commits for the person's review, when asked to address Diffsplainer review comments, or when the repo's diffsplainer/review.json has open entries.
---

# Answer Diffsplainer review comments

A person reviews your unpushed commits with `diffsplainer --local`. They request changes on a chunk, ask a question about selected text or write a general comment, then send the comments to you or approve all the commits. Diffsplainer writes these to a JSON file in the repo's git directory. You start Diffsplainer for the person, answer each open entry with a fixup commit or a reply, then wait for the next review.

## Find the review file

The file is `$(git rev-parse --git-dir)/diffsplainer/review.json`. Use `git rev-parse`, not a literal `.git/`, because in a linked worktree `.git` is a file.

If the file does not exist, there are no comments.

## Format

```json
{
  "reviews": [{"head": "<sha>", "at": "<ISO time>", "push": true}],
  "approved": ["<commit sha>"],
  "summary": {"title": "<what the commits do>", "ticket": "<key or URL, or null>", "body": "<the goal and the approach>", "plan": "<absolute path to the plan, or null>"},
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
- An entry with an empty `file` (and an empty `lines` and `diff`) is a general comment on the work as a whole, not on one chunk. Its `sha` is the newest commit of the range at that time, not the commit the comment concerns.
- `lines` is a hint that goes out of date after the first fixup. Find the place by matching `diff`.
- `reply` is `null` until you answer.
- `summary` is yours, and it is optional. The page shows it at the top, and Claude reads it as the description of the work. Each field is a string or `null`.
- `summary.plan` is optional: the absolute path to the plan, a `.md` file, when the plan is not `approach/<branch>.md` in this repo's git directory (a plan shared with another repo, for example). Diffsplainer reads it before `approach/<branch>.md`, and uses the branch file when `plan` is not an existing, readable `.md` file. It counts only next to a `title`, `ticket` or `body`.
- `reviews` lists the HEAD of each review round. `approved` lists approved commits. Diffsplainer fills it only when the person clicks "Approve" or "Approve and push", which adds every commit of that round at once and is possible only when no entry is open.
- `push` is `true` on a round the person ended with "Approve and push", and is absent on every other round. It allows you to push that round's head once the checks in "Push after approval" pass.

## Protocol

1. Read the file. The entries to answer have `status` `open` and `reply` `null`.
2. For a `change` entry, make the change and commit it with `git commit --fixup <sha>`, using the entry's `sha`. For a general comment, use the commit you judge the change belongs to, which is not necessarily the entry's `sha`. Run the tests first. Set `reply` to one or two sentences on what you changed. If you decide not to change it, make no commit and say why in the reply.
3. For a `question` entry, set `reply` to the answer.
4. Read the file again just before writing, because Diffsplainer may have added an entry or changed a status in the meantime. Change only `reply` on the entries you answered.
5. Write the whole file to a temporary file in the same directory, then rename it over `review.json`.
6. List the new commits with `git log --oneline <last reviews[].head>..HEAD`, and wait for the next review. The page shows them when the person reloads it.

## Start the review

When you list your unpushed commits for the person's review, record the review count, write the summary, start Diffsplainer yourself in the background, then start the wait below straight away. Do not ask the person to start it, and do not wait for them to say they have reviewed.

Before you write the summary or start the server, record how many items `reviews` has. The wait below compares against this number, so a round that the person ends before the wait starts is not missed:

```sh
gd="$(git rev-parse --absolute-git-dir)"; mkdir -p "$gd/diffsplainer"
python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1])).get("reviews", [])))' "$gd/diffsplainer/review.json" >"$gd/diffsplainer/reviews.seen" 2>/dev/null || echo 0 >"$gd/diffsplainer/reviews.seen"
```

Then write `summary` into the review file, so the page and Claude say what the work is for:

- `title` is what the commits do, worded as a PR title.
- `ticket` is the ticket or issue key or URL, or `null`.
- `body` is two to five sentences: the goal, the approach, and anything the reviewer should check.
- `plan` is the absolute path to the plan when it is not `approach/<branch>.md` in this repo's git directory, else leave it out.

Write it like a reply: read the file, change only `summary`, write a temporary file in the same directory and rename it over `review.json`. If the file does not exist, create the `diffsplainer` directory in the git directory and the file as `{"reviews":[],"approved":[],"entries":[],"summary":{...}}`. Update the summary when a later round changes the scope. Claude also reads the plan for the branch: the file `plan` names, else `approach/<branch>.md` in the git directory, when there is one. The person sees the summary checked against the plan and the diff, so write what the commits do, not what you intended.

Then check that the plan exists: the file `plan` names, else `approach/<branch>.md`, else `approach/<ticket key>.md` when the branch name starts with a ticket key such as `BH-740` or `1234`. If none of these is an existing file, tell the person "No plan found; looked at <the paths you checked>" and start the review anyway. Not every branch has a plan. A symlink in `approach/` to a file outside it does not count, so for a plan in another repo set `plan`.

```sh
gd="$(git rev-parse --absolute-git-dir)"; mkdir -p "$gd/diffsplainer"
if command -v diffsplainer >/dev/null; then app=diffsplainer; else app="uvx diffsplainer"; fi
up() { curl -sf --max-time 1 -o /dev/null "http://localhost:$1/api/version"; }
for port in $(seq 3000 3020); do
  up $port && continue # a server already answers on this port
  PORT=$port nohup $app --local >"$gd/diffsplainer/server.log" 2>&1 &
  pid=$!
  for _ in $(seq 40); do # up to 20 s: the first uvx run installs the package
    up $port && { echo $pid >"$gd/diffsplainer/server.pid"; echo "http://localhost:$port"; break 2; }
    kill -0 $pid 2>/dev/null || break # the process exited, so the port is busy
    sleep 0.5
  done
  kill $pid 2>/dev/null
done
```

The app opens the page in the browser. Give the person the URL as well. The loop waits up to 20 seconds for each start to answer on its port. A port that another app uses makes the start fail, or already answers, so the loop tries the next one. If no port works, show the person the end of `server.log`.

When the person approves, stop the server: `kill "$(cat "$gd/diffsplainer/server.pid")"`.

## Wait for the next review

The person ends a round with "Send N comments to agent", "Approve" or "Approve and push", and Diffsplainer then adds an item to `reviews`. Nothing else tells you, so watch the file instead of waiting for the person to tell you. The same wait applies before the first review.

Wait until `reviews` has more items than the number in `reviews.seen`, which you recorded before you started the server. When the wait returns, it writes its own count there, so a round that the person ends while you answer is not missed by the next wait:

```sh
gd="$(git rev-parse --absolute-git-dir)"; f="$gd/diffsplainer/review.json"
count() { python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1])).get("reviews", [])))' "$f" 2>/dev/null || echo -1; }
n=$(cat "$gd/diffsplainer/reviews.seen")
until c=$(count); [ "$c" -gt "$n" ]; do sleep 20; done
echo "$c" >"$gd/diffsplainer/reviews.seen"
```

Run it with your harness's background wait, so you use nothing while you wait; in Claude Code, that is the Monitor tool. A file that is missing or mid-write counts as -1, so the wait continues. Comments the person adds before they end the round are part of that round: answer them after the wait returns, not as they appear.

When the wait returns, check for approval first. The round's commits are `git log --format=%H <previous reviews[].head>..<newest reviews[].head>`, or all unpushed commits in the first round. If the newest round has no entry with `status` `open` and every commit of the round is in `approved`, the person approved the work. A round with no commits (answering with replies only leaves the range empty) has nothing to approve, so with no open entry it counts as approval too: the person can end it only with "Approve" or "Approve and push". Stop waiting, stop the server you started, and continue with "Push after approval". Otherwise start again at step 1.

## Push after approval

Both kinds of approval start the same way:

1. If `~/.claude/skills/self-review/scripts/collect-gate.py` exists, run `python3 ~/.claude/skills/self-review/scripts/collect-gate.py --gate push`, which keeps the person's resolved comments.
2. Check that `git rev-parse HEAD` equals the newest `reviews[].head`. If it does not, you committed after the approval, and those commits are unreviewed. Stop before rebasing and report it.
3. Check that `git rev-list --count --min-parents=2 <base>..HEAD` prints `0`. If it does not, the branch has a merge commit. The rebase in the next step would flatten it, and would copy any merged commits that are not on the upstream yet, such as another unpushed branch. Stop before rebasing and report it.
4. Squash the fixup commits onto the base, the merge-base of HEAD with the upstream, else with the default branch: `GIT_SEQUENCE_EDITOR=: git rebase -i --autosquash <base>`. If the rebase conflicts, run `git rebase --abort`, stop and report it.
5. Check that the rebased tree is the tree the person approved: `git diff --quiet <newest reviews[].head> HEAD` exits 0. If it does not, the rebase changed the result. Stop and report it.
6. Run the repo's tests on the result.

After "Approve" (the newest round has no `push`), tell the person the commits are squashed and tested, and push only when they say so.

After "Approve and push" (the newest round has `"push": true`), push without asking if every check below passes. If one fails, do not push: stop and tell the person which check failed and why.

1. No entry has `status` `open`.
2. The branch is not the default branch and not a deploy branch. The default branch is `git symbolic-ref --short refs/remotes/origin/HEAD` without its `origin/` prefix. If that command fails, or HEAD is detached, this check fails. The repo's AGENTS.md lists its deploy branches on one line, `Deploy branches:` followed by the branch names in backticks, such as ``Deploy branches: `main`, `staging` ``. A repo with no such line counts as one where every branch deploys, so this check fails there.
3. The rebase ran without conflicts.
4. The HEAD, merge-commit and tree checks above passed.
5. The tests pass on the rebased result.

Push only this branch, to a remote branch of the same name: `git push origin HEAD:refs/heads/<branch>`, with `-u` when the branch has no upstream. A bare `git push` can push other branches or a differently named upstream, depending on `push.default`. Never use `--force`: the rebase changed only unpushed commits, so a rejected push means the remote changed. Stop and report it. When the push succeeds, list the pushed commits.

## Rules

- Write only `reply` on the entries and the top-level `summary`. Never change `status`, `body`, `quote`, `diff`, `sha`, `lines`, `id`, `created`, `reviews` or `approved`. The person resolves entries and approves commits ("Approve" or "Approve and push") in Diffsplainer.
- Keep every field you do not know, on every entry and at the top level.
- Do not add, delete or reorder entries.
- If the file is not valid JSON, do not write to it. Tell the person.
- Never push, rebase or squash before the person approves. Then follow "Push after approval".
