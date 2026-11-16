---
name: diffsplainer-review
description: Answer the review comments a person left on unpushed commits in Diffsplainer. Use when asked to address Diffsplainer review comments, or when the repo's diffsplainer/review.json has open entries.
---

# Answer Diffsplainer review comments

A person reviews your unpushed commits with `diffsplainer --local`. They approve commits, request changes on a chunk, or ask a question about selected text. Diffsplainer writes these to a JSON file in the repo's git directory. You answer each open entry with a fixup commit or a reply, then wait for the next review.

## Find the review file

The file is `$(git rev-parse --git-dir)/diffsplainer/review.json`. Use `git rev-parse`, not a literal `.git/`, because in a linked worktree `.git` is a file.

If the file does not exist, there are no comments.

## Format

```json
{
  "reviews": [{"head": "<sha>", "at": "<ISO time>"}],
  "approved": ["<commit sha>"],
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
- `reviews` lists the HEAD of each review round. `approved` lists approved commits.

## Protocol

1. Read the file. The entries to answer have `status` `open` and `reply` `null`.
2. For a `change` entry, make the change and commit it with `git commit --fixup <sha>`, using the entry's `sha`. Run the tests first. Set `reply` to one or two sentences on what you changed. If you decide not to change it, make no commit and say why in the reply.
3. For a `question` entry, set `reply` to the answer.
4. Read the file again just before writing, because Diffsplainer may have added an entry or changed a status in the meantime. Change only `reply` on the entries you answered.
5. Write the whole file to a temporary file in the same directory, then rename it over `review.json`.
6. List the new commits with `git log --oneline <last reviews[].head>..HEAD`, and wait for the next review.

## Wait for the next review

The person clicks Finish review when a round is done, and Diffsplainer then adds an item to `reviews`. Nothing else tells you, so watch the file instead of waiting for the person to tell you. The same wait applies before the first review.

Record how many items `reviews` has, then wait until it has more:

```sh
f="$(git rev-parse --absolute-git-dir)/diffsplainer/review.json"
count() { python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1])).get("reviews", [])))' "$f" 2>/dev/null || echo -1; }
n=$(count); [ "$n" -lt 0 ] && n=0
until [ "$(count)" -gt "$n" ]; do sleep 20; done
```

Run it with your harness's background wait, so you use nothing while you wait; in Claude Code, that is the Monitor tool. When it returns, start again at step 1. A file that is missing or mid-write counts as -1, so the wait continues. Comments the person adds before Finish review are part of that round: answer them after the wait returns, not as they appear.

## Rules

- Write only `reply`. Never change `status`, `body`, `quote`, `diff`, `sha`, `lines`, `id`, `created`, `reviews` or `approved`. The person resolves entries and approves commits in Diffsplainer.
- Keep every field you do not know, on every entry and at the top level.
- Do not add, delete or reorder entries.
- If the file is not valid JSON, do not write to it. Tell the person.
- Never push, rebase or squash. Those happen only after the person approves and says so.
