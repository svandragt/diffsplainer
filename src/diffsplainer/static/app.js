// Front-end: reads the PR from the address bar, renders aligned chunk rows, streams explanations.
const $ = (s, el = document) => el.querySelector(s);
const app = $('#app');
const urlInput = $('#url');
let source = null;
let loadSeq = 0;
let session = null; // local review (`diffsplainer --local`) state; null when a PR is shown
const wrapBox = $('#wrap');
let wrapOn = true;
try { wrapOn = localStorage.getItem('wrap') !== '0'; } catch {}
wrapBox.checked = wrapOn;
document.body.classList.toggle('wrap', wrapOn);
wrapBox.addEventListener('change', () => {
  document.body.classList.toggle('wrap', wrapBox.checked);
  try { localStorage.setItem('wrap', wrapBox.checked ? '1' : '0'); } catch {}
});

$('.host').textContent = location.host;
// Opens the current GitHub PR (github.com or Enterprise) in this app; built from location.origin so it follows any host/port.
const bookmarklet = $('#bookmarklet');
bookmarklet.href = 'javascript:' + encodeURIComponent('(()=>{const m=location.pathname.match(/^\\/([^/]+)\\/([^/]+)\\/pull\\/(\\d+)/);if(!m){alert(\'Open a GitHub pull request first\');return}window.open(ORIGIN+\'/\'+m[1]+\'/\'+m[2]+\'/pull/\'+m[3],\'_blank\')})()'.replace('ORIGIN', JSON.stringify(location.origin)));
app.addEventListener('click', (e) => {
  const link = e.target.closest('#bookmarklet');
  if (!link) return;
  e.preventDefault();
  if (!link.nextElementSibling) link.insertAdjacentHTML('afterend', ' <em class="hint">(drag it, don\'t click)</em>');
});
const emptyHtml = app.innerHTML;

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
// Tiny, safe markdown: `code` and **bold** only.
const md = (s) => esc(s).replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>');

function parsePr(text) {
  const m = String(text).match(/([\w.-]+)\/([\w.-]+)\/pull\/(\d+)/);
  return m ? { owner: m[1], repo: m[2], num: m[3] } : null;
}

// Replaces the page content. The observer lets go of the old diffs first, so it does not keep their tables in memory.
function setApp(html) {
  lazy?.disconnect();
  app.innerHTML = html;
  app.hidden = false;
}

function fromLocation() {
  closePop();
  const pr = parsePr(location.pathname);
  if (!pr) {
    loadSeq++;
    if (source) source.close();
    source = null;
    session = null;
    urlInput.value = '';
    document.title = 'Diffsplainer · PR review';
    app.hidden = true; // until loadLocal knows whether this is the start page or a local review
    loadLocal(loadSeq);
    return;
  }
  const canonical = `/${pr.owner}/${pr.repo}/pull/${pr.num}${location.search}`;
  if (location.pathname + location.search !== canonical) history.replaceState(null, '', canonical);
  urlInput.value = `https://github.com/${pr.owner}/${pr.repo}/pull/${pr.num}`;
  load(pr);
}

$('#load').addEventListener('submit', (e) => {
  e.preventDefault();
  const pr = parsePr(urlInput.value);
  if (!pr) return alert('Enter a GitHub pull request URL, e.g. https://github.com/owner/repo/pull/123');
  history.pushState(null, '', `/${pr.owner}/${pr.repo}/pull/${pr.num}${location.search}`);
  fromLocation();
});
window.addEventListener('popstate', fromLocation);

async function load({ owner, repo, num }) {
  const seq = ++loadSeq;
  if (source) source.close();
  source = null;
  session = null;
  setApp('<div class="card">Loading pull request…</div>');
  const res = await fetch(`/api/pr/${owner}/${repo}/${num}`);
  const data = await res.json();
  if (seq !== loadSeq) return;
  if (!res.ok) {
    setApp(`<div class="card">Could not load PR: ${esc(data.error)}</div>`);
    return;
  }
  document.title = `#${data.pr.number} ${data.pr.title} · Diffsplainer`;
  render(data);
  const prs = new URLSearchParams(location.search).get('prs');
  explain(`/api/explain/${owner}/${repo}/${num}${prs === null ? '' : `?prs=${encodeURIComponent(prs)}`}`);
}

const skeletonCards = `<div class="split">
      <section class="card overview" id="overview"><h2>Overview</h2><div class="skeleton"></div><div class="skeleton short"></div></section>
      <section class="card context" id="context"><h2>Context given to Claude</h2><div class="skeleton short"></div></section>
    </div>`;

function render({ pr, files }) {
  const state = pr.draft ? 'draft' : pr.state;
  setApp(`
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
    ${skeletonCards}
    ${files.map(renderFile).join('')}`);
  if (lazy) document.querySelectorAll('.diff[data-lang]').forEach((el) => lazy.observe(el));
}

// Prism language per file extension; other files stay plain. The grammars are in vendor/prism.js.
const LANGS = {
  go: 'go', py: 'python', php: 'php', c: 'c', h: 'c', cc: 'cpp', cpp: 'cpp', cxx: 'cpp', hpp: 'cpp', hh: 'cpp',
  css: 'css', scss: 'scss', js: 'javascript', mjs: 'javascript', cjs: 'javascript', jsx: 'jsx', ts: 'typescript',
  tsx: 'tsx', html: 'markup', htm: 'markup', xml: 'markup', svg: 'markup', gd: 'gdscript', json: 'json',
  yml: 'yaml', yaml: 'yaml', sh: 'bash', bash: 'bash', md: 'markdown', markdown: 'markdown', rs: 'rust', java: 'java', rb: 'ruby',
  sql: 'sql', toml: 'toml',
  cs: 'csharp', kt: 'kotlin', kts: 'kotlin', swift: 'swift', dart: 'dart', lua: 'lua', ini: 'ini', cfg: 'ini',
  env: 'ini', editorconfig: 'ini', tf: 'hcl', hcl: 'hcl', graphql: 'graphql', gql: 'graphql', ps1: 'powershell',
  psm1: 'powershell', less: 'less', zsh: 'bash', jsonc: 'json', json5: 'json', phtml: 'php', vue: 'markup',
  svelte: 'markup',
};
// Files named by convention rather than extension; checked on the base name before LANGS.
const NAME_LANGS = [[/^Dockerfile(\.|$)/, 'docker'], [/^(Makefile|GNUmakefile)$|\.mk$/, 'makefile'], [/^\.env\./, 'ini']];
const langOf = (name) => {
  const byName = NAME_LANGS.find(([re]) => re.test(name.split('/').pop()));
  if (byName) return byName[1];
  const ext = name.match(/\.([^./]+)$/)?.[1].toLowerCase();
  return Object.hasOwn(LANGS, ext) ? LANGS[ext] : '';
};
const MAX_HIGHLIGHT_CHARS = 1000; // longer lines (minified code) stay plain: Prism's regexes slow down on them
const MAX_HIGHLIGHT_ROWS = 1500; // chunks with more rows (a lockfile or generated file) stay plain: they would block the page

// Highlights one line at a time, so a multi-line comment or string is coloured line by line as code.
// Prism.highlight escapes its input, so raw diff text never reaches innerHTML.
function highlight(diff) {
  const lang = diff.dataset.lang;
  const grammar = Prism.languages[lang];
  const cells = diff.querySelectorAll('td.s');
  if (!grammar || cells.length > MAX_HIGHLIGHT_ROWS) return;
  cells.forEach((td) => {
    const line = td.textContent;
    if (line && line.length <= MAX_HIGHLIGHT_CHARS) td.innerHTML = Prism.highlight(line, grammar, lang);
  });
}

// Chunks are highlighted when they near the viewport, so a long PR costs nothing up front.
// Without Prism (its script failed to load) there is no observer and the code stays plain.
const lazy = typeof Prism === 'undefined' ? null : new IntersectionObserver((entries) => entries.forEach((e) => {
  if (!e.isIntersecting) return;
  lazy.unobserve(e.target);
  highlight(e.target);
}), { rootMargin: '200px' });

// A generated file (a lockfile, say) is not explained: it starts collapsed, with a note, and its chunks get no skeleton.
function renderFile(f) {
  const rename = f.previous ? `<span class="tag">renamed from ${esc(f.previous)}</span>` : '';
  const note = f.generated ? '<span class="note">Generated file, not explained.</span>' : '';
  const head = `<span class="tag">${f.status}</span><span class="name">${esc(f.filename)}</span>${rename}${note}
    <span class="stat"><span class="plus">+${f.additions}</span> <span class="minus">−${f.deletions}</span></span>`;
  const rows = f.chunks.length
    ? f.chunks.map((c) => renderChunk(c, langOf(f.filename), f.generated)).join('')
    : `<div class="row"><div class="nodiff">No textual diff (binary, too large, or rename only).</div></div>`;
  return f.generated
    ? `<details class="file generated"><summary class="file-head">${head}</summary>${rows}</details>`
    : `<section class="file"><div class="file-head">${head}</div>${rows}</section>`;
}

const mark = (t) => (t === 'add' ? '+' : t === 'del' ? '-' : ' ');

function renderChunk(c, lang, generated) {
  const body = c.lines
    .map((l) => {
      const m = mark(l.t);
      return `<tr class="${l.t}"><td class="n">${l.o ?? ''}</td><td class="n">${l.n ?? ''}</td><td class="s" data-m="${m}">${esc(l.s)}</td></tr>`;
    })
    .join('');
  const scope = c.scope ? `<tr class="scope"><td colspan="3">${esc(c.scope)}</td></tr>` : '';
  const tools = session ? `<div class="chunk-tools"><div class="chunk-bar"><code class="sha">${c.sha ? esc(c.sha.slice(0, 7)) : 'unknown commit'}</code>
    <button type="button" class="btn req">Request changes</button></div></div>` : '';
  const inner = generated ? '' : '<div class="inner"><div class="skeleton"></div><div class="skeleton"></div><div class="skeleton short"></div></div>';
  const aside = generated && !tools ? '' : `<aside class="explain">${tools}${inner}</aside>`;
  return `<div class="row" id="${c.id}">
    ${aside}
    <div class="diff"${lang ? ` data-lang="${lang}"` : ''}><table>${scope}${body}</table></div>
  </div>`;
}

function explain(url) {
  source = new EventSource(url);
  const status = (msg) => msg && ($('#status').textContent = msg);

  source.addEventListener('status', (e) => {
    const d = JSON.parse(e.data);
    status(d.message);
    if (d.total) {
      $('#bar').style.width = `${(100 * d.done) / d.total}%`;
      if (!d.message) status(`Explained ${d.done} of ${d.total} batches…`);
    }
  });

  let hasPlan = false; // the checks in the overview are against the plan, or else against the agent's summary
  source.addEventListener('context', (e) => {
    const { instructions, related, plan } = JSON.parse(e.data);
    hasPlan = !!plan;
    $('#context').innerHTML = `<h2>Context given to Claude</h2>
      <div><b>Project instructions:</b> ${instructions.length ? instructions.map((p) => `<code>${esc(p)}</code>`).join(' ') : 'none found'}</div>
      ${session ? `<div><b>Plan:</b> ${plan ? `<code>${esc(plan)}</code>` : 'none'}</div>` : ''}
      <div><b>Previous PRs:</b> ${related.length ? '' : 'none'}</div>
      ${related.length ? `<ul>${related.map((p) => `<li><a href="${esc(p.url)}" target="_blank" rel="noopener">#${p.number}</a> ${esc(p.title)}</li>`).join('')}</ul>` : ''}`;
  });

  source.addEventListener('overview', (e) => {
    const o = JSON.parse(e.data);
    $('#overview').innerHTML = `<h2>Overview</h2><p>${md(o.summary)}</p><p>${md(o.approach)}</p>
      ${o.review_focus?.length ? `<b>Review focus</b><ul>${o.review_focus.map((x) => `<li>${md(x)}</li>`).join('')}</ul>` : ''}
      ${Array.isArray(o.checks) ? `<b>${hasPlan ? 'Against the plan' : "Against the agent's summary"}</b>${o.checks.length ? `<ul>${o.checks.map((x) => `<li>${md(x)}</li>`).join('')}</ul>` : '<p class="none">No mismatches found.</p>'}` : ''}`;
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
      status(d.scope === 'limit' ? d.message : `Error: ${d.message}`);
      $('#status').classList.toggle('warn', d.scope === 'limit');
      $('.progress').hidden = true;
      document.querySelectorAll('.skeleton').forEach((el) => el.remove());
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

// Explain a term: selecting text in an explanation offers a button, then a popup with the answer.
// One floating element (button, notice or popup) exists at a time.
let pop = null;
let popAbort = null;
let popOpener = null;
const make = (html) => {
  const t = document.createElement('template');
  t.innerHTML = html;
  return t.content.firstElementChild;
};

// Just below the end of the selection (document coordinates), kept inside the viewport.
function place(el, a) {
  const gap = 8;
  const maxLeft = scrollX + document.documentElement.clientWidth - el.offsetWidth - gap;
  el.style.left = `${Math.max(scrollX + gap, Math.min(a.x, maxLeft))}px`;
  let top = a.bottom + 6;
  if (top + el.offsetHeight > scrollY + innerHeight - gap) top = Math.max(scrollY + gap, a.top - el.offsetHeight - 6);
  el.style.top = `${top}px`;
}

function mount(el, a) {
  pop?.remove();
  pop = el;
  document.body.append(el);
  place(el, a);
}

function closePop() {
  if (!pop) return;
  popAbort?.abort();
  popAbort = null;
  const hadFocus = pop.contains(document.activeElement);
  pop.remove();
  pop = null;
  if (hadFocus) popOpener?.focus();
}

async function askTerm({ text, around, chunk }, a) {
  const dlg = make(`<div class="term-float term-pop" role="dialog" aria-label="Explanation of the selected text" aria-live="polite" tabindex="-1">
    <div class="skeleton"></div><div class="skeleton"></div><div class="skeleton short"></div></div>`);
  mount(dlg, a);
  dlg.focus({ preventScroll: true });
  const ctl = (popAbort = new AbortController());
  const q = new URLSearchParams({ text, around });
  if (chunk) q.set('chunk', chunk);
  if (session) for (const k of ['base', 'head']) q.set(k, session.info[k]); // the chunk id is in the range this page shows
  try {
    const pr = parsePr(location.pathname);
    const res = await fetch(`${pr ? `/api/explain-term/${pr.owner}/${pr.repo}/${pr.num}` : '/api/local/explain-term'}?${q}`, { signal: ctl.signal });
    const d = await res.json();
    dlg.innerHTML = res.ok
      ? `<p><span class="lbl">Meaning</span>${md(d.meaning)}</p><p><span class="lbl">Here</span>${md(d.here)}</p>`
      : `<p class="err">${esc(d.error || `Error ${res.status}`)}</p>`;
  } catch (err) {
    if (err.name === 'AbortError') return;
    dlg.innerHTML = `<p class="err">${esc(err.message)}</p>`;
  }
  place(dlg, a);
}

function onSelect(e) {
  if (e.type === 'keyup' && !(e.shiftKey || e.key === 'Shift')) return; // only keys that extend a selection
  if (e.type === 'keyup' && (e.target.matches?.('input, textarea, select') || e.target.isContentEditable)) return; // typing is not selecting
  closePop();
  popOpener = document.activeElement === document.body ? null : document.activeElement;
  const sel = getSelection();
  const text = sel.toString().trim();
  if (!text) return;
  const range = sel.getRangeAt(0);
  const node = range.commonAncestorContainer;
  const inner = node.nodeType === 1 ? node : node.parentElement;
  // a range that starts and ends in different hosts has a common ancestor outside both; a local review also takes comments on diffs
  const host = inner?.closest(session ? '.explain, #overview, .diff' : '.explain, #overview');
  if (!host || inner.closest('.chunk-tools')) return;
  const rects = range.getClientRects();
  const end = rects[rects.length - 1] || range.getBoundingClientRect();
  const a = { x: end.right + scrollX, top: end.top + scrollY, bottom: end.bottom + scrollY };
  const row = session && host.closest('.row'); // a chunk of a local review takes comments
  const explainable = text.length <= 300;
  if (!explainable && !row) return mount(make('<div class="term-float term-note" role="status">Select less text</div>'), a);
  const group = make('<div class="term-float term-btns"></div>');
  group.addEventListener('mousedown', (e) => e.preventDefault()); // keep the selection
  if (explainable) {
    const btn = make('<button class="term-btn">Explain</button>');
    const around = (inner.closest('p, td.s') || host).textContent.replace(/\s+/g, ' ').trim().slice(0, 4000); // the server rejects more
    btn.addEventListener('click', () => askTerm({ text, around, chunk: host.closest('.row')?.id }, a));
    group.append(btn);
  }
  if (row) {
    const btn = make('<button class="term-btn">Comment</button>');
    btn.addEventListener('click', () => askComment(text, row.id, a));
    group.append(btn);
  }
  mount(group, a);
}

// Deferred so a click that clears the selection is seen as empty. The popup check runs first:
// by the time the deferred call runs, a clicked button has already been replaced by the popup.
const onUp = (e) => pop?.contains(e.target) || setTimeout(onSelect, 0, e);
document.addEventListener('mouseup', onUp);
document.addEventListener('keyup', onUp);
document.addEventListener('mousedown', (e) => {
  if (!pop || pop.contains(e.target)) return;
  closePop();
  window.getSelection().removeAllRanges(); // else the mouseup that follows reopens the Explain button
});
document.addEventListener('keydown', (e) => e.key === 'Escape' && closePop());

// Local review (`diffsplainer --local`): the unpushed commits, and the decisions that go into the review file the agent reads.
// Text from that file (the agent writes `reply`) is only ever set with textContent, never through innerHTML.
const h = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
};
const button = (label, cls = '') => Object.assign(h('button', `btn ${cls}`, label), { type: 'button' });
const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
const NO_SHA = 'Git could not tell which commit wrote this chunk, so it cannot go into the review.';

// `/` is the start page in PR mode; with `--local` it is the commit review (`/api/local` answers 404 otherwise).
async function loadLocal(seq, recorded = '') {
  const res = await fetch('/api/local').catch(() => null);
  if (!res || res.status === 404) return seq === loadSeq && setApp(emptyHtml);
  const data = await res.json().catch(() => ({ error: `Error ${res.status}` })); // #app stays hidden until a setApp
  if (seq !== loadSeq) return;
  document.title = `${data.local?.summary?.title ?? 'Unpushed commits'} · Diffsplainer`;
  if (!res.ok) return setApp(`<div class="card">Could not load the commits: ${esc(data.error)}</div>`);
  renderLocal(data, recorded);
}

// The top of the page: the repository and branch, then the agent's summary of the work. Without a summary, the commit count.
function renderAbout(info, count) {
  const s = info.summary;
  if (!s) {
    const heading = h('h1', null, `${count} on `);
    heading.append(h('code', null, info.branch));
    return [heading];
  }
  const where = h('div', 'meta where');
  where.append(h('code', null, info.repo), h('code', null, info.branch));
  return [where, h('div', 'lbl', "Agent's summary"), h('h1', null, s.title ?? count), s.ticket && h('div', 'meta', s.ticket), s.body && h('p', 'summary-body', s.body)].filter(Boolean);
}

function renderLocal({ local: info, files, review }, recorded) {
  // draft: the general comment typed and not yet sent; recorded: only for the empty range it was shown for, because new commits need the controls
  session = { info, review, chunks: new Map(), recorded: info.commits.length ? '' : recorded, draft: '' };
  files.forEach((f) => f.chunks.forEach((c) => session.chunks.set(c.id, { file: f.filename, c })));
  const n = info.commits.length;
  const sum = (key) => files.reduce((t, f) => t + f[key], 0);
  const count = n ? plural(n, 'unpushed commit') : 'No unpushed commits';
  const stats = n ? `${plural(files.length, 'file')} <span class="plus">+${sum('additions')}</span> <span class="minus">−${sum('deletions')}</span>` : '';
  const line = [info.summary && count, stats].filter(Boolean).join(' · ');
  setApp(`
    <section class="card pr-head">
      <div id="about"></div>
      ${line ? `<div class="meta">${line}</div>` : ''}
      ${n ? `<div class="status" id="status">Starting…</div>
      <div class="progress"><div id="bar"></div></div>` : ''}
    </section>
    ${n ? '<section class="card commits" id="commits"></section>' : ''}
    <section class="card review" id="review"></section>
    ${n ? skeletonCards + files.map(renderFile).join('') : ''}`);
  $('#about').replaceChildren(...renderAbout(info, count));
  renderCommits();
  renderReview();
  if (!n) return;
  if (lazy) document.querySelectorAll('.diff[data-lang]').forEach((el) => lazy.observe(el));
  const q = new URLSearchParams({ base: info.base, head: info.head }); // the range this page shows: chunk ids belong to it
  const prs = new URLSearchParams(location.search).get('prs');
  if (prs !== null) q.set('prs', prs);
  explain(`/api/local/explain?${q}`);
}

// Sends one review request. The answer is the new review data, which redraws the commits and the panel.
async function postReview(body) {
  const res = await fetch('/api/review', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const d = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(d.error || `Error ${res.status}`);
  session.review = d.review;
  renderCommits();
  renderReview();
  return d.review;
}

// A button's request. A refused one puts the server's message in `err`, next to the button.
async function act(body, err, after) {
  try {
    await postReview(body);
    after?.();
  } catch (e) { err.textContent = e.message; }
}

// After a round ends: load the range again, as the next run would, and keep the confirmation `recorded` on the empty range.
function reloadLocal(recorded) {
  const seq = ++loadSeq;
  if (source) source.close();
  source = null;
  loadLocal(seq, recorded);
}

function renderCommits() {
  const box = $('#commits');
  if (!box) return;
  const list = h('ul', 'commit-list');
  const open = new Set([...box.querySelectorAll('details[open]')].map((d) => d.dataset.sha)); // bodies the person opened
  for (const c of session.info.commits) {
    const what = h('div', 'what');
    if (c.body) {
      const d = h('details');
      d.dataset.sha = c.sha;
      d.open = open.has(c.sha);
      d.append(h('summary', null, c.subject), h('pre', 'commit-body', c.body));
      what.append(d);
    } else what.append(h('span', null, c.subject));
    const li = h('li');
    li.append(h('code', 'sha', c.sha.slice(0, 7)), what);
    list.append(li);
  }
  box.replaceChildren(h('h2', null, 'Commits'), list);
}

function renderReview() {
  const { review, info } = session;
  const entries = review.entries.filter((e) => e && typeof e === 'object'); // the file is written by another program too
  const resolved = entries.filter((e) => e.status === 'resolved');
  const open = entries.filter((e) => e.status !== 'resolved');
  const box = $('#review');
  const wasOpen = $('.resolved', box)?.open;
  box.replaceChildren(h('h2', null, `Review comments (${open.length} open)`), ...open.map(renderEntry));
  if (!entries.length) box.append(h('p', 'none', 'No comments yet.'));
  if (resolved.length) {
    const d = h('details', 'resolved');
    d.open = !!wasOpen;
    d.append(h('summary', null, `Resolved (${resolved.length})`), ...resolved.map(renderEntry));
    box.append(d);
  }
  if (!info.commits.length && !entries.length && !session.recorded) return; // an agent that only replies leaves the range empty; the round still ends here
  const foot = h('div', 'finish');
  if (session.recorded) foot.append(h('p', null, session.recorded));
  else {
    // One button ends the round: with open entries or a general comment it hands them to the agent, with none it approves every commit.
    const label = h('label', 'lbl', 'General comment');
    const text = Object.assign(h('textarea'), { id: 'general', rows: 3, value: session.draft });
    label.htmlFor = text.id;
    const general = h('div', 'general');
    general.append(label, text);
    box.append(general);
    const btn = button('', 'primary');
    const err = h('span', 'err');
    const count = () => open.length + (text.value.trim() ? 1 : 0);
    const sync = () => {
      session.draft = text.value; // the entry list redraws this panel and would drop what is typed
      btn.textContent = count() ? `Send ${plural(count(), 'comment')} to agent` : 'Approve all and finish';
    };
    text.addEventListener('input', sync);
    sync();
    btn.title = 'Records this HEAD, so the next run shows only commits after it';
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      const approve = !count();
      const note = text.value.trim() ? text.value : '';
      const recorded = `${approve ? 'Approved' : 'Sent to the agent'}. The next run shows only new commits.`;
      if (note) {
        session.draft = ''; // the redraw after the post shows the entry, not the draft
        try { await postReview({ action: 'change', sha: info.head, file: '', lines: '', diff: '', body: note }); } catch (e) {
          session.draft = note;
          err.textContent = e.message;
          btn.disabled = false;
          return;
        }
      }
      await act({ action: 'start', head: info.head, ...(approve && { approve }) }, $('.finish .err', box), () => reloadLocal(recorded));
      btn.disabled = false;
    });
    foot.append(btn, err);
  }
  box.append(foot);
}

function renderEntry(e) {
  const done = e.status === 'resolved';
  const div = h('div', 'entry');
  const head = h('div', 'entry-head');
  head.append(h('b', null, e.kind === 'question' ? 'Question' : 'Change request'), e.file ? h('code', null, `${e.file}:${e.lines ?? ''}`) : h('span', null, 'General comment'),
    h('code', 'sha', String(e.sha ?? '').slice(0, 7)), h('span', `pill ${done ? 'draft' : 'merged'}`, done ? 'resolved' : 'open'));
  div.append(head);
  if (e.quote) div.append(h('blockquote', 'quote', e.quote));
  div.append(h('p', 'body', e.body));
  if (e.reply) {
    const reply = h('div', 'reply');
    reply.append(h('span', 'lbl', 'Agent reply'), h('p', null, e.reply));
    div.append(reply);
  }
  const btn = button(done ? 'Reopen' : 'Resolve');
  const err = h('span', 'err');
  btn.addEventListener('click', () => act({ action: 'status', id: e.id, status: done ? 'open' : 'resolved' }, err));
  const actions = h('div', 'entry-actions');
  actions.append(btn, err);
  div.append(actions);
  return div;
}

// What a request about a chunk carries: its commit, file, a line-range hint (new side, else old side) and the diff as text.
function chunkRef({ c, file }) {
  const side = c.lines.some((l) => l.n != null) ? 'n' : 'o';
  const nums = c.lines.map((l) => l[side]).filter((n) => n != null);
  const range = nums[0] === nums.at(-1) ? `${nums[0]}` : `${nums[0]}-${nums.at(-1)}`;
  return { sha: c.sha, file, lines: side === 'n' ? range : `old ${range}`, diff: c.lines.map((l) => mark(l.t) + l.s).join('\n') };
}

// A textarea and a submit button, off while the text is empty or `blocked` holds a reason. `send(body)` posts it;
// `done(form, review)` shows the result, and an error from `send` appears next to the button.
function bodyForm({ label, blocked, send, done, cancel }) {
  const form = make(`<form class="body-form"><textarea rows="3" aria-label="${label}" placeholder="${label}"></textarea>
    <div class="actions"><button type="submit" class="btn primary">Add to review</button><button type="button" class="btn cancel">Cancel</button><span class="err" role="alert"></span></div></form>`);
  const [text, submit, err] = [$('textarea', form), $('[type=submit]', form), $('.err', form)];
  const sync = () => { submit.disabled = !!blocked || !text.value.trim(); };
  err.textContent = blocked || '';
  text.addEventListener('input', sync);
  $('.cancel', form).addEventListener('click', () => cancel(form));
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    submit.disabled = true;
    try { done(form, await send(text.value)); } catch (x) { err.textContent = x.message; sync(); }
  });
  sync();
  return form;
}

const lastId = (review) => review.entries.at(-1).id;

app.addEventListener('click', (e) => {
  const btn = e.target.closest('.req');
  if (!btn) return;
  const tools = btn.closest('.chunk-tools');
  const open = $('.body-form', tools);
  if (open) return $('textarea', open).focus();
  const chunk = session.chunks.get(btn.closest('.row').id);
  const form = bodyForm({
    label: 'What should change?',
    blocked: chunk.c.sha ? '' : NO_SHA,
    send: (body) => postReview({ action: 'change', ...chunkRef(chunk), body }),
    done: (f, review) => f.replaceWith(h('p', 'sent', `Change request ${lastId(review)} added to the review.`)),
    cancel: (f) => f.remove(),
  });
  tools.append(form);
  $('textarea', form).focus();
});

// Comment on the selected text of a chunk's diff or explanation; it becomes a question in the review.
function askComment(quote, chunkId, a) {
  const chunk = session.chunks.get(chunkId);
  const dlg = make('<div class="term-float term-pop" role="dialog" aria-label="Comment on the selected text" tabindex="-1"></div>');
  dlg.append(h('blockquote', 'quote', quote), bodyForm({
    label: 'Your comment',
    blocked: chunk.c.sha ? '' : NO_SHA,
    send: (body) => postReview({ action: 'question', ...chunkRef(chunk), quote, body }),
    done: (f, review) => { f.replaceWith(h('p', 'sent', `Comment ${lastId(review)} added to the review.`)); place(dlg, a); },
    cancel: closePop,
  }));
  mount(dlg, a);
  $('textarea', dlg).focus({ preventScroll: true });
}

fetch('/api/version').then((r) => r.json()).then((v) => {
  document.getElementById('version').textContent = 'Diffsplainer' + (v.version ? ` ${v.version}` : '') +(v.commit ? ` (${v.commit})` : '');
}).catch(() => {});

fromLocation();
