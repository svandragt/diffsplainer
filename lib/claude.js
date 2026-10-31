// Runs Claude Code headlessly (`claude -p`) with structured JSON output.
const { spawn } = require('child_process');
const os = require('os');
const { toText } = require('./diff');

const CLAUDE_BIN = process.env.CLAUDE_BIN || 'claude';
const MODEL = process.env.CLAUDE_MODEL || '';
const TIMEOUT_MS = +(process.env.CLAUDE_TIMEOUT_MS || 240000);

function runClaude(prompt, schema) {
  return new Promise((resolve, reject) => {
    const args = ['-p', '--tools', '', '--output-format', 'json', '--json-schema', JSON.stringify(schema)];
    if (MODEL) args.push('--model', MODEL);
    // Run outside this repo so Claude Code doesn't pick up our own CLAUDE.md.
    const child = spawn(CLAUDE_BIN, args, { cwd: os.tmpdir(), stdio: ['pipe', 'pipe', 'pipe'] });
    let out = '';
    let err = '';
    const timer = setTimeout(() => child.kill('SIGTERM'), TIMEOUT_MS);
    child.stdout.on('data', (d) => (out += d));
    child.stderr.on('data', (d) => (err += d));
    child.on('error', (e) => {
      clearTimeout(timer);
      reject(new Error(`Could not start Claude Code (${CLAUDE_BIN}): ${e.message}`));
    });
    child.on('close', (code) => {
      clearTimeout(timer);
      try {
        const res = JSON.parse(out);
        if (res.is_error) return reject(new Error(res.result || 'Claude Code returned an error'));
        resolve(res.structured_output || JSON.parse(res.result));
      } catch {
        reject(new Error(`Claude Code exited ${code}: ${(err || out).slice(0, 400)}`));
      }
    });
    child.stdin.end(prompt);
  });
}

function contextBlock(ctx) {
  const parts = [];
  parts.push(`# Pull request #${ctx.pr.number}: ${ctx.pr.title}\nAuthor: ${ctx.pr.user}\nBase: ${ctx.pr.base} ← Head: ${ctx.pr.head}\n\n${ctx.pr.body || '(no description)'}`);
  if (ctx.instructions.length) {
    parts.push(
      '# Project instructions\n' +
        ctx.instructions.map((d) => `<file path="${d.path}">\n${d.body}\n</file>`).join('\n')
    );
  }
  if (ctx.related.length) {
    parts.push(
      '# Previous merged PRs touching the same files (use only if relevant)\n' +
        ctx.related
          .map((p) => `## #${p.number} ${p.title} (merged ${p.merged_at?.slice(0, 10)})\nFiles: ${p.files.join(', ')}\n${p.body}`)
          .join('\n\n')
    );
  }
  parts.push(
    '# All files changed in this PR\n' +
      ctx.files.map((f) => `- ${f.filename} (${f.status}, +${f.additions}/-${f.deletions})`).join('\n')
  );
  return parts.join('\n\n');
}

const CHUNK_SCHEMA = {
  type: 'object',
  properties: {
    chunks: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: { type: 'string' },
          title: { type: 'string', description: 'Short headline (max ~8 words)' },
          what: { type: 'string', description: 'What the change does, 1-3 sentences' },
          why: { type: 'string', description: 'Why it is made / how it fits the PR, 1-3 sentences' },
          concern: { type: 'string', description: 'Optional reviewer note: risk, bug, or question. Empty if none.' },
        },
        required: ['id', 'title', 'what', 'why'],
      },
    },
  },
  required: ['chunks'],
};

const OVERVIEW_SCHEMA = {
  type: 'object',
  properties: {
    summary: { type: 'string', description: 'What the PR does overall, 2-4 sentences' },
    approach: { type: 'string', description: 'How the change is structured, 1-3 sentences' },
    review_focus: { type: 'array', items: { type: 'string' }, description: 'Up to 4 things a reviewer should check' },
  },
  required: ['summary', 'approach', 'review_focus'],
};

const SYSTEM = `You are helping a code reviewer understand a GitHub pull request.
Explain changes plainly and concretely for an engineer who has not seen this code.
Ground the "why" in the PR description, project instructions and previous PRs when they apply; never invent history.
Use \`backticks\` for identifiers. Be concise.`;

function explainFile(ctx, file) {
  const chunks = file.chunks
    .map((c) => `<chunk id="${c.id}"${c.scope ? ` scope="${c.scope.replace(/"/g, "'")}"` : ''}>\n${toText(c.lines)}\n</chunk>`)
    .join('\n');
  const prompt = `${SYSTEM}\n\n${contextBlock(ctx)}\n\n# File to explain: ${file.filename} (${file.status}${
    file.previous ? `, renamed from ${file.previous}` : ''
  })\nExplain each chunk below. Return one entry per chunk id, in order.\n\n${chunks}`;
  return runClaude(prompt, CHUNK_SCHEMA);
}

function explainOverview(ctx, files) {
  const sample = files
    .flatMap((f) => f.chunks.slice(0, 3).map((c) => `--- ${f.filename}\n${toText(c.lines).slice(0, 1500)}`))
    .join('\n')
    .slice(0, 30000);
  const prompt = `${SYSTEM}\n\n${contextBlock(ctx)}\n\n# Diff excerpt\n${sample}\n\nSummarise this pull request for the reviewer.`;
  return runClaude(prompt, OVERVIEW_SCHEMA);
}

module.exports = { explainFile, explainOverview };
