#!/usr/bin/env node
// PR review app: diff chunks on the right, Claude Code explanations aligned on the left.
const http = require('http');
const fs = require('fs');
const path = require('path');
const gh = require('./lib/github');
const { chunkFiles } = require('./lib/diff');
const { projectInstructions, relatedPulls } = require('./lib/context');
const { explainFile, explainOverview } = require('./lib/claude');

const PORT = +(process.env.PORT || 3000);
const CONCURRENCY = +(process.env.CLAUDE_CONCURRENCY || 3);
const MAX_PRS = 10;
const BATCH_CHUNKS = 12;
const BATCH_LINES = 400;
const PUBLIC = path.join(__dirname, 'public');
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' };

const prCache = new Map(); // "o/r/n" -> {data, at}
const explainCache = new Map(); // "o/r/n@sha:prs" -> [events]

async function loadPr(owner, repo, num) {
  const key = `${owner}/${repo}/${num}`;
  const hit = prCache.get(key);
  if (hit && Date.now() - hit.at < 60_000) return hit.data;
  const [pr, rawFiles] = await Promise.all([gh.getPull(owner, repo, num), gh.getPullFiles(owner, repo, num)]);
  const data = {
    pr: {
      number: pr.number,
      title: pr.title,
      body: pr.body || '',
      url: pr.html_url,
      state: pr.merged_at ? 'merged' : pr.state,
      draft: pr.draft,
      user: pr.user?.login,
      avatar: pr.user?.avatar_url,
      base: pr.base.ref,
      head: pr.head.ref,
      baseSha: pr.base.sha,
      headSha: pr.head.sha,
      additions: pr.additions,
      deletions: pr.deletions,
      changed_files: pr.changed_files,
    },
    files: chunkFiles(rawFiles),
    _raw: pr,
  };
  prCache.set(key, { data, at: Date.now() });
  return data;
}

function batches(file) {
  const out = [];
  let cur = [];
  let lines = 0;
  for (const c of file.chunks) {
    if (cur.length && (cur.length >= BATCH_CHUNKS || lines + c.lines.length > BATCH_LINES)) {
      out.push(cur);
      cur = [];
      lines = 0;
    }
    cur.push(c);
    lines += c.lines.length;
  }
  if (cur.length) out.push(cur);
  return out.map((chunks) => ({ ...file, chunks }));
}

async function pool(tasks, n) {
  let i = 0;
  await Promise.all(
    Array.from({ length: Math.min(n, tasks.length) }, async () => {
      while (i < tasks.length) await tasks[i++]();
    })
  );
}

async function streamExplain(req, res, owner, repo, num, prLimit) {
  res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', Connection: 'keep-alive' });
  let closed = false;
  req.on('close', () => (closed = true));
  const send = (event, data) => !closed && res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);

  try {
    const data = await loadPr(owner, repo, num);
    const key = `${owner}/${repo}/${num}@${data.pr.headSha}:${prLimit}`;
    if (explainCache.has(key)) {
      for (const [e, d] of explainCache.get(key)) send(e, d);
      send('done', {});
      return res.end();
    }
    const log = [];
    const emit = (e, d) => {
      log.push([e, d]);
      send(e, d);
    };

    const paths = data.files.map((f) => f.filename);
    send('status', { message: 'Loading project instructions and related PRs…' });
    const [instructions, related] = await Promise.all([
      projectInstructions(owner, repo, data.pr.baseSha, paths),
      relatedPulls(owner, repo, data._raw, paths, prLimit),
    ]);
    emit('context', { instructions: instructions.map((d) => d.path), related: related.map(({ body, ...r }) => r) });

    const ctx = { pr: data.pr, instructions, related, files: data.files };
    const work = data.files.filter((f) => f.chunks.length).flatMap(batches);
    let done = 0;
    const total = work.length + 1;
    send('status', { message: `Asking Claude Code to explain ${total - 1} file batch(es)…`, done, total });
    let failed = false;

    const tasks = [
      async () => {
        if (closed) return;
        try {
          emit('overview', await explainOverview(ctx, data.files));
        } catch (e) {
          failed = true;
          send('error', { scope: 'overview', message: e.message });
        }
        send('status', { done: ++done, total });
      },
      ...work.map((file) => async () => {
        if (closed) return;
        try {
          const out = await explainFile(ctx, file);
          const valid = new Set(file.chunks.map((c) => c.id));
          for (const c of out.chunks || []) if (valid.has(c.id)) emit('chunk', c);
        } catch (e) {
          failed = true;
          send('error', { scope: 'file', ids: file.chunks.map((c) => c.id), message: e.message });
        }
        send('status', { done: ++done, total });
      }),
    ];
    await pool(tasks, CONCURRENCY);
    if (!failed && !closed) explainCache.set(key, log);
    send('done', {});
  } catch (e) {
    send('error', { scope: 'fatal', message: e.message });
  }
  res.end();
}

function json(res, code, body) {
  res.writeHead(code, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(body));
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://x');
  const p = decodeURIComponent(url.pathname);

  let m = p.match(/^\/api\/pr\/([\w.-]+)\/([\w.-]+)\/(\d+)$/);
  if (m) {
    try {
      const { _raw, ...data } = await loadPr(m[1], m[2], +m[3]);
      return json(res, 200, data);
    } catch (e) {
      return json(res, e.status || 500, { error: e.message });
    }
  }
  m = p.match(/^\/api\/explain\/([\w.-]+)\/([\w.-]+)\/(\d+)$/);
  if (m) {
    const prs = Math.max(0, Math.min(MAX_PRS, +(url.searchParams.get('prs') ?? 3) || 0));
    return streamExplain(req, res, m[1], m[2], +m[3], prs);
  }

  // Static assets; any other path (e.g. /owner/repo/pull/123) gets the SPA shell.
  const file = path.join(PUBLIC, path.normalize(p).replace(/^(\.\.[/\\])+/, ''));
  if (file.startsWith(PUBLIC) && fs.existsSync(file) && fs.statSync(file).isFile()) {
    res.writeHead(200, { 'Content-Type': MIME[path.extname(file)] || 'application/octet-stream' });
    return fs.createReadStream(file).pipe(res);
  }
  res.writeHead(200, { 'Content-Type': 'text/html' });
  fs.createReadStream(path.join(PUBLIC, 'index.html')).pipe(res);
});

server.listen(PORT, () => {
  console.log(`PR review app on http://localhost:${PORT}`);
  console.log(`Open e.g. http://localhost:${PORT}/owner/repo/pull/123`);
});
