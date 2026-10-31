// Gathers review context: project instructions and a limited set of related previous PRs.
const gh = require('./github');

const ROOT_DOCS = ['CLAUDE.md', 'AGENTS.md', '.github/copilot-instructions.md', 'CONTRIBUTING.md', 'README.md'];
const NESTED_DOCS = new Set(['CLAUDE.md', 'AGENTS.md']);
const MAX_DOC_CHARS = 6000;
const MAX_TOTAL_DOC_CHARS = 20000;

const clip = (s, n) => (s && s.length > n ? s.slice(0, n) + '\n…[truncated]' : s || '');

async function projectInstructions(owner, repo, ref, changedPaths) {
  const wanted = [...ROOT_DOCS];
  // Nested CLAUDE.md / AGENTS.md in directories that contain changed files.
  const tree = await gh.getTree(owner, repo, ref).catch(() => null);
  if (tree && tree.tree) {
    const dirs = new Set();
    for (const p of changedPaths) {
      const parts = p.split('/');
      for (let i = 1; i < parts.length; i++) dirs.add(parts.slice(0, i).join('/'));
    }
    for (const node of tree.tree) {
      const base = node.path.split('/').pop();
      const dir = node.path.split('/').slice(0, -1).join('/');
      if (node.type === 'blob' && NESTED_DOCS.has(base) && dirs.has(dir)) wanted.push(node.path);
    }
  }
  const docs = [];
  let total = 0;
  for (const path of wanted) {
    if (total >= MAX_TOTAL_DOC_CHARS) break;
    const text = await gh.getFile(owner, repo, path, ref).catch(() => null);
    if (!text) continue;
    // README is only a fallback when there are no real instruction files.
    if (path === 'README.md' && docs.length) continue;
    const body = clip(text, Math.min(MAX_DOC_CHARS, MAX_TOTAL_DOC_CHARS - total));
    total += body.length;
    docs.push({ path, body });
  }
  return docs;
}

async function relatedPulls(owner, repo, pr, changedPaths, limit) {
  if (!limit) return [];
  const paths = changedPaths.slice(0, 8);
  const shaHits = new Map(); // sha -> set of paths
  await Promise.all(
    paths.map(async (p) => {
      const commits = await gh.commitsForPath(owner, repo, p, pr.base.sha, 3).catch(() => null);
      for (const c of commits || []) {
        if (!shaHits.has(c.sha)) shaHits.set(c.sha, new Set());
        shaHits.get(c.sha).add(p);
      }
    })
  );
  const prs = new Map(); // number -> {pr, files}
  const shas = [...shaHits.keys()].slice(0, 12);
  await Promise.all(
    shas.map(async (sha) => {
      const list = await gh.pullsForCommit(owner, repo, sha).catch(() => null);
      for (const p of list || []) {
        if (p.number === pr.number || !p.merged_at) continue;
        const entry = prs.get(p.number) || { pr: p, files: new Set() };
        shaHits.get(sha).forEach((f) => entry.files.add(f));
        prs.set(p.number, entry);
      }
    })
  );
  return [...prs.values()]
    .sort((a, b) => b.files.size - a.files.size || new Date(b.pr.merged_at) - new Date(a.pr.merged_at))
    .slice(0, limit)
    .map(({ pr: p, files }) => ({
      number: p.number,
      title: p.title,
      url: p.html_url,
      merged_at: p.merged_at,
      files: [...files],
      body: clip(p.body, 1200),
    }));
}

module.exports = { projectInstructions, relatedPulls };
