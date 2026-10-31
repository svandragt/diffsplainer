// Front-end: reads the PR from the address bar, renders aligned chunk rows, streams explanations.
const $ = (s, el = document) => el.querySelector(s);
const app = $('#app');
const urlInput = $('#url');
let source = null;
let loadSeq = 0;

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
}

function fromLocation() {
  closePop();
  const pr = parsePr(location.pathname);
  if (!pr) {
    loadSeq++;
    if (source) source.close();
    source = null;
    urlInput.value = '';
    document.title = 'Diffsplainer · PR review';
    setApp(emptyHtml);
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
  explain(owner, repo, num);
}

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
    <div class="split">
      <section class="card overview" id="overview"><h2>Overview</h2><div class="skeleton"></div><div class="skeleton short"></div></section>
      <section class="card context" id="context"><h2>Context given to Claude</h2><div class="skeleton short"></div></section>
    </div>
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

function renderFile(f) {
  const rename = f.previous ? `<span class="tag">renamed from ${esc(f.previous)}</span>` : '';
  const head = `<div class="file-head"><span class="tag">${f.status}</span><span class="name">${esc(f.filename)}</span>${rename}
    <span class="stat"><span class="plus">+${f.additions}</span> <span class="minus">−${f.deletions}</span></span></div>`;
  const rows = f.chunks.length
    ? f.chunks.map((c) => renderChunk(c, langOf(f.filename))).join('')
    : `<div class="row"><div class="nodiff">No textual diff (binary, too large, or rename only).</div></div>`;
  return `<section class="file">${head}${rows}</section>`;
}

function renderChunk(c, lang) {
  const body = c.lines
    .map((l) => {
      const m = l.t === 'add' ? '+' : l.t === 'del' ? '-' : ' ';
      return `<tr class="${l.t}"><td class="n">${l.o ?? ''}</td><td class="n">${l.n ?? ''}</td><td class="s" data-m="${m}">${esc(l.s)}</td></tr>`;
    })
    .join('');
  const scope = c.scope ? `<tr class="scope"><td colspan="3">${esc(c.scope)}</td></tr>` : '';
  return `<div class="row" id="${c.id}">
    <aside class="explain"><div class="inner"><div class="skeleton"></div><div class="skeleton"></div><div class="skeleton short"></div></div></aside>
    <div class="diff"${lang ? ` data-lang="${lang}"` : ''}><table>${scope}${body}</table></div>
  </div>`;
}

function explain(owner, repo, num) {
  const prs = new URLSearchParams(location.search).get('prs');
  source = new EventSource(`/api/explain/${owner}/${repo}/${num}${prs === null ? '' : `?prs=${encodeURIComponent(prs)}`}`);
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
      status(d.scope === 'limit' ? d.message : `Error: ${d.message}`);
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
  const { owner, repo, num } = parsePr(location.pathname);
  const dlg = make(`<div class="term-float term-pop" role="dialog" aria-label="Explanation of the selected text" aria-live="polite" tabindex="-1">
    <div class="skeleton"></div><div class="skeleton"></div><div class="skeleton short"></div></div>`);
  mount(dlg, a);
  dlg.focus({ preventScroll: true });
  const ctl = (popAbort = new AbortController());
  const q = new URLSearchParams({ text, around });
  if (chunk) q.set('chunk', chunk);
  try {
    const res = await fetch(`/api/explain-term/${owner}/${repo}/${num}?${q}`, { signal: ctl.signal });
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
  const host = inner?.closest('.explain, #overview'); // a range that starts and ends in different hosts has a common ancestor outside both
  if (!host) return;
  const rects = range.getClientRects();
  const end = rects[rects.length - 1] || range.getBoundingClientRect();
  const a = { x: end.right + scrollX, top: end.top + scrollY, bottom: end.bottom + scrollY };
  if (text.length > 300) return mount(make('<div class="term-float term-note" role="status">Select less text</div>'), a);
  const btn = make('<button class="term-float term-btn">Explain</button>');
  const around = (inner.closest('p') || host).textContent.replace(/\s+/g, ' ').trim().slice(0, 4000); // the server rejects more
  btn.addEventListener('mousedown', (e) => e.preventDefault()); // keep the selection
  btn.addEventListener('click', () => askTerm({ text, around, chunk: host.closest('.row')?.id }, a));
  mount(btn, a);
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

fromLocation();
