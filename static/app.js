/* IA Music — Apple Music–style front end for the IA Downloader API.
   Views: Library (finished albums), Album, Downloads (queue/failed/removed/hidden), Search.
   Player: mini-player + full-screen Now Playing tinted from the album art. */
'use strict';

/* ── icons ─────────────────────────────────────────────────────────────── */
const I = {
  play:    '<svg viewBox="0 0 24 24"><path class="fill" d="M8 5.2v13.6c0 .8.9 1.3 1.6.8l10.3-6.8c.6-.4.6-1.2 0-1.6L9.6 4.4C8.9 3.9 8 4.4 8 5.2z"/></svg>',
  pause:   '<svg viewBox="0 0 24 24"><rect class="fill" x="6" y="4.5" width="4.2" height="15" rx="1.2"/><rect class="fill" x="13.8" y="4.5" width="4.2" height="15" rx="1.2"/></svg>',
  next:    '<svg viewBox="0 0 24 24"><path class="fill" d="M3.5 6.3v11.4c0 .7.8 1.1 1.4.7l8.1-5.7c.5-.3.5-1.1 0-1.4L4.9 5.6c-.6-.4-1.4 0-1.4.7zm9.5 0v11.4c0 .7.8 1.1 1.4.7l8.1-5.7c.5-.3.5-1.1 0-1.4l-8.1-5.7c-.6-.4-1.4 0-1.4.7z"/></svg>',
  prev:    '<svg viewBox="0 0 24 24"><path class="fill" d="M20.5 6.3v11.4c0 .7-.8 1.1-1.4.7L11 12.7c-.5-.3-.5-1.1 0-1.4l8.1-5.7c.6-.4 1.4 0 1.4.7zm-9.5 0v11.4c0 .7-.8 1.1-1.4.7l-8.1-5.7c-.5-.3-.5-1.1 0-1.4l8.1-5.7c.6-.4 1.4 0 1.4.7z"/></svg>',
  shuffle: '<svg viewBox="0 0 24 24"><path d="M3 7h3.5c2 0 3.2 1 4.3 2.7l2.4 4.6c1.1 1.7 2.3 2.7 4.3 2.7H21M18 14l3 3-3 3M3 17h3.5c1.3 0 2.2-.4 3-1.1M21 7h-3.5c-1.3 0-2.2.4-3 1.1M18 4l3 3-3 3"/></svg>',
  repeat:  '<svg viewBox="0 0 24 24"><path d="M4 11V9.5A3.5 3.5 0 0 1 7.5 6H20M17 3l3 3-3 3M20 13v1.5a3.5 3.5 0 0 1-3.5 3.5H4M7 21l-3-3 3-3"/></svg>',
  repeat1: '<svg viewBox="0 0 24 24"><path d="M4 11V9.5A3.5 3.5 0 0 1 7.5 6H20M17 3l3 3-3 3M20 13v1.5a3.5 3.5 0 0 1-3.5 3.5H4M7 21l-3-3 3-3"/><path d="M11 10.5 12.5 9.5v5" stroke-width="1.6"/></svg>',
  list:    '<svg viewBox="0 0 24 24"><path d="M9 6h11M9 12h11M9 18h11"/><circle class="fill" cx="4.5" cy="6" r="1.3"/><circle class="fill" cx="4.5" cy="12" r="1.3"/><circle class="fill" cx="4.5" cy="18" r="1.3"/></svg>',
  more:    '<svg viewBox="0 0 24 24"><circle class="fill" cx="5.5" cy="12" r="1.6"/><circle class="fill" cx="12" cy="12" r="1.6"/><circle class="fill" cx="18.5" cy="12" r="1.6"/></svg>',
  toFront: '<svg viewBox="0 0 24 24"><path d="M5 4.5h14M12 20V9M7.5 13.5 12 9l4.5 4.5"/></svg>',
  retry:   '<svg viewBox="0 0 24 24"><path d="M4 12a8 8 0 1 0 2.4-5.7L4 8.5"/><path d="M4 4v4.5h4.5"/></svg>',
  note:    '<svg viewBox="0 0 24 24"><path d="M9 18V5.5l11-2V16"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="17.5" cy="16" r="2.5"/></svg>',
};

/* ── small helpers ─────────────────────────────────────────────────────── */
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
const WAITING = new Set(['queued','pending_meta','fetching']);
const ACTIVE  = new Set(['running','verifying','queued','pending_meta','fetching']);
const CATEGORIES = ['Video Game','Music','Classical','Jazz','Anime','Sound Effects'];

async function api(path, opts = {}) {
  if(opts.json !== undefined) {
    opts = {...opts, method: opts.method || 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(opts.json)};
  }
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch(_) {}
  if(!res.ok) throw new Error((data && data.detail) || `Request failed (${res.status})`);
  return data;
}

function fmtTime(s) {
  if(!isFinite(s) || s < 0) return '0:00';
  return `${Math.floor(s/60)}:${String(Math.floor(s%60)).padStart(2,'0')}`;
}

function artUrl(job) { return job && job.identifier ? `/api/art/${encodeURIComponent(job.identifier)}` : ''; }

// Lazy cover art: <img data-src> loads when it nears the viewport
const imgObserver = new IntersectionObserver(entries => {
  for(const e of entries) {
    if(!e.isIntersecting) continue;
    const img = e.target;
    img.onload = () => img.classList.add('loaded');
    img.onerror = () => img.remove();
    img.src = img.dataset.src;
    imgObserver.unobserve(img);
  }
}, {rootMargin: '400px'});

function artHtml(job, cls = 'art') {
  const src = artUrl(job);
  return `<div class="${cls}"><div class="art-ph">${I.note}</div>${src ? `<img data-src="${src}" alt="">` : ''}</div>`;
}
function observeImages(root) { root.querySelectorAll('img[data-src]').forEach(img => imgObserver.observe(img)); }

// "01. Title", "01 - Title", "1-03 Title" → {n, t}
function parseTrack(name) {
  let m = name.match(/^(\d{1,2})-(\d{1,3})[\s._-]+(.+)$/);
  if(m) return {n: +m[2], t: m[3]};
  m = name.match(/^(\d{1,3})[\s._-]+(?:-\s*)?(.+)$/);
  if(m) return {n: +m[1], t: m[2]};
  return {n: null, t: name};
}

let toastTimer = null;
function toast(msg) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 2800);
}

/* ── data store ────────────────────────────────────────────────────────── */
const jobs = new Map();      // job_id → job
let queuePos = new Map();    // job_id → 1-based queue position
let paused = false;

function normalize(j) {
  return {
    job_id: j.job_id, identifier: j.identifier || '', artist: j.artist || '', album: j.album || '',
    fmt: j.format || j.fmt || '—', category: j.category || '', duplicate: !!j.duplicate,
    pct: j.pct || 0, count: j.count || 0, total: j.total || 0, status: j.status || 'error',
    error: j.error || null, hidden: !!j.hidden, created_at: j.created_at || 0,
  };
}
const albumName = j => j.album || j.identifier || 'Untitled';
const artistName = j => (j.artist && j.artist !== '…') ? j.artist : 'Unknown Artist';

async function loadJobs() {
  const list = await api('/api/jobs');
  jobs.clear();
  queuePos = new Map();
  for(const j of list) {
    jobs.set(j.job_id, normalize(j));
    if(j.queue_pos) queuePos.set(j.job_id, j.queue_pos);
  }
}

// One shared progress stream: changed jobs plus the queue order when it changes
let stream = null;
function startStream() {
  if(stream) return;
  stream = new EventSource('/api/progress');
  stream.onmessage = e => {
    const m = JSON.parse(e.data);
    let membership = false;
    if(m.queue) { queuePos = new Map(m.queue.map((id, i) => [id, i + 1])); membership = true; }
    for(const d of (m.jobs || [])) {
      const old = jobs.get(d.job_id);
      const next = {...(old || {}), ...normalize({...old, ...d, format: d.fmt}), hidden: old ? old.hidden : false,
                    created_at: old ? old.created_at : 0, duplicate: old ? old.duplicate : false};
      if(!old || old.status !== next.status) membership = true;
      jobs.set(d.job_id, next);
      patchProgress(next);
    }
    if(membership) scheduleRender();
  };
  stream.onerror = () => { stream.close(); stream = null; setTimeout(startStream, 2000); };
}

setInterval(async () => {
  try {
    const s = await api('/api/status');
    if(s.paused !== paused) { paused = s.paused; renderNowCard(); }
  } catch(_) {}
}, 4000);

/* ── routing ───────────────────────────────────────────────────────────── */
let route = {name: 'library'};
let lastTop = 'library';
const scrollMem = {};

function parseRoute() {
  const h = location.hash.replace(/^#\/?/, '');
  const [name, arg] = h.split('/');
  if(name === 'album' && arg) return {name: 'album', id: decodeURIComponent(arg)};
  if(['library','downloads','search'].includes(name)) return {name};
  return {name: 'library'};
}
const routeKey = r => r.name === 'album' ? `album/${r.id}` : r.name;

function onRoute() {
  scrollMem[routeKey(route)] = window.scrollY;
  route = parseRoute();
  if(route.name !== 'album') lastTop = route.name;
  document.querySelectorAll('.view').forEach(v => v.classList.toggle('on', v.id === `view-${route.name}`));
  document.querySelectorAll('.tabbar a').forEach(a => a.classList.toggle('on', a.dataset.tab === (route.name === 'album' ? lastTop : route.name)));
  render();
  window.scrollTo(0, route.name === 'album' ? 0 : (scrollMem[routeKey(route)] || 0));
  updateNavbars();
  if(route.name === 'search') setTimeout(() => { if(!$('search-input').value) $('search-input').focus(); }, 50);
}
window.addEventListener('hashchange', onRoute);

function goBack() {
  location.hash = `#/${lastTop}`;
  return false;
}

// Large title scrolls under the bar → bar turns to glass and shows the compact title
function updateNavbars() {
  const view = document.querySelector('.view.on');
  if(!view) return;
  const bar = view.querySelector('.navbar');
  const title = view.querySelector('.large-title, .album-title');
  const barBottom = bar.getBoundingClientRect().bottom;
  bar.classList.toggle('solid', !title || title.getBoundingClientRect().bottom < barBottom + 2);
}
window.addEventListener('scroll', updateNavbars, {passive: true});

let renderTimer = null;
function scheduleRender() {
  clearTimeout(renderTimer);
  renderTimer = setTimeout(render, 350);
}
function render() {
  updateBadges();
  if(route.name === 'library') renderLibrary();
  else if(route.name === 'album') renderAlbum(route.id);
  else if(route.name === 'downloads') renderDownloads();
  else if(route.name === 'search') renderSearch();
}

/* infinite lists: grow when the sentinel nears the viewport */
const PAGE = 120;
let libShown = PAGE, dlShown = PAGE;
const sentinelObserver = new IntersectionObserver(entries => {
  for(const e of entries) {
    if(!e.isIntersecting) continue;
    if(e.target.id === 'library-sentinel' && route.name === 'library') { libShown += PAGE; renderLibrary(true); }
    if(e.target.id === 'dl-sentinel' && route.name === 'downloads') { dlShown += PAGE; renderDownloads(true); }
  }
}, {rootMargin: '800px'});

/* ── Library ───────────────────────────────────────────────────────────── */
let libSort = (() => { try { return localStorage.getItem('libSort') || 'recent'; } catch(_) { return 'recent'; } })();
const SORTS = {recent: 'Recently Added', title: 'Title', artist: 'Artist'};

function libraryAlbums() {
  const list = [...jobs.values()].filter(j => j.status === 'done' && !j.hidden);
  const key = j => albumName(j).toLowerCase();
  if(libSort === 'title') list.sort((a, b) => key(a).localeCompare(key(b)));
  else if(libSort === 'artist') list.sort((a, b) => artistName(a).toLowerCase().localeCompare(artistName(b).toLowerCase()) || key(a).localeCompare(key(b)));
  else list.sort((a, b) => b.created_at - a.created_at);
  return list;
}

function tileHtml(j) {
  const playing = player.jobId === j.job_id ? ' playing' : '';
  return `<a class="tile${playing}" href="#/album/${encodeURIComponent(j.job_id)}">
    ${artHtml(j, 'tile-art')}
    <div class="tile-title">${esc(albumName(j))}</div>
    <div class="tile-sub">${esc(artistName(j))}</div></a>`;
}

function renderLibrary(append = false) {
  const albums = libraryAlbums();
  $('library-sub').textContent = albums.length ? `${albums.length.toLocaleString()} albums · ${SORTS[libSort]}` : '';
  const grid = $('library-grid');
  if(!albums.length) {
    grid.innerHTML = `<div class="empty" style="grid-column:1/-1"><b>No albums yet</b>Finished downloads appear here.</div>`;
    return;
  }
  const shown = grid.querySelectorAll('.tile').length;
  if(append && shown) {
    grid.insertAdjacentHTML('beforeend', albums.slice(shown, libShown).map(tileHtml).join(''));
  } else {
    grid.innerHTML = albums.slice(0, libShown).map(tileHtml).join('');
  }
  observeImages(grid);
}

function openSortSheet() {
  actionSheet({
    title: 'Sort Albums By',
    actions: Object.entries(SORTS).map(([k, label]) => ({
      label, checked: k === libSort,
      fn: () => { libSort = k; try { localStorage.setItem('libSort', k); } catch(_) {} libShown = PAGE; renderLibrary(); window.scrollTo(0, 0); },
    })),
  });
}

/* ── Album page ────────────────────────────────────────────────────────── */
const trackCache = new Map();

async function getTracks(id) {
  if(trackCache.has(id)) return trackCache.get(id);
  const tracks = await api(`/api/jobs/${encodeURIComponent(id)}/tracks`);
  trackCache.set(id, tracks);
  return tracks;
}

async function renderAlbum(id) {
  const j = jobs.get(id);
  const body = $('album-body');
  $('album-back-label').textContent = {library: 'Library', downloads: 'Downloads', search: 'Search'}[lastTop];
  if(!j) { body.innerHTML = `<div class="empty"><b>Album not found</b>It may have been deleted.</div>`; return; }
  $('album-nav-title').textContent = albumName(j);
  $('album-more').onclick = () => jobActions(id);
  if(body.dataset.id !== id) {
    body.dataset.id = id;
    body.innerHTML = `
      <div class="album-hero">
        ${artHtml(j)}
        <div class="album-hero-text">
          <div class="album-title">${esc(albumName(j))}</div>
          <div class="album-artist">${esc(artistName(j))}</div>
          <div class="album-meta">${esc([j.category, j.fmt !== '—' ? j.fmt : ''].filter(Boolean).join(' · '))}</div>
          <div class="album-actions">
            <button class="pill" id="album-play">${I.play}Play</button>
            <button class="pill" id="album-shuffle">${I.shuffle}Shuffle</button>
          </div>
        </div>
      </div>
      <div class="tracks" id="album-tracks"></div>
      <div class="album-foot" id="album-foot"></div>`;
    observeImages(body);
    $('album-play').onclick = () => playAlbum(id, 0, false);
    $('album-shuffle').onclick = () => playAlbum(id, 0, true);
  }
  const tracksEl = $('album-tracks');
  if(j.status !== 'done') {
    tracksEl.innerHTML = '';
    $('album-foot').textContent = j.status === 'removed' ? 'Files removed — download again from the ⋯ menu.' : 'Not downloaded yet.';
    $('album-play').disabled = $('album-shuffle').disabled = true;
    return;
  }
  $('album-play').disabled = $('album-shuffle').disabled = false;
  try {
    const tracks = await getTracks(id);
    if(route.name !== 'album' || route.id !== id) return;
    tracksEl.innerHTML = tracks.map((t, i) => {
      const p = parseTrack(t.display);
      return `<button class="track" data-i="${i}" data-url="${esc(t.url)}">
        <span class="track-n">${p.n ?? i + 1}</span><span class="track-t">${esc(p.t)}</span></button>`;
    }).join('');
    tracksEl.querySelectorAll('.track').forEach(b => b.onclick = () => playAlbum(id, +b.dataset.i, false));
    $('album-foot').textContent = `${tracks.length} ${tracks.length === 1 ? 'song' : 'songs'}`;
    markPlayingTrack();
  } catch(e) {
    tracksEl.innerHTML = '';
    $('album-foot').textContent = `Couldn't load tracks: ${e.message}`;
  }
}

function markPlayingTrack() {
  const cur = player.queue[player.idx];
  document.querySelectorAll('#album-tracks .track').forEach(b => {
    const on = !!cur && player.jobId === route.id && b.dataset.url === cur.url;
    b.classList.toggle('playing', on);
    const n = b.querySelector('.track-n');
    if(on) n.innerHTML = `<span class="bars${audio.paused ? ' paused' : ''}"><i></i><i></i><i></i></span>`;
    else if(n.querySelector('.bars')) n.textContent = parseTrack(b.querySelector('.track-t').textContent).n ?? (+b.dataset.i + 1);
  });
}

/* ── Downloads ─────────────────────────────────────────────────────────── */
let segment = 'queue';
document.querySelectorAll('#dl-segments button').forEach(b => b.onclick = () => {
  segment = b.dataset.seg;
  document.querySelectorAll('#dl-segments button').forEach(x => x.classList.toggle('on', x === b));
  dlShown = PAGE;
  renderDownloads();
});

function runningJob() { return [...jobs.values()].find(j => j.status === 'running' || j.status === 'verifying'); }

function segmentJobs(seg) {
  const all = [...jobs.values()];
  if(seg === 'hidden') return all.filter(j => j.hidden).sort((a, b) => albumName(a).localeCompare(albumName(b)));
  const visible = all.filter(j => !j.hidden);
  if(seg === 'failed') return visible.filter(j => j.status === 'error');
  if(seg === 'removed') return visible.filter(j => j.status === 'removed');
  const pos = j => queuePos.get(j.job_id) || 1e9;
  return visible.filter(j => WAITING.has(j.status)).sort((a, b) => pos(a) - pos(b));
}

function updateBadges() {
  let q = 0, f = 0, r = 0, h = 0;
  for(const j of jobs.values()) {
    if(j.hidden) { h++; continue; }
    if(WAITING.has(j.status)) q++;
    else if(j.status === 'error') f++;
    else if(j.status === 'removed') r++;
  }
  const n = v => v ? v.toLocaleString() : '';
  $('seg-queue').textContent = n(q); $('seg-failed').textContent = n(f);
  $('seg-removed').textContent = n(r); $('seg-hidden').textContent = n(h);
  const badge = $('tab-badge');
  badge.textContent = f > 99 ? '99+' : f;
  badge.classList.toggle('show', f > 0);
}

function statusLine(j) {
  const s = j.status;
  if(s === 'error') return ['err', j.error || 'Failed'];
  if(s === 'removed') return ['', 'Files removed'];
  if(s === 'done') return ['', 'Downloaded'];
  if(s === 'running' || s === 'verifying') return ['run', `${s === 'verifying' ? 'Verifying' : 'Downloading'} · ${j.pct}%`];
  if(s === 'pending_meta' || s === 'fetching') {
    const p = queuePos.get(j.job_id);
    return ['', p ? `Waiting · #${p.toLocaleString()}` : 'Waiting'];
  }
  const p = queuePos.get(j.job_id);
  return ['', p ? `Queued · #${p.toLocaleString()}` : 'Queued'];
}

function rowHtml(j) {
  const [cls, text] = statusLine(j);
  const pos = queuePos.get(j.job_id);
  let trailing = '';
  if(WAITING.has(j.status) && pos && pos > 1) trailing += `<button class="icon-btn" data-act="front" aria-label="Move to front">${I.toFront}</button>`;
  if(j.status === 'error' || j.status === 'removed') trailing += `<button class="icon-btn" data-act="retry" aria-label="Retry">${I.retry}</button>`;
  trailing += `<button class="icon-btn dim" data-act="more" aria-label="More">${I.more}</button>`;
  const fmt = j.fmt && j.fmt !== '—' ? `<span class="badge">${esc(j.fmt)}</span>` : '';
  return `<div class="row" data-id="${esc(j.job_id)}">
    ${artHtml(j)}
    <div class="row-main" data-act="open">
      <div class="row-title">${esc(albumName(j))}${fmt}</div>
      <div class="row-sub">${esc(artistName(j))}</div>
      <div class="row-status ${cls}">${esc(text)}</div>
    </div>${trailing}</div>`;
}

function renderDownloads(append = false) {
  renderNowCard();
  updateBadges();
  const list = segmentJobs(segment);
  const head = $('dl-head');
  if(segment === 'failed' && list.length) head.innerHTML = `<span>${list.length.toLocaleString()} failed</span><button class="text-btn" onclick="retryAllFailed()">Retry All</button>`;
  else if(segment === 'queue' && list.length) head.innerHTML = `<span>${list.length.toLocaleString()} waiting</span>`;
  else head.innerHTML = '';
  const el = $('dl-list');
  if(!list.length) {
    const msg = {queue: ['Queue is empty', 'Tap + to add an album or collection.'], failed: ['No failures', ''],
                 removed: ['Nothing removed', ''], hidden: ['No hidden albums', '']}[segment];
    el.innerHTML = `<div class="empty"><b>${msg[0]}</b>${msg[1]}</div>`;
    return;
  }
  const shown = el.querySelectorAll('.row').length;
  if(append && shown) el.insertAdjacentHTML('beforeend', list.slice(shown, dlShown).map(rowHtml).join(''));
  else el.innerHTML = list.slice(0, dlShown).map(rowHtml).join('');
  observeImages(el);
}

$('dl-list').addEventListener('click', e => {
  const btn = e.target.closest('[data-act]');
  const row = e.target.closest('.row');
  if(!btn || !row) return;
  const id = row.dataset.id, j = jobs.get(id);
  if(!j) return;
  const act = btn.dataset.act;
  if(act === 'front') moveToFront(id);
  else if(act === 'retry') retryJob(id);
  else if(act === 'more') jobActions(id);
  else if(act === 'open') { if(j.status === 'done') location.hash = `#/album/${encodeURIComponent(id)}`; else jobActions(id); }
});

function renderNowCard() {
  const card = $('now-card');
  if(!card) return;
  const r = runningJob();
  if(!r) {
    const waiting = segmentJobs('queue').length;
    card.className = 'now-card idle';
    card.innerHTML = paused && waiting
      ? `<span style="flex:1">Downloads paused · ${waiting.toLocaleString()} waiting</span><button class="now-btn" onclick="togglePause()" aria-label="Resume">${I.play}</button>`
      : (waiting ? 'Starting next download…' : 'Nothing downloading');
    return;
  }
  card.className = 'now-card';
  if(card.dataset.id !== r.job_id) {
    card.dataset.id = r.job_id;
    card.innerHTML = `${artHtml(r)}
      <div class="now-info">
        <div class="now-label" id="now-label"></div>
        <div class="now-title">${esc(albumName(r))}</div>
        <div class="now-sub" id="now-sub"></div>
        <div class="bar"><div id="now-bar"></div></div>
      </div>
      <button class="now-btn" id="now-btn" onclick="togglePause()"></button>`;
    observeImages(card);
  }
  $('now-label').textContent = paused ? 'Paused' : (r.status === 'verifying' ? 'Verifying' : 'Downloading');
  $('now-sub').textContent = `${artistName(r)} · ${r.count}/${r.total || '?'} files · ${r.pct}%`;
  $('now-bar').style.width = `${r.pct}%`;
  $('now-btn').innerHTML = paused ? I.play : I.pause;
  $('now-btn').setAttribute('aria-label', paused ? 'Resume' : 'Pause');
}

// Progress-only updates: touch just the now-card and the job's row
function patchProgress(j) {
  if(route.name !== 'downloads') return;
  if(j.status === 'running' || j.status === 'verifying') renderNowCard();
  const row = document.querySelector(`#dl-list .row[data-id="${CSS.escape(j.job_id)}"] .row-status`);
  if(row) { const [cls, text] = statusLine(j); row.className = `row-status ${cls}`; row.textContent = text; }
}

/* ── Search ────────────────────────────────────────────────────────────── */
let searchLimit = {albums: 24, other: 30};
const searchInput = $('search-input');
searchInput.addEventListener('input', () => {
  $('search-clear').classList.toggle('show', !!searchInput.value);
  searchLimit = {albums: 24, other: 30};
  clearTimeout(searchInput._t);
  searchInput._t = setTimeout(renderSearch, 120);
});
$('search-clear').onclick = () => { searchInput.value = ''; $('search-clear').classList.remove('show'); renderSearch(); searchInput.focus(); };

function renderSearch() {
  const q = searchInput.value.trim().toLowerCase();
  const out = $('search-results');
  if(!q) { out.innerHTML = `<div class="empty">Search your library and downloads.</div>`; return; }
  const hit = j => albumName(j).toLowerCase().includes(q) || artistName(j).toLowerCase().includes(q) || j.identifier.toLowerCase().includes(q);
  const matches = [...jobs.values()].filter(hit);
  const albums = matches.filter(j => j.status === 'done' && !j.hidden).sort((a, b) => albumName(a).localeCompare(albumName(b)));
  const other = matches.filter(j => !(j.status === 'done' && !j.hidden));
  if(!albums.length && !other.length) { out.innerHTML = `<div class="empty"><b>No Results</b>Nothing matches “${esc(searchInput.value)}”.</div>`; return; }
  let html = '';
  if(albums.length) {
    html += `<div class="result-head">Albums</div><div class="grid">${albums.slice(0, searchLimit.albums).map(tileHtml).join('')}</div>`;
    if(albums.length > searchLimit.albums) html += `<button class="more-btn" data-more="albums">Show all ${albums.length.toLocaleString()} albums</button>`;
  }
  if(other.length) {
    html += `<div class="result-head">Downloads</div><div class="list" id="search-list">${other.slice(0, searchLimit.other).map(rowHtml).join('')}</div>`;
    if(other.length > searchLimit.other) html += `<button class="more-btn" data-more="other">Show all ${other.length.toLocaleString()}</button>`;
  }
  out.innerHTML = html;
  observeImages(out);
  out.querySelectorAll('[data-more]').forEach(b => b.onclick = () => { searchLimit[b.dataset.more] = 1e9; renderSearch(); });
  const list = $('search-list');
  if(list) list.addEventListener('click', e => {
    const btn = e.target.closest('[data-act]'), row = e.target.closest('.row');
    if(!btn || !row) return;
    const id = row.dataset.id;
    ({front: moveToFront, retry: retryJob, more: jobActions, open: jobActions})[btn.dataset.act](id);
  });
}

/* ── sheets ────────────────────────────────────────────────────────────── */
function closeSheets() {
  $('scrim').classList.remove('open');
  $('add-sheet').classList.remove('open');
  $('asheet').classList.remove('open');
}
document.addEventListener('keydown', e => {
  if(e.key !== 'Escape') return;
  if($('np').classList.contains('open')) closeNowPlaying(); else closeSheets();
});

// iOS-style action sheet: {title, message, actions: [{label, fn, destructive, checked}]}
function actionSheet({title, message, actions}) {
  const group = $('asheet-group');
  const head = (title || message) ? `<div class="asheet-msg">${title ? `<b>${esc(title)}</b>` : ''}${esc(message || '')}</div>` : '';
  group.innerHTML = head + actions.map((a, i) =>
    `<button data-i="${i}" class="${a.destructive ? 'destructive' : ''}${a.checked ? ' checked' : ''}">${esc(a.label)}</button>`).join('');
  group.querySelectorAll('button').forEach(b => b.onclick = () => { closeSheets(); setTimeout(() => actions[+b.dataset.i].fn(), 120); });
  $('scrim').classList.add('open');
  $('asheet').classList.add('open');
}

function openAddSheet() {
  $('add-note').textContent = '';
  $('add-note').className = 'field-note';
  $('scrim').classList.add('open');
  $('add-sheet').classList.add('open');
  setTimeout(() => $('add-url').focus(), 300);
}
$('add-url').addEventListener('keydown', e => { if(e.key === 'Enter') submitAdd(); });

async function submitAdd() {
  const url = $('add-url').value.trim();
  if(!url) return;
  const note = $('add-note');
  $('add-go').disabled = true;
  note.className = 'field-note';
  note.textContent = 'Looking up the item on archive.org…';
  try {
    const data = await api('/api/download', {json: {url, category: $('add-cat').value}});
    if(data.collection) {
      for(const j of data.jobs) jobs.set(j.job_id, normalize({...j, album: j.identifier, status: 'pending_meta', created_at: Date.now()/1000}));
      toast(`Added ${data.count.toLocaleString()} albums from the collection` + (data.skipped_blocked ? ` · ${data.skipped_blocked} deleted skipped` : ''));
    } else {
      jobs.set(data.job_id, normalize({...data, status: 'queued', created_at: Date.now()/1000}));
      toast(data.duplicate ? `“${data.album}” is already in your library — it will be re-checked` : `Added “${data.album}”`);
    }
    $('add-url').value = '';
    closeSheets();
    render();
  } catch(e) {
    note.className = 'field-note err';
    note.textContent = e.message;
  } finally {
    $('add-go').disabled = false;
  }
}

/* ── job actions ───────────────────────────────────────────────────────── */
function jobActions(id) {
  const j = jobs.get(id);
  if(!j) return;
  const acts = [];
  if(j.status === 'done') {
    acts.push({label: 'Play', fn: () => playAlbum(id, 0, false)});
    if(route.name !== 'album') acts.push({label: 'Go to Album', fn: () => { location.hash = `#/album/${encodeURIComponent(id)}`; }});
  }
  const pos = queuePos.get(id);
  if(WAITING.has(j.status) && pos > 1) acts.push({label: 'Move to Front of Queue', fn: () => moveToFront(id)});
  if(j.status === 'error' || j.status === 'removed') acts.push({label: 'Download Again', fn: () => retryJob(id)});
  if(j.status !== 'running' && j.status !== 'verifying') acts.push({label: 'Change Category…', fn: () => categorySheet(id)});
  acts.push({label: j.hidden ? 'Unhide' : 'Hide', fn: () => setHidden(id, !j.hidden)});
  if(j.status !== 'removed') acts.push({label: 'Remove Downloaded Files', destructive: true, fn: () => confirmRemove(id)});
  acts.push({label: 'Delete Permanently', destructive: true, fn: () => confirmDelete(id)});
  actionSheet({title: albumName(j), message: artistName(j), actions: acts});
}

function categorySheet(id) {
  const j = jobs.get(id);
  actionSheet({
    title: 'Category', message: albumName(j),
    actions: CATEGORIES.map(c => ({label: c, checked: c === j.category, fn: async () => {
      try {
        await api(`/api/jobs/${encodeURIComponent(id)}/category`, {method: 'PATCH', json: {category: c}});
        jobs.set(id, {...jobs.get(id), category: c});
        trackCache.delete(id);
        $('album-body').dataset.id = '';  // rebuild the album header with the new category
        toast(`Moved to ${c}`);
        render();
      } catch(e) { toast(e.message); }
    }})),
  });
}

async function setHidden(id, hidden) {
  try {
    await api(`/api/jobs/${encodeURIComponent(id)}/hide`, {json: {hidden}});
    jobs.set(id, {...jobs.get(id), hidden});
    toast(hidden ? 'Hidden — find it under Downloads › Hidden' : 'Unhidden');
    render();
  } catch(e) { toast(e.message); }
}

function confirmRemove(id) {
  const j = jobs.get(id);
  actionSheet({
    title: 'Remove downloaded files?',
    message: `The files for “${albumName(j)}” are deleted. The album stays in your list so you can download it again.`,
    actions: [{label: 'Remove Files', destructive: true, fn: async () => {
      try {
        const r = await api(`/api/jobs/${encodeURIComponent(id)}/remove-files`, {method: 'POST'});
        for(const jid of r.job_ids) { jobs.set(jid, {...jobs.get(jid), status: 'removed', pct: 0, count: 0, error: null}); trackCache.delete(jid); }
        if(player.jobId === id) stopPlayer();
        toast('Files removed');
        render();
      } catch(e) { toast(e.message); }
    }}],
  });
}

function confirmDelete(id) {
  const j = jobs.get(id);
  actionSheet({
    title: 'Delete permanently?',
    message: `“${albumName(j)}” and its files are deleted, and it goes on the do-not-download list so it can't be added again.`,
    actions: [{label: 'Delete Album', destructive: true, fn: async () => {
      try {
        const r = await api(`/api/jobs/${encodeURIComponent(id)}`, {method: 'DELETE'});
        for(const jid of r.job_ids) { jobs.delete(jid); trackCache.delete(jid); }
        if(player.jobId === id) stopPlayer();
        toast('Deleted');
        if(route.name === 'album') goBack(); else render();
      } catch(e) { toast(e.message); }
    }}],
  });
}

async function retryJob(id) {
  try {
    const r = await api(`/api/jobs/${encodeURIComponent(id)}/retry`, {method: 'POST'});
    jobs.set(id, {...jobs.get(id), status: r.status, error: null});
    toast('Added to the front of the queue');
    render();
  } catch(e) { toast(e.message); }
}

async function retryAllFailed() {
  try {
    const r = await api('/api/clear', {json: {mode: 'retry_failed'}});
    for(const id of r.retried) jobs.set(id, {...jobs.get(id), status: 'queued', error: null});
    toast(`Retrying ${r.retried.length.toLocaleString()} downloads`);
    render();
  } catch(e) { toast(e.message); }
}

async function moveToFront(id) {
  try {
    await api(`/api/jobs/${encodeURIComponent(id)}/front`, {method: 'POST'});
    // the stream sends the new order; apply it locally right away too
    const order = [...queuePos.entries()].sort((a, b) => a[1] - b[1]).map(e => e[0]).filter(x => x !== id);
    queuePos = new Map([id, ...order].map((x, i) => [x, i + 1]));
    render();
  } catch(e) { toast(e.message); }
}

async function togglePause() {
  try {
    const r = await api(paused ? '/api/resume' : '/api/pause', {method: 'POST'});
    paused = r.paused;
    renderNowCard();
  } catch(e) { toast(e.message); }
}

/* ── player ────────────────────────────────────────────────────────────── */
const audio = $('audio');
const player = {jobId: null, queue: [], order: [], idx: 0, shuffle: false, repeat: 'none', seeking: false};

function shuffled(arr) {
  const a = [...arr];
  for(let i = a.length - 1; i > 0; i--) { const k = Math.floor(Math.random() * (i + 1)); [a[i], a[k]] = [a[k], a[i]]; }
  return a;
}

async function playAlbum(id, start = 0, shuffle = false) {
  let tracks;
  try { tracks = await getTracks(id); } catch(e) { toast(`Can't play: ${e.message}`); return; }
  if(!tracks.length) { toast('No playable tracks in this album'); return; }
  const changed = player.jobId !== id;
  player.jobId = id;
  player.order = tracks;
  player.shuffle = shuffle;
  if(shuffle) { player.queue = shuffled(tracks); player.idx = 0; }
  else { player.queue = [...tracks]; player.idx = start; }
  loadTrack(player.idx, true);
  document.body.classList.add('has-player');
  $('mini').classList.remove('hidden');
  if(changed) applyArtColors(jobs.get(id));
  document.querySelectorAll('.tile').forEach(t => t.classList.toggle('playing', t.getAttribute('href') === `#/album/${encodeURIComponent(id)}`));
}

function stopPlayer() {
  audio.pause();
  audio.removeAttribute('src');
  player.jobId = null; player.queue = [];
  $('mini').classList.add('hidden');
  document.body.classList.remove('has-player');
  closeNowPlaying();
}

function loadTrack(idx, autoplay) {
  if(idx < 0 || idx >= player.queue.length) return;
  player.idx = idx;
  audio.src = player.queue[idx].url;
  if(autoplay) audio.play().catch(() => {});
  updatePlayerUI();
}

function playerToggle() {
  if(!audio.src) return;
  if(audio.paused) audio.play().catch(() => {}); else audio.pause();
}

function playerPrev() {
  if(audio.currentTime > 3 || player.idx === 0 && player.repeat !== 'all') { audio.currentTime = 0; return; }
  loadTrack(player.idx > 0 ? player.idx - 1 : player.queue.length - 1, true);
}

function playerNext(fromEnded = false) {
  if(fromEnded && player.repeat === 'one') { audio.currentTime = 0; audio.play().catch(() => {}); return; }
  if(player.idx + 1 < player.queue.length) loadTrack(player.idx + 1, true);
  else if(player.repeat === 'all') loadTrack(0, true);
  else if(fromEnded) { audio.currentTime = 0; audio.pause(); }
}

function toggleShuffle() {
  if(!player.queue.length) return;
  const cur = player.queue[player.idx];
  player.shuffle = !player.shuffle;
  if(player.shuffle) player.queue = [cur, ...shuffled(player.order.filter(t => t.url !== cur.url))];
  else player.queue = [...player.order];
  player.idx = player.queue.findIndex(t => t.url === cur.url);
  updatePlayerUI();
}

function cycleRepeat() {
  player.repeat = {none: 'all', all: 'one', one: 'none'}[player.repeat];
  updatePlayerUI();
}

function updatePlayerUI() {
  const j = jobs.get(player.jobId);
  const t = player.queue[player.idx];
  if(!t) return;
  const title = parseTrack(t.display).t;
  const sub = j ? `${artistName(j)} — ${albumName(j)}` : '';
  $('mini-title').textContent = title;
  $('mini-sub').textContent = j ? artistName(j) : '';
  $('np-title').textContent = title;
  $('np-sub').textContent = sub;
  $('np-sub').href = j ? `#/album/${encodeURIComponent(j.job_id)}` : '#';
  $('np-sub').onclick = () => closeNowPlaying();
  const src = artUrl(j);
  for(const el of [$('mini-art'), $('np-art')]) {
    if(el.dataset.src !== src) {
      el.dataset.src = src;
      el.innerHTML = `<div class="art-ph">${I.note}</div>` + (src ? `<img src="${src}" alt="" onerror="this.remove()">` : '');
    }
  }
  $('np-shuffle').innerHTML = I.shuffle; $('np-shuffle').classList.toggle('on', player.shuffle);
  $('np-repeat').innerHTML = player.repeat === 'one' ? I.repeat1 : I.repeat; $('np-repeat').classList.toggle('on', player.repeat !== 'none');
  $('np-queue-btn').innerHTML = I.list;
  renderUpNext();
  updatePlayState();
  updateMediaSession(j, title);
  markPlayingTrack();
}

function updatePlayState() {
  const playing = !audio.paused;
  $('mini-play').innerHTML = playing ? I.pause : I.play;
  $('np-play').innerHTML = playing ? I.pause : I.play;
  $('np').classList.toggle('paused', !playing);
  document.querySelectorAll('#album-tracks .bars').forEach(b => b.classList.toggle('paused', !playing));
  if('mediaSession' in navigator) navigator.mediaSession.playbackState = playing ? 'playing' : 'paused';
}

function renderUpNext() {
  const box = $('np-queue');
  box.innerHTML = `<h3>Playing Next</h3>` + player.queue.map((t, i) =>
    `<button data-i="${i}" class="${i === player.idx ? 'cur' : ''}"><span class="qn">${i === player.idx ? '▶' : i + 1}</span>${esc(parseTrack(t.display).t)}</button>`).join('');
  box.querySelectorAll('button').forEach(b => b.onclick = () => loadTrack(+b.dataset.i, true));
}

function toggleQueue() {
  $('np').classList.toggle('show-queue');
  $('np-queue-btn').classList.toggle('on', $('np').classList.contains('show-queue'));
  const cur = $('np-queue').querySelector('.cur');
  if(cur) cur.scrollIntoView({block: 'center'});
}

function setScrub(el, value, max) {
  el.max = String(max || 100);
  el.value = String(value || 0);
  el.style.setProperty('--pct', `${max ? (value / max) * 100 : 0}%`);
}

audio.addEventListener('play', updatePlayState);
audio.addEventListener('pause', updatePlayState);
audio.addEventListener('ended', () => playerNext(true));
audio.addEventListener('loadedmetadata', () => { $('np-rem').textContent = `-${fmtTime(audio.duration)}`; $('mini-dur').textContent = fmtTime(audio.duration); });
let lastPositionSync = 0;
audio.addEventListener('timeupdate', () => {
  const cur = audio.currentTime, dur = audio.duration || 0;
  $('mini-progress-fill').style.width = dur ? `${(cur / dur) * 100}%` : '0';
  if(!player.seeking) {
    setScrub($('np-seek'), cur, dur); setScrub($('mini-seek'), cur, dur);
    $('np-cur').textContent = fmtTime(cur); $('mini-cur').textContent = fmtTime(cur);
    $('np-rem').textContent = `-${fmtTime(dur - cur)}`;
  }
  if('mediaSession' in navigator && dur && Date.now() - lastPositionSync > 5000) {
    lastPositionSync = Date.now();
    try { navigator.mediaSession.setPositionState({duration: dur, position: Math.min(cur, dur), playbackRate: 1}); } catch(_) {}
  }
});

for(const id of ['np-seek', 'mini-seek']) {
  const el = $(id);
  el.addEventListener('pointerdown', () => { player.seeking = true; });
  el.addEventListener('input', () => {
    player.seeking = true;
    el.style.setProperty('--pct', `${(el.value / el.max) * 100}%`);
    $('np-cur').textContent = fmtTime(+el.value);
    $('np-rem').textContent = `-${fmtTime((audio.duration || 0) - el.value)}`;
  });
  el.addEventListener('change', () => { audio.currentTime = +el.value; player.seeking = false; });
}
// iOS ignores page-set volume (hardware buttons only), so hide the slider there
if(isIOS) $('np-volume').classList.add('hidden');
$('np-vol').addEventListener('input', e => { audio.volume = +e.target.value; e.target.style.setProperty('--pct', `${e.target.value * 100}%`); });
$('np-vol').style.setProperty('--pct', '100%');

$('np-prev').innerHTML = I.prev; $('np-next').innerHTML = I.next;
$('mini-prev').innerHTML = I.prev; $('mini-next').innerHTML = I.next;
$('mini-play').innerHTML = I.play; $('np-play').innerHTML = I.play;

// Lock screen / Control Center controls
function updateMediaSession(j, title) {
  if(!('mediaSession' in navigator)) return;
  const art = artUrl(j);
  navigator.mediaSession.metadata = new MediaMetadata({
    title, artist: j ? artistName(j) : '', album: j ? albumName(j) : '',
    artwork: art ? [{src: location.origin + art, sizes: '512x512', type: 'image/jpeg'}] : [],
  });
}
if('mediaSession' in navigator) {
  const ms = navigator.mediaSession;
  const set = (a, fn) => { try { ms.setActionHandler(a, fn); } catch(_) {} };
  set('play', () => audio.play());
  set('pause', () => audio.pause());
  set('previoustrack', playerPrev);
  set('nexttrack', () => playerNext());
  set('seekto', d => { audio.currentTime = d.seekTime; });
}

/* Now Playing sheet */
function openNowPlaying() {
  if(!player.queue.length) return;
  $('np').classList.add('open');
  $('np').setAttribute('aria-hidden', 'false');
  document.body.classList.add('np-open');
}
function closeNowPlaying() {
  $('np').classList.remove('open', 'show-queue');
  $('np-queue-btn').classList.remove('on');
  $('np').setAttribute('aria-hidden', 'true');
  document.body.classList.remove('np-open');
}

// drag the sheet down to dismiss
(() => {
  const np = $('np');
  let y0 = null, dy = 0;
  np.addEventListener('touchstart', e => {
    if(e.target.closest('input, .np-queue')) return;
    y0 = e.touches[0].clientY; dy = 0;
    np.classList.add('dragging');
  }, {passive: true});
  np.addEventListener('touchmove', e => {
    if(y0 === null) return;
    dy = Math.max(0, e.touches[0].clientY - y0);
    np.style.transform = `translateY(${dy}px)`;
  }, {passive: true});
  np.addEventListener('touchend', () => {
    if(y0 === null) return;
    np.classList.remove('dragging');
    np.style.transform = '';
    if(dy > 120) closeNowPlaying();
    y0 = null;
  });
})();

// Tint Now Playing with the album art's dominant vivid color
const colorCache = new Map();
function applyArtColors(j) {
  const src = artUrl(j);
  const np = $('np');
  const apply = c => { np.style.setProperty('--np-c1', c ? c[0] : '#3a3a3c'); np.style.setProperty('--np-c2', c ? c[1] : '#111'); };
  if(!src) return apply(null);
  if(colorCache.has(src)) return apply(colorCache.get(src));
  const img = new Image();
  img.onload = () => {
    try {
      const c = document.createElement('canvas');
      c.width = c.height = 24;
      const g = c.getContext('2d', {willReadFrequently: true});
      g.drawImage(img, 0, 0, 24, 24);
      const d = g.getImageData(0, 0, 24, 24).data;
      let r = 0, gr = 0, b = 0, w = 0;
      for(let i = 0; i < d.length; i += 4) {
        const R = d[i], G = d[i+1], B = d[i+2];
        const mx = Math.max(R, G, B), mn = Math.min(R, G, B);
        const sat = mx ? (mx - mn) / mx : 0, lum = mx / 255;
        const weight = 0.05 + sat * sat * (lum > 0.15 ? 1 : 0.2);   // favor vivid, non-black pixels
        r += R * weight; gr += G * weight; b += B * weight; w += weight;
      }
      const hsl = rgbToHsl(r / w, gr / w, b / w);
      const s = Math.min(hsl[1], 0.65);
      const colors = [`hsl(${hsl[0]} ${Math.round(s * 100)}% 34%)`, `hsl(${hsl[0]} ${Math.round(s * 80)}% 11%)`];
      colorCache.set(src, colors);
      if(player.jobId === j.job_id) apply(colors);
    } catch(_) { apply(null); }
  };
  img.onerror = () => apply(null);
  img.src = src;
}
function rgbToHsl(r, g, b) {
  r /= 255; g /= 255; b /= 255;
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b), l = (mx + mn) / 2;
  if(mx === mn) return [0, 0, l];
  const d = mx - mn, s = l > 0.5 ? d / (2 - mx - mn) : d / (mx + mn);
  let h = mx === r ? (g - b) / d + (g < b ? 6 : 0) : mx === g ? (b - r) / d + 2 : (r - g) / d + 4;
  return [Math.round(h * 60), s, l];
}

/* ── start ─────────────────────────────────────────────────────────────── */
(async () => {
  sentinelObserver.observe($('library-sentinel'));
  sentinelObserver.observe($('dl-sentinel'));
  try {
    const [, status] = await Promise.all([loadJobs(), api('/api/status')]);
    paused = !!status.paused;
  } catch(e) { toast(`Couldn't load: ${e.message}`); }
  onRoute();
  startStream();
})();
