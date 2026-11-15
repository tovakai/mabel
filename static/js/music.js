// Music: a Navidrome client inside Mabel.
//
// Navidrome stays its own service (other PC). Everything goes through Mabel's
// backend (routes/music_routes.py): /api/music/rest/<method> for the Subsonic
// API, /api/music/stream/<id> for audio, /api/music/cover/<id> for art. The
// browser never sees the Navidrome credentials.
//
// Pieces:
//   - one <audio> owned by this module, so music keeps playing while windows
//     open and close (a page reload stops it; the queue and position come back
//     paused);
//   - the sidebar mini-player (#music-mini) and a rail play button;
//   - the Music window (#music-modal): search, home shelves, album/artist,
//     playlists, starred, queue, synced lyrics, connection;
//   - Media Session, so media keys and the phone lock screen work;
//   - scrobbles to Navidrome (now playing, then played at half / 4 min).

import uiModule from './ui.js?v=20260916largetoolscroll1';
import { makeWindowDraggable } from './windowDrag.js';

const toast = (msg) => uiModule.showToast(msg);
const STORE_KEY = 'mabel-music-state-v1';

// ── API ──

async function api(method, params = {}) {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === '') continue;
    for (const one of Array.isArray(v) ? v : [v]) q.append(k, String(one));
  }
  const r = await fetch(`/api/music/rest/${method}?${q}`, { credentials: 'same-origin' });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
  return r.json();
}

async function apiPost(method, body) {
  const r = await fetch(`/api/music/rest/${method}`, {
    method: 'POST', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
  return r.json();
}

const cover = (id, size = 300) => (id ? `/api/music/cover/${encodeURIComponent(id)}?size=${size}` : '');
const streamUrl = (id) => `/api/music/stream/${encodeURIComponent(id)}`;

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function fmt(sec) {
  sec = Math.max(0, Math.floor(Number(sec) || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${m}:${String(s).padStart(2, '0')}`;
}

function song(raw) {
  return {
    id: raw.id, title: raw.title || '(untitled)', artist: raw.artist || raw.displayArtist || '',
    artistId: raw.artistId || '', album: raw.album || '', albumId: raw.albumId || '',
    coverArt: raw.coverArt || raw.albumId || '', duration: raw.duration || 0,
    track: raw.track || 0, starred: !!raw.starred,
  };
}

// ── Player state ──

const audio = new Audio();
audio.preload = 'auto';
const state = {
  queue: [],          // song objects
  index: -1,
  repeat: 'off',      // off | all | one
  scrobbled: false,   // this play counted?
  nowPlayingSent: false,
};

function current() { return state.queue[state.index] || null; }

function save() {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify({
      queue: state.queue.slice(0, 500), index: state.index, repeat: state.repeat,
      position: audio.currentTime || 0, volume: audio.volume,
    }));
  } catch { /* private window / quota: not essential */ }
}

function restore() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORE_KEY) || 'null');
    if (!saved || !Array.isArray(saved.queue)) return;
    state.queue = saved.queue;
    state.index = Math.min(saved.index ?? -1, state.queue.length - 1);
    state.repeat = ['off', 'all', 'one'].includes(saved.repeat) ? saved.repeat : 'off';
    if (typeof saved.volume === 'number') audio.volume = saved.volume;
    const s = current();
    if (s) {
      audio.src = streamUrl(s.id);
      const pos = Number(saved.position) || 0;
      if (pos > 0) audio.addEventListener('loadedmetadata', () => { audio.currentTime = pos; }, { once: true });
      audio.preload = 'metadata';
    }
  } catch { /* corrupt store: start empty */ }
}

function load(index, { autoplay = true } = {}) {
  if (index < 0 || index >= state.queue.length) return;
  state.index = index;
  state.scrobbled = false;
  state.nowPlayingSent = false;
  audio.preload = 'auto';
  audio.src = streamUrl(current().id);
  if (autoplay) audio.play().catch(err => { if (err.name !== 'AbortError') toast(`Music: ${err.message}`); });
  renderPlayer();
  updateMediaSession();
  if (view.name === 'queue') renderView();
  if (view.name === 'lyrics') loadLyrics();
  save();
}

function playList(list, start = 0) {
  if (!list.length) return;
  state.queue = list.map(song);
  load(Math.max(0, Math.min(start, list.length - 1)));
}

function playNext(list) {
  const items = list.map(song);
  if (!current()) return playList(items);
  state.queue.splice(state.index + 1, 0, ...items);
  toast(items.length === 1 ? `Next: ${items[0].title}` : `${items.length} songs play next`);
  renderPlayer(); save();
  if (view.name === 'queue') renderView();
}

function enqueue(list) {
  const items = list.map(song);
  if (!current()) return playList(items);
  state.queue.push(...items);
  toast(items.length === 1 ? `Queued: ${items[0].title}` : `${items.length} songs queued`);
  renderPlayer(); save();
  if (view.name === 'queue') renderView();
}

function toggle() {
  if (!current()) return;
  if (audio.paused) audio.play().catch(err => toast(`Music: ${err.message}`));
  else audio.pause();
}

function next(auto = false) {
  if (!state.queue.length) return;
  if (auto && state.repeat === 'one') { audio.currentTime = 0; audio.play(); return; }
  if (state.index + 1 < state.queue.length) load(state.index + 1);
  else if (state.repeat === 'all') load(0);
  else if (auto) { audio.pause(); audio.currentTime = 0; renderPlayer(); }
}

function prev() {
  if (!current()) return;
  if (audio.currentTime > 3 || state.index === 0) { audio.currentTime = 0; return; }
  load(state.index - 1);
}

function shuffleUpcoming() {
  const head = state.queue.slice(0, state.index + 1);
  const tail = state.queue.slice(state.index + 1);
  for (let i = tail.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [tail[i], tail[j]] = [tail[j], tail[i]];
  }
  state.queue = head.concat(tail);
  save();
}

async function star(item, on, kind = 'id') {
  try {
    await apiPost(on ? 'star' : 'unstar', { [kind]: item.id });
    item.starred = on;
    toast(on ? 'Starred' : 'Unstarred');
  } catch (err) { toast(`Music: ${err.message}`); }
}

// ── Scrobbling ──

function maybeScrobble() {
  const s = current();
  if (!s) return;
  if (!state.nowPlayingSent && audio.currentTime > 1) {
    state.nowPlayingSent = true;
    apiPost('scrobble', { id: s.id, submission: false }).catch(() => {});
  }
  const dur = audio.duration || s.duration || 0;
  if (!state.scrobbled && dur > 30 && (audio.currentTime >= dur / 2 || audio.currentTime >= 240)) {
    state.scrobbled = true;
    apiPost('scrobble', { id: s.id, submission: true, time: Date.now() }).catch(() => {});
  }
}

// ── Media Session ──

function updateMediaSession() {
  if (!('mediaSession' in navigator)) return;
  const s = current();
  if (!s) return;
  try {
    navigator.mediaSession.metadata = new MediaMetadata({
      title: s.title, artist: s.artist, album: s.album,
      artwork: s.coverArt ? [96, 256, 512].map(n => ({ src: cover(s.coverArt, n), sizes: `${n}x${n}` })) : [],
    });
  } catch { /* older browsers */ }
}

function wireMediaSession() {
  if (!('mediaSession' in navigator)) return;
  const set = (action, fn) => { try { navigator.mediaSession.setActionHandler(action, fn); } catch { /* unsupported */ } };
  set('play', () => audio.play());
  set('pause', () => audio.pause());
  set('previoustrack', prev);
  set('nexttrack', () => next());
  set('seekto', (d) => { if (d.seekTime != null) audio.currentTime = d.seekTime; });
  set('seekbackward', (d) => { audio.currentTime = Math.max(0, audio.currentTime - (d.seekOffset || 10)); });
  set('seekforward', (d) => { audio.currentTime += d.seekOffset || 10; });
}

// ── Mini-player (sidebar + rail) ──

const $ = (id) => document.getElementById(id);

function renderPlayer() {
  const mini = $('music-mini');
  const railPlay = $('rail-music-play');
  const s = current();
  const has = !!s;
  if (mini) mini.hidden = !has;
  if (railPlay) railPlay.hidden = !has;
  const playing = has && !audio.paused;
  document.body.classList.toggle('music-playing', playing);
  const eq = $('music-row-eq');
  if (eq) eq.hidden = !playing;
  if (!mini || !has) return;
  mini.querySelector('.music-mini-title').textContent = s.title;
  mini.querySelector('.music-mini-sub').textContent = [s.artist, s.album].filter(Boolean).join(' · ');
  const img = mini.querySelector('.music-mini-art img');
  const src = cover(s.coverArt, 96);
  if (img.getAttribute('src') !== src) {
    if (src) { img.src = src; img.hidden = false; } else { img.removeAttribute('src'); img.hidden = true; }
  }
  renderProgress();
}

function renderProgress() {
  const s = current();
  const dur = audio.duration && Number.isFinite(audio.duration) ? audio.duration : (s?.duration || 0);
  const pos = audio.currentTime || 0;
  const fill = document.querySelector('#music-mini-seek .music-mini-seek-fill');
  if (fill) fill.style.width = dur ? `${Math.min(100, (pos / dur) * 100)}%` : '0%';
  const p = $('music-mini-pos'); if (p) p.textContent = fmt(pos);
  const d = $('music-mini-dur'); if (d) d.textContent = fmt(dur);
  const seek = $('music-mini-seek');
  if (seek) { seek.setAttribute('aria-valuenow', String(Math.round(pos))); seek.setAttribute('aria-valuemax', String(Math.round(dur))); }
  if ('mediaSession' in navigator && dur && navigator.mediaSession.setPositionState) {
    try { navigator.mediaSession.setPositionState({ duration: dur, position: Math.min(pos, dur), playbackRate: audio.playbackRate }); } catch { /* ignore */ }
  }
}

function wireSeek(bar) {
  if (!bar) return;
  const seekTo = (clientX) => {
    const rect = bar.getBoundingClientRect();
    const dur = audio.duration || current()?.duration || 0;
    if (!dur) return;
    audio.currentTime = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width)) * dur;
    renderProgress();
  };
  bar.addEventListener('pointerdown', (e) => {
    seekTo(e.clientX);
    bar.setPointerCapture(e.pointerId);
    const move = (ev) => seekTo(ev.clientX);
    const up = () => { bar.removeEventListener('pointermove', move); bar.removeEventListener('pointerup', up); };
    bar.addEventListener('pointermove', move);
    bar.addEventListener('pointerup', up);
  });
  bar.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowRight') { audio.currentTime += 5; e.preventDefault(); }
    if (e.key === 'ArrowLeft') { audio.currentTime = Math.max(0, audio.currentTime - 5); e.preventDefault(); }
  });
}

// ── Music window ──

const view = { name: 'home', arg: null, history: [] };
let searchTimer = null;
let status = null;

function main() { return $('music-main'); }

function setNav(name) {
  document.querySelectorAll('#music-nav [data-view]').forEach(b => b.classList.toggle('active', b.dataset.view === name));
}

function go(name, arg = null, { push = true } = {}) {
  if (push && (view.name !== name || view.arg !== arg)) view.history.push({ name: view.name, arg: view.arg });
  view.name = name; view.arg = arg;
  setNav(name);
  renderView();
}

function back() {
  const prevView = view.history.pop();
  if (prevView) go(prevView.name, prevView.arg, { push: false });
}

function loading() { main().innerHTML = '<div class="music-empty">Loading…</div>'; }
function failed(err) { main().innerHTML = `<div class="music-empty">${esc(err.message || err)}</div>`; }

const backBtn = () => (view.history.length ? '<button class="music-back" data-act="back">‹ Back</button>' : '');

function albumCard(a) {
  return `<button class="music-card" data-act="album" data-id="${esc(a.id)}" title="${esc(a.name || a.title)}">
    <span class="music-card-art">${a.coverArt ? `<img loading="lazy" src="${cover(a.coverArt, 300)}" alt="">` : ''}</span>
    <span class="music-card-title">${esc(a.name || a.title)}</span>
    <span class="music-card-sub">${esc(a.artist || '')}${a.year ? ` · ${a.year}` : ''}</span>
  </button>`;
}

let listCache = {};   // key -> songs, for row actions
function songRows(list, key, { showAlbum = true, numbered = false, playlistId = null } = {}) {
  listCache[key] = list;
  if (!list.length) return '<div class="music-empty">Nothing here.</div>';
  const cur = current()?.id;
  return `<div class="music-songs">${list.map((s, i) => `
    <div class="music-song${s.id === cur ? ' current' : ''}" data-act="play-from" data-list="${esc(key)}" data-i="${i}">
      <span class="music-song-n">${numbered ? (s.track || i + 1) : `<img loading="lazy" src="${cover(s.coverArt || s.albumId, 64)}" alt="">`}</span>
      <span class="music-song-main"><span class="music-song-title">${esc(s.title)}</span>
        <span class="music-song-sub">${esc(s.artist || '')}${showAlbum && s.album ? ` · ${esc(s.album)}` : ''}</span></span>
      <span class="music-song-actions">
        <button data-act="next" data-list="${esc(key)}" data-i="${i}" title="Play next">⇥</button>
        <button data-act="enqueue" data-list="${esc(key)}" data-i="${i}" title="Add to queue">＋</button>
        <button data-act="to-playlist" data-list="${esc(key)}" data-i="${i}" title="Add to playlist">♫</button>
        <button data-act="star-song" data-list="${esc(key)}" data-i="${i}" title="${s.starred ? 'Unstar' : 'Star'}">${s.starred ? '★' : '☆'}</button>
        ${playlistId ? `<button data-act="pl-remove" data-pl="${esc(playlistId)}" data-i="${i}" title="Remove from this playlist">✕</button>` : ''}
      </span>
      <span class="music-song-dur">${fmt(s.duration)}</span>
    </div>`).join('')}</div>`;
}

function shelf(title, albums) {
  if (!albums?.length) return '';
  return `<section class="music-shelf"><h5>${esc(title)}</h5><div class="music-shelf-row">${albums.map(albumCard).join('')}</div></section>`;
}

async function renderView() {
  const el = main();
  if (!el) return;
  if (!status?.configured && view.name !== 'connection') { return renderConnection(); }
  try {
    switch (view.name) {
      case 'home': return await renderHome();
      case 'search': return await renderSearch(view.arg);
      case 'album': return await renderAlbum(view.arg);
      case 'artist': return await renderArtist(view.arg);
      case 'latest': return await renderLatest();
      case 'playlists': return await renderPlaylists();
      case 'playlist': return await renderPlaylist(view.arg);
      case 'starred': return await renderStarred();
      case 'queue': return renderQueue();
      case 'lyrics': return loadLyrics();
      case 'connection': return renderConnection();
      default: return await renderHome();
    }
  } catch (err) { failed(err); }
}

async function renderHome() {
  loading();
  const lists = await Promise.all(['recent', 'newest', 'frequent', 'random'].map(type =>
    api('getAlbumList2', { type, size: 18 }).then(r => r.albumList2?.album || []).catch(() => [])));
  main().innerHTML = `
    <div class="music-hero">
      <button class="music-pill accent" data-act="shuffle-all">Shuffle everything</button>
      ${current() ? '<button class="music-pill" data-act="go-queue">Up next</button>' : ''}
    </div>
    ${shelf('Recently played', lists[0])}${shelf('Recently added', lists[1])}
    ${shelf('Most played', lists[2])}${shelf('Something random', lists[3])}
    ${lists.every(l => !l.length) ? '<div class="music-empty">The library looks empty.</div>' : ''}`;
}

async function renderSearch(q) {
  if (!q) return renderHome();
  loading();
  const r = (await api('search3', { query: q, artistCount: 8, albumCount: 18, songCount: 40 })).searchResult3 || {};
  if (view.name !== 'search' || view.arg !== q) return;   // a newer search took over
  const artists = r.artist || [], albums = r.album || [], songs = r.song || [];
  main().innerHTML = `
    ${artists.length ? `<section class="music-shelf"><h5>Artists</h5><div class="music-chips">${artists.map(a =>
      `<button class="music-chip" data-act="artist" data-id="${esc(a.id)}">${esc(a.name)}</button>`).join('')}</div></section>` : ''}
    ${shelf('Albums', albums)}
    ${songs.length ? `<section class="music-shelf"><h5>Songs <button class="music-pill small" data-act="play-list" data-list="search">Play all</button></h5>${songRows(songs, 'search')}</section>` : ''}
    ${!artists.length && !albums.length && !songs.length ? `<div class="music-empty">Nothing for “${esc(q)}”.</div>` : ''}`;
}

async function renderAlbum(id) {
  loading();
  const a = (await api('getAlbum', { id })).album;
  const songs = a.song || [];
  main().innerHTML = `${backBtn()}
    <div class="music-head">
      <span class="music-head-art">${a.coverArt ? `<img src="${cover(a.coverArt, 500)}" alt="">` : ''}</span>
      <div class="music-head-meta">
        <div class="music-head-kind">Album${a.year ? ` · ${a.year}` : ''}${a.genre ? ` · ${esc(a.genre)}` : ''}</div>
        <h3>${esc(a.name)}</h3>
        <button class="music-link" data-act="artist" data-id="${esc(a.artistId || '')}">${esc(a.artist || '')}</button>
        <div class="music-head-sub">${songs.length} songs · ${fmt(a.duration)}</div>
        <div class="music-head-btns">
          <button class="music-pill accent" data-act="play-list" data-list="album">Play</button>
          <button class="music-pill" data-act="shuffle-list" data-list="album">Shuffle</button>
          <button class="music-pill" data-act="next-list" data-list="album">Play next</button>
          <button class="music-pill" data-act="enqueue-list" data-list="album">Queue</button>
          <button class="music-pill" data-act="list-to-playlist" data-list="album">Add to playlist</button>
          <button class="music-pill" data-act="star-album" data-id="${esc(a.id)}" data-on="${a.starred ? '1' : ''}">${a.starred ? '★ Starred' : '☆ Star'}</button>
        </div>
      </div>
    </div>
    ${songRows(songs, 'album', { showAlbum: false, numbered: true })}`;
}

async function renderArtist(id) {
  if (!id) return;
  loading();
  const a = (await api('getArtist', { id })).artist;
  const top = await api('getTopSongs', { artist: a.name, count: 10 }).then(r => r.topSongs?.song || []).catch(() => []);
  main().innerHTML = `${backBtn()}
    <div class="music-head compact">
      <span class="music-head-art round">${a.coverArt ? `<img src="${cover(a.coverArt, 300)}" alt="">` : ''}</span>
      <div class="music-head-meta"><div class="music-head-kind">Artist</div><h3>${esc(a.name)}</h3>
        <div class="music-head-btns"><button class="music-pill accent" data-act="artist-shuffle" data-id="${esc(a.id)}">Shuffle artist</button></div></div>
    </div>
    ${top.length ? `<section class="music-shelf"><h5>Top songs</h5>${songRows(top, 'top')}</section>` : ''}
    ${shelf('Albums', a.album || [])}`;
}

const LATEST_PAGE = 60;
async function renderLatest(offset = 0) {
  if (!offset) loading();
  const albums = (await api('getAlbumList2', { type: 'newest', size: LATEST_PAGE, offset })).albumList2?.album || [];
  if (view.name !== 'latest') return;
  const cards = albums.map(a => albumCard(a)).join('');
  const more = albums.length === LATEST_PAGE ? `<div class="music-more"><button class="music-pill" data-act="latest-more" data-offset="${offset + LATEST_PAGE}">More</button></div>` : '';
  if (!offset) {
    main().innerHTML = albums.length
      ? `<div class="music-hero"><button class="music-pill accent" data-act="play-latest">Play the newest</button>
           <span class="music-hero-note">Newest first</span></div><div class="music-grid" id="music-latest-grid">${cards}</div>${more}`
      : '<div class="music-empty">Nothing added yet.</div>';
  } else {
    main().querySelector('.music-more')?.remove();
    main().querySelector('#music-latest-grid')?.insertAdjacentHTML('beforeend', cards);
    main().insertAdjacentHTML('beforeend', more);
  }
}

async function renderPlaylists() {
  loading();
  const lists = (await api('getPlaylists')).playlists?.playlist || [];
  main().innerHTML = lists.length ? `<div class="music-grid">${lists.map(p => `
    <button class="music-card" data-act="playlist" data-id="${esc(p.id)}">
      <span class="music-card-art">${p.coverArt ? `<img loading="lazy" src="${cover(p.coverArt, 300)}" alt="">` : ''}</span>
      <span class="music-card-title">${esc(p.name)}</span>
      <span class="music-card-sub">${p.songCount || 0} songs</span>
    </button>`).join('')}</div>` : '<div class="music-empty">No playlists yet.</div>';
}

async function renderPlaylist(id) {
  loading();
  const p = (await api('getPlaylist', { id })).playlist;
  main().innerHTML = `${backBtn()}
    <div class="music-head compact"><span class="music-head-art">${p.coverArt ? `<img src="${cover(p.coverArt, 300)}" alt="">` : ''}</span>
      <div class="music-head-meta"><div class="music-head-kind">Playlist</div><h3>${esc(p.name)}</h3>
      <div class="music-head-sub">${p.songCount || 0} songs · ${fmt(p.duration)}</div>
      <div class="music-head-btns"><button class="music-pill accent" data-act="play-list" data-list="playlist">Play</button>
      <button class="music-pill" data-act="shuffle-list" data-list="playlist">Shuffle</button></div></div></div>
    ${songRows(p.entry || [], 'playlist', { playlistId: p.id })}`;
}

async function renderStarred() {
  loading();
  const r = (await api('getStarred2')).starred2 || {};
  main().innerHTML = `${shelf('Albums', r.album || [])}
    <section class="music-shelf"><h5>Songs ${(r.song || []).length ? '<button class="music-pill small" data-act="play-list" data-list="starred">Play all</button>' : ''}</h5>
    ${songRows(r.song || [], 'starred')}</section>`;
}

function renderQueue() {
  const q = state.queue;
  const cur = state.index;
  main().innerHTML = `
    <div class="music-hero">
      <button class="music-pill" data-act="shuffle-upcoming" ${q.length > cur + 2 ? '' : 'disabled'}>Shuffle upcoming</button>
      <button class="music-pill" data-act="repeat">Repeat: ${state.repeat}</button>
      <button class="music-pill" data-act="queue-to-playlist" ${q.length ? '' : 'disabled'}>Save as playlist</button>
      <button class="music-pill" data-act="clear-queue" ${q.length ? '' : 'disabled'}>Clear</button>
      <span class="music-hero-note">${q.length} songs · ${fmt(q.reduce((t, s) => t + (s.duration || 0), 0))}</span>
    </div>
    ${q.length ? `<div class="music-songs music-queue" id="music-queue">${q.map((s, i) => `
      <div class="music-song${i === cur ? ' current' : ''}${i < cur ? ' played' : ''}" data-act="jump" data-i="${i}">
        <span class="music-song-n"><span class="music-drag" data-act="noop" title="Drag to reorder">⠿</span>${i === cur ? '▶' : i + 1}</span>
        <span class="music-song-main"><span class="music-song-title">${esc(s.title)}</span><span class="music-song-sub">${esc(s.artist)}${s.album ? ` · ${esc(s.album)}` : ''}</span></span>
        <span class="music-song-actions"><button data-act="remove" data-i="${i}" title="Remove">✕</button></span>
        <span class="music-song-dur">${fmt(s.duration)}</span>
      </div>`).join('')}</div>` : '<div class="music-empty">The queue is empty. Search for something, or shuffle everything from Home.</div>'}`;
  main().querySelector('.music-song.current')?.scrollIntoView({ block: 'center' });
}

// Lyrics (OpenSubsonic getLyricsBySongId: synced lines when the file has them)
let lyrics = { songId: null, lines: [], synced: false };
async function loadLyrics() {
  if (view.name !== 'lyrics') return;
  const s = current();
  if (!s) { main().innerHTML = '<div class="music-empty">Play something to see its lyrics.</div>'; return; }
  if (lyrics.songId !== s.id) {
    main().innerHTML = '<div class="music-empty">Looking for lyrics…</div>';
    lyrics = { songId: s.id, lines: [], synced: false };
    try {
      const list = (await api('getLyricsBySongId', { id: s.id })).lyricsList?.structuredLyrics || [];
      const pick = list.find(l => l.synced) || list[0];
      if (pick) lyrics = { songId: s.id, synced: !!pick.synced, lines: (pick.line || []).map(l => ({ t: (l.start ?? -1) / 1000, text: l.value || '' })) };
    } catch { /* none */ }
    if (current()?.id !== s.id || view.name !== 'lyrics') return;
  }
  main().innerHTML = lyrics.lines.length
    ? `<div class="music-lyrics${lyrics.synced ? ' synced' : ''}"><div class="music-lyrics-head">${esc(s.title)} · ${esc(s.artist)}</div>${lyrics.lines.map((l, i) =>
      `<p data-i="${i}" ${lyrics.synced ? `data-act="lyric-seek" data-t="${l.t}"` : ''}>${esc(l.text) || '&nbsp;'}</p>`).join('')}</div>`
    : `<div class="music-empty">No lyrics for “${esc(s.title)}” in the library.</div>`;
  highlightLyric(true);
}

let lyricIdx = -1;
function highlightLyric(force = false) {
  if (view.name !== 'lyrics' || !lyrics.synced || lyrics.songId !== current()?.id) return;
  const t = audio.currentTime;
  let idx = -1;
  for (let i = 0; i < lyrics.lines.length; i++) { if (lyrics.lines[i].t <= t + 0.15) idx = i; else break; }
  if (idx === lyricIdx && !force) return;
  lyricIdx = idx;
  const box = main().querySelector('.music-lyrics');
  if (!box) return;
  box.querySelectorAll('p.on').forEach(p => p.classList.remove('on'));
  const p = box.querySelector(`p[data-i="${idx}"]`);
  if (p) { p.classList.add('on'); p.scrollIntoView({ block: 'center', behavior: force ? 'auto' : 'smooth' }); }
}

function renderConnection() {
  setNav('connection');
  const s = status || {};
  main().innerHTML = `
    <div class="music-connect">
      <h3>Navidrome</h3>
      <p class="music-connect-note">${s.configured
        ? (s.ok ? `Connected to <b>${esc(s.url)}</b> as <b>${esc(s.user)}</b> (Navidrome ${esc(s.server || '?')}).` : `Saved, but Navidrome isn't answering: ${esc(s.error || 'unknown error')}`)
        : 'Connect Mabel to your Navidrome server. The password is stored encrypted in Mabel and never sent to the browser.'}</p>
      <form id="music-connect-form" autocomplete="off">
        <label>Address<input name="url" placeholder="http://host:4533" value="${esc(s.url || '')}" required></label>
        <label>User<input name="user" value="${esc(s.user || '')}" required autocomplete="username"></label>
        <label>Password<input name="password" type="password" placeholder="${s.configured ? '(unchanged)' : ''}" autocomplete="new-password"></label>
        <div class="music-head-btns">
          <button class="music-pill accent" type="submit">${s.configured ? 'Save' : 'Connect'}</button>
          ${s.configured ? '<button class="music-pill" type="button" data-act="disconnect">Disconnect</button>' : ''}
        </div>
      </form>
    </div>`;
  $('music-connect-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const btn = e.target.querySelector('[type=submit]');
    btn.disabled = true; btn.textContent = 'Testing…';
    try {
      const r = await fetch('/api/music/config', {
        method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: f.get('url'), user: f.get('user'), password: f.get('password') || '' }),
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
      toast(`Music connected (Navidrome ${body.server || ''})`);
      await refreshStatus();
      go('home', null, { push: false });
    } catch (err) {
      toast(`Music: ${err.message}`);
      btn.disabled = false; btn.textContent = s.configured ? 'Save' : 'Connect';
    }
  });
}

async function refreshStatus() {
  try {
    const r = await fetch('/api/music/status', { credentials: 'same-origin' });
    status = r.ok ? await r.json() : { configured: false };
  } catch { status = { configured: false }; }
  return status;
}

// ── Window actions (one delegated handler) ──

async function onMainClick(e) {
  const t = e.target.closest('[data-act]');
  if (!t) return;
  const act = t.dataset.act;
  const list = t.dataset.list ? (listCache[t.dataset.list] || []) : [];
  const i = Number(t.dataset.i);
  e.stopPropagation();
  try {
    switch (act) {
      case 'back': return back();
      case 'album': return go('album', t.dataset.id);
      case 'artist': return t.dataset.id && go('artist', t.dataset.id);
      case 'playlist': return go('playlist', t.dataset.id);
      case 'go-queue': return go('queue');
      case 'play-from': return playList(list, i);
      case 'next': return playNext([list[i]]);
      case 'enqueue': return enqueue([list[i]]);
      case 'star-song': {
        const s = list[i]; await star(s, !s.starred);
        t.textContent = s.starred ? '★' : '☆'; t.title = s.starred ? 'Unstar' : 'Star';
        const q = current(); if (q && q.id === s.id) q.starred = s.starred;
        return;
      }
      case 'star-album': {
        const on = !t.dataset.on; await star({ id: t.dataset.id }, on, 'albumId');
        t.dataset.on = on ? '1' : ''; t.textContent = on ? '★ Starred' : '☆ Star'; return;
      }
      case 'play-list': return playList(list, 0);
      case 'shuffle-list': { const l = [...list]; for (let k = l.length - 1; k > 0; k--) { const j = Math.floor(Math.random() * (k + 1)); [l[k], l[j]] = [l[j], l[k]]; } return playList(l, 0); }
      case 'next-list': return playNext(list);
      case 'enqueue-list': return enqueue(list);
      case 'shuffle-all': return playList((await api('getRandomSongs', { size: 100 })).randomSongs?.song || []);
      case 'artist-shuffle': {
        const a = (await api('getArtist', { id: t.dataset.id })).artist;
        const albums = await Promise.all((a.album || []).slice(0, 30).map(al => api('getAlbum', { id: al.id }).then(r => r.album.song || []).catch(() => [])));
        const songs = albums.flat();
        for (let k = songs.length - 1; k > 0; k--) { const j = Math.floor(Math.random() * (k + 1)); [songs[k], songs[j]] = [songs[j], songs[k]]; }
        return playList(songs);
      }
      case 'jump': return load(i);
      case 'noop': return;
      case 'latest-more': return renderLatest(Number(t.dataset.offset) || 0);
      case 'play-latest': {
        const albums = (await api('getAlbumList2', { type: 'newest', size: 5 })).albumList2?.album || [];
        const songs = (await Promise.all(albums.map(a => api('getAlbum', { id: a.id }).then(r => r.album.song || []).catch(() => [])))).flat();
        return playList(songs);
      }
      case 'to-playlist': return playlistPicker(t, [list[i]]);
      case 'list-to-playlist': return playlistPicker(t, list);
      case 'queue-to-playlist': return playlistPicker(t, state.queue, { suggest: 'Queue ' + new Date().toLocaleDateString() });
      case 'pl-remove': {
        await apiPost('updatePlaylist', { playlistId: t.dataset.pl, songIndexToRemove: i });
        toast('Removed from playlist');
        return renderPlaylist(t.dataset.pl);
      }
      case 'remove': {
        state.queue.splice(i, 1);
        if (i < state.index) state.index -= 1;
        else if (i === state.index) {
          if (state.queue.length) load(Math.min(i, state.queue.length - 1), { autoplay: !audio.paused });
          else { audio.pause(); audio.removeAttribute('src'); state.index = -1; }
        }
        renderPlayer(); save(); return renderQueue();
      }
      case 'clear-queue':
        audio.pause(); audio.removeAttribute('src'); audio.load();
        state.queue = []; state.index = -1; renderPlayer(); save(); return renderQueue();
      case 'shuffle-upcoming': shuffleUpcoming(); return renderQueue();
      case 'repeat':
        state.repeat = { off: 'all', all: 'one', one: 'off' }[state.repeat]; save(); return renderQueue();
      case 'lyric-seek': audio.currentTime = Number(t.dataset.t) || 0; return;
      case 'disconnect':
        if (!confirm('Disconnect Mabel from Navidrome? The saved login is removed.')) return;
        await fetch('/api/music/config', { method: 'DELETE', credentials: 'same-origin' });
        await refreshStatus(); return renderConnection();
      default: return;
    }
  } catch (err) { toast(`Music: ${err.message}`); }
}

// ── Add to playlist ──

function closePicker() { document.querySelector('.music-picker')?.remove(); }

async function playlistPicker(anchor, songs, { suggest = '' } = {}) {
  closePicker();
  const ids = songs.map(x => x.id).filter(Boolean);
  if (!ids.length) return;
  const box = document.createElement('div');
  box.className = 'music-picker';
  box.innerHTML = '<div class="music-picker-title">Add to playlist</div><div class="music-empty small">Loading…</div>';
  const host = $('music-modal')?.querySelector('.modal-content') || document.body;
  host.appendChild(box);
  const a = anchor.getBoundingClientRect(), h = host.getBoundingClientRect();
  box.style.left = `${Math.max(8, Math.min(a.left - h.left, h.width - 230))}px`;
  box.style.top = `${Math.min(a.bottom - h.top + 4, h.height - 60)}px`;
  let lists = [];
  try { lists = (await api('getPlaylists')).playlists?.playlist || []; } catch (err) { toast(`Music: ${err.message}`); }
  box.innerHTML = `<div class="music-picker-title">Add ${ids.length === 1 ? 'song' : `${ids.length} songs`} to</div>
    <button data-pick="new">＋ New playlist…</button>
    ${lists.map(p => `<button data-pick="${esc(p.id)}">${esc(p.name)} <span>${p.songCount || 0}</span></button>`).join('')}`;
  box.addEventListener('click', async (e) => {
    const b = e.target.closest('[data-pick]');
    if (!b) return;
    e.stopPropagation();
    try {
      if (b.dataset.pick === 'new') {
        const name = (prompt('Name for the new playlist', suggest) || '').trim();
        if (!name) return;
        await apiPost('createPlaylist', { name, songId: ids });
        toast(`Created “${name}”`);
      } else {
        await apiPost('updatePlaylist', { playlistId: b.dataset.pick, songIdToAdd: ids });
        toast(`Added to “${b.textContent.replace(/\s+\d+$/, '').trim()}”`);
      }
      closePicker();
      if (view.name === 'playlists') renderPlaylists();
    } catch (err) { toast(`Music: ${err.message}`); }
  });
}

// ── Queue drag-to-reorder (pointer events: mouse and touch) ──

function moveInQueue(from, to) {
  if (from === to || from < 0 || to < 0 || from >= state.queue.length || to >= state.queue.length) return;
  const [item] = state.queue.splice(from, 1);
  state.queue.splice(to, 0, item);
  if (state.index === from) state.index = to;
  else if (from < state.index && to >= state.index) state.index -= 1;
  else if (from > state.index && to <= state.index) state.index += 1;
  save();
}

function wireQueueDrag(container) {
  container.addEventListener('pointerdown', (e) => {
    const handle = e.target.closest('.music-drag');
    const list = handle && main()?.querySelector('#music-queue');
    if (!list) return;
    const row = handle.closest('.music-song');
    const rows = [...list.querySelectorAll('.music-song')];
    const from = rows.indexOf(row);
    if (from < 0) return;
    e.preventDefault();
    handle.setPointerCapture(e.pointerId);
    row.classList.add('dragging');
    let before = from;
    const scroller = main();
    let scrollTimer = null;
    const mark = (clientY) => {
      rows.forEach(r => r.classList.remove('drop-before', 'drop-after'));
      before = rows.length;
      for (let k = 0; k < rows.length; k++) {
        const r = rows[k].getBoundingClientRect();
        if (clientY < r.top + r.height / 2) { before = k; break; }
      }
      if (before < rows.length) rows[before].classList.add('drop-before');
      else rows[rows.length - 1].classList.add('drop-after');
      const box = scroller.getBoundingClientRect();
      clearInterval(scrollTimer);
      const edge = clientY < box.top + 40 ? -8 : clientY > box.bottom - 40 ? 8 : 0;
      if (edge) scrollTimer = setInterval(() => { scroller.scrollTop += edge; }, 16);
    };
    const move = (ev) => mark(ev.clientY);
    const up = () => {
      clearInterval(scrollTimer);
      handle.removeEventListener('pointermove', move);
      handle.removeEventListener('pointerup', up);
      handle.removeEventListener('pointercancel', up);
      row.classList.remove('dragging');
      const to = before > from ? before - 1 : before;
      if (to !== from) { moveInQueue(from, to); renderPlayer(); }
      renderQueue();
    };
    handle.addEventListener('pointermove', move);
    handle.addEventListener('pointerup', up);
    handle.addEventListener('pointercancel', up);
  });
}

// ── Window open/close ──

let dragWired = false;
function wireDrag(modal) {
  if (dragWired) return;
  const content = modal.querySelector('.modal-content');
  const header = modal.querySelector('.modal-header');
  if (!content || !header) return;
  dragWired = true;
  makeWindowDraggable(modal, { content, header, skipSelector: 'button, a, input, select, label', enableDock: true, enableLeftDock: true });
}

async function openMusic(target = null) {
  const modal = $('music-modal');
  if (!modal) return;
  const wasHidden = modal.classList.contains('hidden');
  modal.classList.remove('hidden', 'modal-minimized');
  modal.style.display = '';
  wireDrag(modal);
  if (!status) await refreshStatus();
  if (target) go(target);
  else if (wasHidden && !main().childElementCount) renderView();
  if (status?.configured) setTimeout(() => $('music-search')?.focus({ preventScroll: true }), 50);
}

function closeMusic() { $('music-modal')?.classList.add('hidden'); }

function toggleMusic() {
  const modal = $('music-modal');
  if (!modal) return;
  if (modal.classList.contains('hidden') || modal.classList.contains('modal-minimized')) openMusic();
  else closeMusic();
}

// ── Init ──

function init() {
  restore();
  wireMediaSession();

  audio.addEventListener('play', () => { renderPlayer(); });
  audio.addEventListener('pause', () => { renderPlayer(); save(); });
  audio.addEventListener('ended', () => next(true));
  audio.addEventListener('timeupdate', () => { renderProgress(); maybeScrobble(); highlightLyric(); });
  audio.addEventListener('loadedmetadata', renderProgress);
  audio.addEventListener('volumechange', () => { const v = $('music-volume'); if (v) v.value = String(audio.volume); save(); });
  audio.addEventListener('error', () => {
    if (!current() || !audio.getAttribute('src')) return;
    toast(`Music: couldn't play “${current().title}”`);
  });
  setInterval(() => { if (!audio.paused) save(); }, 10000);
  window.addEventListener('pagehide', save);

  // Sidebar + rail
  $('tool-music-btn')?.addEventListener('click', toggleMusic);
  document.querySelectorAll('#music-mini [data-music]').forEach(b => b.addEventListener('click', (e) => {
    e.stopPropagation();
    ({ prev, toggle, next: () => next() })[b.dataset.music]?.();
  }));
  $('music-mini-art')?.addEventListener('click', () => openMusic());
  $('music-mini-meta')?.addEventListener('click', () => openMusic('queue'));
  $('rail-music-play')?.addEventListener('click', toggle);
  wireSeek($('music-mini-seek'));

  // Window
  const modal = $('music-modal');
  if (modal) {
    $('close-music-modal')?.addEventListener('click', closeMusic);
    modal.addEventListener('click', (e) => { if (e.target === modal) closeMusic(); });
    main()?.addEventListener('click', onMainClick);
    if (main()) wireQueueDrag(main());
    document.addEventListener('pointerdown', (e) => {
      if (!e.target.closest('.music-picker, [data-act="to-playlist"], [data-act="list-to-playlist"], [data-act="queue-to-playlist"]')) closePicker();
    });
    $('music-nav')?.addEventListener('click', (e) => {
      const b = e.target.closest('[data-view]');
      if (b) { view.history = []; go(b.dataset.view, null, { push: false }); }
    });
    const vol = $('music-volume');
    if (vol) { vol.value = String(audio.volume); vol.addEventListener('input', () => { audio.volume = Number(vol.value); }); }
    const search = $('music-search');
    search?.addEventListener('input', () => {
      clearTimeout(searchTimer);
      const q = search.value.trim();
      searchTimer = setTimeout(() => {
        if (!q) { if (view.name === 'search') go('home', null, { push: false }); return; }
        if (view.name === 'search') { view.arg = q; renderView(); } else go('search', q);
      }, 250);
    });
    search?.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        const first = listCache.search?.length && view.name === 'search' ? listCache.search : null;
        if (first) playList(first, 0);
      }
      if (e.key === 'Escape' && search.value) { search.value = ''; search.dispatchEvent(new Event('input')); e.stopPropagation(); }
    });
  }

  // Mabel's control_music tool (via chatStream.handleUIControl).
  const seen = new Set();
  window.addEventListener('mabel-music-command', (e) => {
    const cmd = e.detail || {};
    if (!cmd.op || (cmd.nonce && seen.has(cmd.nonce))) return;
    if (cmd.nonce) seen.add(cmd.nonce);
    const songs = Array.isArray(cmd.songs) ? cmd.songs.filter(s => s && s.id) : [];
    switch (cmd.op) {
      case 'play': if (songs.length) playList(songs, 0); break;
      case 'queue': if (songs.length) enqueue(songs); break;
      case 'play_next': if (songs.length) playNext(songs); break;
      case 'pause': audio.pause(); break;
      case 'resume': if (current()) audio.play().catch(err => toast(`Music: ${err.message}`)); break;
      case 'next': next(); break;
      case 'previous': prev(); break;
      default: break;
    }
  });

  // Space toggles play when focus isn't in a text field and the Music window is up.
  document.addEventListener('keydown', (e) => {
    if (e.code !== 'Space' || e.target.closest('input, textarea, [contenteditable="true"], button')) return;
    if ($('music-modal')?.classList.contains('hidden')) return;
    e.preventDefault(); toggle();
  });

  renderPlayer();
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

export default { openMusic, toggle, next, prev, playList, enqueue };
