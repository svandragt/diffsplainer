// Front-end: reads the PR from the address bar, renders aligned chunk rows, streams explanations.
const $ = (s, el = document) => el.querySelector(s);
const app = $('#app');
const urlInput = $('#url');
const prsInput = $('#prs');
let source = null;

$('.host').textContent = location.host;

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
// Tiny, safe markdown: `code` and **bold** only.
const md = (s) => esc(s).replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>');

function parsePr(text) {
  const m = String(text).match(/([\w.-]+)\/([\w.-]+)\/pull\/(\d+)/);
  return m ? { owner: m[1], repo: m[2], num: m[3] } : null;
}

function fromLocation() {
  const pr = parsePr(location.pathname);
  const prs = new URLSearchParams(location.search).get('prs');
  if (prs !== null) prsInput.value = prs;
  if (!pr) return;
  const canonical = `/${pr.owner}/${pr.repo}/pull/${pr.num}${location.search}`;
  if (location.pathname + location.search !== canonical) history.replaceState(null, '', canonical);
  urlInput.value = `https://github.com/${pr.owner}/${pr.repo}/pull/${pr.num}`;
  load(pr);
}

$('#load').addEventListener('submit', (e) => {
  e.preventDefault();
  const pr = parsePr(urlInput.value);
  if (!pr) return alert('Enter a GitHub pull request URL, e.g. https://github.com/owner/repo/pull/123');
  const qs = prsInput.value !== '3' ? `?prs=${prsInput.value}` : '';
  history.pushState(null, '', `/${pr.owner}/${pr.repo}/pull/${pr.num}${qs}`);
  fromLocation();
});
window.addEventListener('popstate', fromLocation);

async function load({ owner, repo, num }) {
  if (source) source.close();
  app.innerHTML = '<div class="card">Loading pull request…</div>';
  const res = await fetch(`/api/pr/${owner}/${repo}/${num}`);
  const data = await res.json();
  if (!res.ok) {
    app.innerHTML = `<div class="card">Could not load PR: ${esc(data.error)}</div>`;
    return;
  }
  document.title = `#${data.pr.number} ${data.pr.title} · Fearful Owl`;
  render(data);
  explain(owner, repo, num);
}

function render({ pr, files }) {
  const state = pr.draft ? 'draft' : pr.state;
  app.innerHTML = `
    <section class="card pr-head">
      <h1>${esc(pr.title)} <a href="${esc(pr.url)}" target="_blank" rel="noopener">#${pr.number}</a></h1>
      <div class="meta">
        <span class="pill ${state}">${state}</span>
        ${pr.avatar ? `<img src="${esc(pr.avatar)}" alt="">` : ''}<b>${esc(pr.user)}</b>
        wants to merge <code>${esc(pr.head)}</code> into <code>${esc(pr.base)}</code>
        · ${pr.changed_files} files <span class="plus">+${pr.additions}</span> <span class="minus">−${pr.deletions}</span>
      </div>
      <div class="status" id="status">Starting…</div>
      <div class="progress"><div id="bar"></div></div>
    </section>
    <div class="split">
      <section class="card overview" id="overview"><h2>Overview</h2><div class="skeleton"></div><div class="skeleton short"></div></section>
      <section class="card context" id="context"><h2>Context given to Claude</h2><div class="skeleton short"></div></section>
    </div>
    ${files.map(renderFile).join('')}`;
}

function renderFile(f) {
  const rename = f.previous ? `<span class="tag">renamed from ${esc(f.previous)}</span>` : '';
  const head = `<div class="file-head"><span class="tag">${f.status}</span><span class="name">${esc(f.filename)}</span>${rename}
    <span class="stat"><span class="plus">+${f.additions}</span> <span class="minus">−${f.deletions}</span></span></div>`;
  const rows = f.chunks.length
    ? f.chunks.map(renderChunk).join('')
    : `<div class="row"><div class="nodiff">No textual diff (binary, too large, or rename only).</div></div>`;
  return `<section class="file">${head}${rows}</section>`;
}

function renderChunk(c) {
  const body = c.lines
    .map((l) => {
      const m = l.t === 'add' ? '+' : l.t === 'del' ? '-' : ' ';
      return `<tr class="${l.t}"><td class="n">${l.o ?? ''}</td><td class="n">${l.n ?? ''}</td><td class="s" data-m="${m}">${esc(l.s)}</td></tr>`;
    })
    .join('');
  const scope = c.scope ? `<tr class="scope"><td colspan="3">${esc(c.scope)}</td></tr>` : '';
  return `<div class="row" id="${c.id}">
    <aside class="explain"><div class="inner"><div class="skeleton"></div><div class="skeleton"></div><div class="skeleton short"></div></div></aside>
    <div class="diff"><table>${scope}${body}</table></div>
  </div>`;
}

function explain(owner, repo, num) {
  source = new EventSource(`/api/explain/${owner}/${repo}/${num}?prs=${encodeURIComponent(prsInput.value || 0)}`);
  const status = (msg) => msg && ($('#status').textContent = msg);

  source.addEventListener('status', (e) => {
    const d = JSON.parse(e.data);
    status(d.message);
    if (d.total) {
      $('#bar').style.width = `${(100 * d.done) / d.total}%`;
      if (!d.message) status(`Explained ${d.done} of ${d.total} batches…`);
    }
  });

  source.addEventListener('context', (e) => {
    const { instructions, related } = JSON.parse(e.data);
    $('#context').innerHTML = `<h2>Context given to Claude</h2>
      <div><b>Project instructions:</b> ${instructions.length ? instructions.map((p) => `<code>${esc(p)}</code>`).join(' ') : 'none found'}</div>
      <div><b>Previous PRs:</b> ${related.length ? '' : 'none'}</div>
      ${related.length ? `<ul>${related.map((p) => `<li><a href="${esc(p.url)}" target="_blank" rel="noopener">#${p.number}</a> ${esc(p.title)}</li>`).join('')}</ul>` : ''}`;
  });

  source.addEventListener('overview', (e) => {
    const o = JSON.parse(e.data);
    $('#overview').innerHTML = `<h2>Overview</h2><p>${md(o.summary)}</p><p>${md(o.approach)}</p>
      ${o.review_focus?.length ? `<b>Review focus</b><ul>${o.review_focus.map((x) => `<li>${md(x)}</li>`).join('')}</ul>` : ''}`;
  });

  source.addEventListener('chunk', (e) => {
    const c = JSON.parse(e.data);
    const el = document.querySelector(`#${c.id} .inner`);
    if (!el) return;
    el.innerHTML = `<h3>${md(c.title)}</h3>
      <p><span class="lbl">What</span>${md(c.what)}</p>
      <p><span class="lbl">Why</span>${md(c.why)}</p>
      ${c.concern ? `<p class="concern"><span class="lbl">Check</span>${md(c.concern)}</p>` : ''}`;
  });

  source.addEventListener('error', (e) => {
    if (!e.data) return; // connection-level error; 'done' or server close handles it
    const d = JSON.parse(e.data);
    if (d.scope === 'overview') $('#overview').innerHTML = `<h2>Overview</h2><p class="err">${esc(d.message)}</p>`;
    else if (d.scope === 'file') d.ids.forEach((id) => {
      const el = document.querySelector(`#${id} .inner`);
      if (el) el.innerHTML = `<p class="err">Explanation failed: ${esc(d.message)}</p>`;
    });
    else {
      source.close(); // stop EventSource auto-reconnecting into the same failure
      status(`Error: ${d.message}`);
    }
  });

  source.addEventListener('done', () => {
    source.close();
    status('Done.');
    $('#bar').style.width = '100%';
    document.querySelectorAll('.explain .inner').forEach((el) => {
      if (el.querySelector('.skeleton')) el.innerHTML = '<p class="err">No explanation returned.</p>';
    });
  });
}

fromLocation();
