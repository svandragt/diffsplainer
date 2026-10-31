"""Parse GitHub per-file patches into logical chunks.

A chunk is a diff hunk. Long hunks are split further: at runs of unchanged
lines between edits, or (for long runs of pure additions/deletions, such as
new files) at top-level blocks. Each chunk then covers one coherent change.
"""

from __future__ import annotations

import re

LONG_HUNK = 30  # lines before we try to split a hunk
GAP = 3  # context lines kept either side of a split
MIN_BLOCK = 12  # min lines before a top-level declaration may start a new chunk

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$")


def parse_hunks(patch: str) -> list[dict]:
    hunks: list[dict] = []
    cur = None
    old_no = new_no = 0
    for line in patch.split("\n"):
        m = HUNK_RE.match(line)
        if m:
            old_no, new_no = int(m[1]), int(m[2])
            cur = {"scope": m[3].strip(), "lines": []}
            hunks.append(cur)
            continue
        if cur is None or line.startswith("\\"):  # "\ No newline at end of file"
            continue
        t, s = line[:1], line[1:]
        if t == "+":
            cur["lines"].append({"t": "add", "o": None, "n": new_no, "s": s})
            new_no += 1
        elif t == "-":
            cur["lines"].append({"t": "del", "o": old_no, "n": None, "s": s})
            old_no += 1
        else:
            cur["lines"].append({"t": "ctx", "o": old_no, "n": new_no, "s": s})
            old_no += 1
            new_no += 1
    return hunks


def _starts_block(prev: dict | None, line: dict) -> bool:
    """True when `line` starts a new top-level block (unindented, after a blank line)."""
    s = line["s"]
    return (
        prev is not None
        and prev["s"].strip() == ""
        and s.strip() != ""
        and not s[0].isspace()
        and s[0] not in ")}]"
    )


def split_hunk(lines: list[dict]) -> list[list[dict]]:
    if len(lines) <= LONG_HUNK:
        return [lines]
    parts: list[list[dict]] = []
    part: list[dict] = []
    ctx_run: list[dict] = []
    for line in lines:
        if line["t"] == "ctx":
            ctx_run.append(line)
            continue
        if not ctx_run and len(part) >= MIN_BLOCK and _starts_block(part[-1], line):
            parts.append(part)
            part = []
        if len(ctx_run) >= GAP * 2 and any(x["t"] != "ctx" for x in part):
            # Close the current part with trailing context; start the next with leading context.
            part.extend(ctx_run[:GAP])
            parts.append(part)
            part = ctx_run[-GAP:]
        else:
            part.extend(ctx_run)
        ctx_run = []
        part.append(line)
    part.extend(ctx_run)
    if part:
        parts.append(part)
    return parts


def to_text(lines: list[dict]) -> str:
    mark = {"add": "+", "del": "-", "ctx": " "}
    return "\n".join(mark[l["t"]] + l["s"] for l in lines)


def chunk_files(files: list[dict]) -> list[dict]:
    out = []
    for fi, f in enumerate(files):
        chunks = []
        if f.get("patch"):
            ci = 0
            for h in parse_hunks(f["patch"]):
                for lines in split_hunk(h["lines"]):
                    chunks.append({"id": f"f{fi}c{ci}", "scope": h["scope"], "lines": lines})
                    ci += 1
        out.append(
            {
                "index": fi,
                "filename": f["filename"],
                "previous": f.get("previous_filename"),
                "status": f.get("status"),
                "additions": f.get("additions", 0),
                "deletions": f.get("deletions", 0),
                "chunks": chunks,
            }
        )
    return out
