// Minimal GitHub REST client (no dependencies). Uses GITHUB_TOKEN / GH_TOKEN when present.
const API = process.env.GITHUB_API || 'https://api.github.com';
const TOKEN = process.env.GITHUB_TOKEN || process.env.GH_TOKEN || '';
let useToken = Boolean(TOKEN);

async function gh(path, { raw = false, allow404 = false } = {}) {
  const headers = {
    'User-Agent': 'fearful-owl-review',
    Accept: raw ? 'application/vnd.github.raw' : 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28',
  };
  if (useToken) headers.Authorization = `Bearer ${TOKEN}`;
  let res = await fetch(API + path, { headers });
  if (res.status === 401 && useToken) {
    // Bad token: fall back to anonymous access for public repos.
    useToken = false;
    delete headers.Authorization;
    res = await fetch(API + path, { headers });
  }
  if (allow404 && res.status === 404) return null;
  if (!res.ok) {
    const text = await res.text();
    const err = new Error(`GitHub ${res.status} on ${path}: ${text.slice(0, 200)}`);
    err.status = res.status;
    throw err;
  }
  return raw ? res.text() : res.json();
}

async function getPull(owner, repo, num) {
  return gh(`/repos/${owner}/${repo}/pulls/${num}`);
}

async function getPullFiles(owner, repo, num) {
  const files = [];
  for (let page = 1; page <= 10; page++) {
    const batch = await gh(`/repos/${owner}/${repo}/pulls/${num}/files?per_page=100&page=${page}`);
    files.push(...batch);
    if (batch.length < 100) break;
  }
  return files;
}

async function getFile(owner, repo, path, ref) {
  return gh(`/repos/${owner}/${repo}/contents/${encodeURI(path)}?ref=${encodeURIComponent(ref)}`, {
    raw: true,
    allow404: true,
  });
}

async function getTree(owner, repo, ref) {
  return gh(`/repos/${owner}/${repo}/git/trees/${encodeURIComponent(ref)}?recursive=1`, { allow404: true });
}

async function commitsForPath(owner, repo, path, ref, n = 3) {
  return gh(
    `/repos/${owner}/${repo}/commits?path=${encodeURIComponent(path)}&sha=${encodeURIComponent(ref)}&per_page=${n}`,
    { allow404: true }
  );
}

async function pullsForCommit(owner, repo, sha) {
  return gh(`/repos/${owner}/${repo}/commits/${sha}/pulls`, { allow404: true });
}

module.exports = { getPull, getPullFiles, getFile, getTree, commitsForPath, pullsForCommit };
