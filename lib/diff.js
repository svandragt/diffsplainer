// Parse GitHub per-file patches into logical chunks.
// A chunk is a diff hunk; long hunks are further split where they contain
// a run of unchanged lines, so each chunk covers one coherent change.

const LONG_HUNK = 30; // lines before we try to split a hunk
const GAP = 3; // run of context lines that separates two logical changes

function parseHunks(patch) {
  const hunks = [];
  let cur = null;
  let oldNo = 0;
  let newNo = 0;
  for (const line of patch.split('\n')) {
    const m = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$/);
    if (m) {
      oldNo = +m[1];
      newNo = +m[2];
      cur = { header: line, scope: m[3].trim(), lines: [] };
      hunks.push(cur);
      continue;
    }
    if (!cur) continue;
    if (line.startsWith('\\')) continue; // "\ No newline at end of file"
    const t = line[0];
    if (t === '+') cur.lines.push({ t: 'add', o: null, n: newNo++, s: line.slice(1) });
    else if (t === '-') cur.lines.push({ t: 'del', o: oldNo++, n: null, s: line.slice(1) });
    else cur.lines.push({ t: 'ctx', o: oldNo++, n: newNo++, s: line.slice(1) });
  }
  return hunks;
}

const MIN_BLOCK = 12; // min lines before a top-level declaration may start a new chunk

// True when `l` starts a new top-level block (unindented line after a blank line).
function startsBlock(prev, l) {
  return prev && prev.s.trim() === '' && l.s.trim() !== '' && !/^\s/.test(l.s) && !/^[)}\]]/.test(l.s);
}

function splitHunk(hunk) {
  if (hunk.lines.length <= LONG_HUNK) return [hunk.lines];
  const parts = [];
  let part = [];
  let ctxRun = [];
  for (const l of hunk.lines) {
    if (l.t === 'ctx') {
      ctxRun.push(l);
      continue;
    }
    // Long run of changes with no context (e.g. a new file): split on top-level blocks.
    if (!ctxRun.length && part.length >= MIN_BLOCK && startsBlock(part[part.length - 1], l)) {
      parts.push(part);
      part = [];
    }
    if (ctxRun.length >= GAP * 2 && part.some((x) => x.t !== 'ctx')) {
      // Close the current part with a little trailing context; start a new one with leading context.
      part.push(...ctxRun.slice(0, GAP));
      parts.push(part);
      part = ctxRun.slice(-GAP);
    } else {
      part.push(...ctxRun);
    }
    ctxRun = [];
    part.push(l);
  }
  part.push(...ctxRun);
  if (part.length) parts.push(part);
  return parts;
}

function toText(lines) {
  return lines.map((l) => (l.t === 'add' ? '+' : l.t === 'del' ? '-' : ' ') + l.s).join('\n');
}

function chunkFiles(files) {
  const out = [];
  files.forEach((f, fi) => {
    const file = {
      index: fi,
      filename: f.filename,
      previous: f.previous_filename || null,
      status: f.status,
      additions: f.additions,
      deletions: f.deletions,
      chunks: [],
    };
    if (f.patch) {
      let ci = 0;
      for (const h of parseHunks(f.patch)) {
        for (const lines of splitHunk(h)) {
          file.chunks.push({ id: `f${fi}c${ci++}`, scope: h.scope, lines });
        }
      }
    }
    out.push(file);
  });
  return out;
}

module.exports = { chunkFiles, toText };
