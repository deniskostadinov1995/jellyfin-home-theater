/* ═══════════════════════════════════════════════════════════════════════
   Jellyfin Home Theater — TV Display Logic
   Handles: Jellyfin API, navigation, video playback, WebSocket remote control
═══════════════════════════════════════════════════════════════════════ */

// ── API Helpers ────────────────────────────────────────────────────────────
async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`API error ${r.status}: ${path}`);
  return r.json();
}

async function apiPost(path, body) {
  const r = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  if (!r.ok) throw new Error(`API error ${r.status}: ${path}`);
  return r.json();
}

function imgUrl(thumbPath, w = 300, h = 450) {
  if (!thumbPath) return '';
  return `/img?path=${encodeURIComponent(thumbPath)}&w=${w}&h=${h}`;
}

function formatTime(ms) {
  if (!ms || isNaN(ms)) return '0:00';
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60);
  const h = Math.floor(m / 60);
  const ss = String(s % 60).padStart(2, '0');
  const mm = String(m % 60).padStart(2, '0');
  return h > 0 ? `${h}:${mm}:${ss}` : `${m}:${ss}`;
}

// ── App State ──────────────────────────────────────────────────────────────
const App = {
  screen:        'home',    // home | library | detail | player | search
  heroItem:      null,
  detailItem:    null,
  currentSeason: null,
  currentLib:    null,
  libraries:     [],
  // 2D focus grid for D-pad navigation
  focusGrid:     [],
  focusRow:      0,
  focusCol:      0,
  // Stack of saved focus states for restoring when modals close
  focusStack:    [],
  // Window width the current focusGrid was derived at; a mismatch means the
  // layout re-flowed and the row grouping needs rebuilding.
  gridBuiltWidth: null,
  // Pop-out sidebar nav (home screen)
  sidebarOpen:   false,
  sidebarIdx:    1,              // default highlight = Home
  sidebarItems:  [],
  overlayTimer:  null,
  hls:           null,
  history:       [],
  // Library browsing state
  libFilters:    { sort: 'titleSort', genre: 'all', decade: 'all', unwatched: '0' },
  libItems:      [],
  // Player state
  currentItem:   null,
  streamUrls:    null,           // last fetched stream URLs
  streamDuration: 0,
  audioTracks:   [],
  subTracks:     [],
  partId:        null,
  markers:       [],             // intro/credits markers for current item
  activeMarker:  null,           // marker the playhead is currently inside
  playbackMode:  null,           // 'direct' | 'directStream' | 'transcode'
  forceNextMode: null,           // one-shot mode override for the next playItem
  sideSubId:     0,              // id of the currently sideloaded subtitle track
  // YouTube playback: {id, duration (ms), offset (s)} — offset tracks where
  // the current remux pipe started, since pipes aren't natively seekable
  ytPlayback:    null,
  // Screen aspect / zoom mode — keeps black bars off the screen so SignalRGB's
  // capture always sees real picture. 'auto' | 'fit' | 'fill' | 'zoom'
  aspectMode:    'auto',
  currentQualityResolution: 'original',  // Maximum (original) for local playback, like real Jellyfin
  // A title can exist as several files (e.g. a 1080p rip next to a 4K remux).
  // null = let the backend pick the best one; otherwise a media-source id.
  currentMediaSource: null,
  streamVersions: [],
  // Next-up countdown
  nextUpTimer:   null,
  nextUpInterval: null,
  // Screensaver
  ssArt:         [],
  ssIdleTimer:   null,
  ssActive:      false,
  ssRotateTimer: null,
  ssClockTimer:  null,
  IDLE_MS:       3 * 60 * 1000,  // 3 minutes
  // Modal tracking — when any of these are open, focus/back behave differently
  modalOpen:     null,
};

// ── Focus / Navigation System ──────────────────────────────────────────────
function setFocus(rowIdx, colIdx) {
  document.querySelectorAll('.focused').forEach(el => el.classList.remove('focused'));

  const grid = App.focusGrid;
  // Stamp the width this grid layout corresponds to, so navigation can tell
  // when the window has changed underneath it.
  App.gridBuiltWidth = window.innerWidth;
  if (!grid.length) return;

  rowIdx = Math.max(0, Math.min(rowIdx, grid.length - 1));
  const row = grid[rowIdx] || [];
  colIdx = Math.max(0, Math.min(colIdx, row.length - 1));

  App.focusRow = rowIdx;
  App.focusCol = colIdx;

  const el = grid[rowIdx][colIdx];
  if (el) {
    el.classList.add('focused');
    el.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'smooth' });
  }
}

function navigate(dir) {
  // Sidebar captures navigation while it's open
  if (App.sidebarOpen) {
    if (dir === 'up')    setSidebarFocus(App.sidebarIdx - 1);
    else if (dir === 'down') setSidebarFocus(App.sidebarIdx + 1);
    else if (dir === 'right') closeSidebar();
    // left: already at the edge, stay put
    return;
  }

  // A resize (or the fullscreen toggle) re-flows the grids; make sure the row
  // grouping matches the screen before acting on a direction.
  ensureFocusMatchesLayout();

  let { focusRow: r, focusCol: c } = App;
  const grid = App.focusGrid;

  // Pressing LEFT at the first column on the home screen opens the sidebar
  if (dir === 'left' && c === 0 && canUseSidebar()) {
    openSidebar();
    return;
  }

  if (dir === 'up')    r--;
  if (dir === 'down')  r++;
  if (dir === 'left')  c--;
  if (dir === 'right') c++;

  if (grid[r]) c = Math.max(0, Math.min(c, grid[r].length - 1));

  setFocus(r, c);
}

function selectFocused() {
  if (App.sidebarOpen) { App.sidebarItems[App.sidebarIdx]?.click(); return; }
  const el = App.focusGrid[App.focusRow]?.[App.focusCol];
  if (el) el.click();
}

// ── Sidebar (pop-out nav) ──────────────────────────────────────────────────
function canUseSidebar() {
  return App.screen === 'home' && !App.modalOpen && !App.ssActive &&
         App.sidebarItems.length > 0;
}

function buildSidebar(libs) {
  const wrap = document.getElementById('sb-libraries');
  wrap.innerHTML = '';
  const iconFor = t => t === 'movie' ? '🎬' : t === 'show' ? '📺'
                     : t === 'artist' ? '🎵' : t === 'photo' ? '📷' : '📁';
  (libs || []).forEach(lib => {
    const el = document.createElement('div');
    el.className = 'sb-item';
    el.dataset.sb = 'library';
    el.innerHTML =
      `<span class="sb-icon">${iconFor(lib.type)}</span>` +
      `<span class="sb-label">${lib.title}</span>`;
    el.addEventListener('click', () => { closeSidebar(); openLibrary(lib); });
    wrap.appendChild(el);
  });

  const wire = (sel, fn) => { const e = document.querySelector(sel); if (e) e.onclick = fn; };
  wire('[data-sb="search"]',  () => { closeSidebar(); openSearch(); });
  wire('[data-sb="home"]',    () => { closeSidebar(); });
  wire('[data-sb="youtube"]', () => { closeSidebar(); openYouTube(); });

  App.sidebarItems = [...document.querySelectorAll('#sidebar .sb-item')];
}

function openSidebar() {
  if (!canUseSidebar()) return;
  App.sidebarOpen = true;
  document.getElementById('sidebar').classList.add('open');
  document.getElementById('sidebar').setAttribute('aria-hidden', 'false');
  document.body.classList.add('sidebar-open');
  document.querySelectorAll('.focused').forEach(el => el.classList.remove('focused'));
  setSidebarFocus(App.sidebarIdx);
}

function closeSidebar(restoreFocus = true) {
  if (!App.sidebarOpen && restoreFocus) return;
  App.sidebarOpen = false;
  document.getElementById('sidebar').classList.remove('open');
  document.getElementById('sidebar').setAttribute('aria-hidden', 'true');
  document.body.classList.remove('sidebar-open');
  App.sidebarItems.forEach(el => el.classList.remove('sb-focused'));
  if (restoreFocus) setFocus(App.focusRow, App.focusCol);
}

function setSidebarFocus(idx) {
  const n = App.sidebarItems.length;
  if (!n) return;
  idx = Math.max(0, Math.min(idx, n - 1));
  App.sidebarIdx = idx;
  App.sidebarItems.forEach((el, i) => el.classList.toggle('sb-focused', i === idx));
  App.sidebarItems[idx]?.scrollIntoView({ block: 'nearest' });
}

/**
 * Group cards into rows exactly as the browser laid them out.
 *
 * The library and search grids use `grid-template-columns: repeat(auto-fill,
 * minmax(…))`, so the column count follows the viewport — 6 across a 1280px
 * window, 10 across a 1080p TV. Any fixed number makes the D-pad disagree
 * with the screen: with 10 real columns and 8 assumed, "Down" moves 8 cards
 * forward, which is still inside the same visual row. That is what made
 * navigation feel arbitrary. Reading the rendered geometry keeps the focus
 * model and the picture in step at any size.
 *
 * `offsetTop` is used rather than getBoundingClientRect so the grouping does
 * not shift as the grid scrolls.
 */
function rowsFromLayout(cards) {
  const rows = [];
  let row = null, rowTop = null;
  for (const card of cards) {
    const top = card.offsetTop;
    // A couple of pixels of tolerance: sub-pixel grid maths can nudge
    // siblings that are visually on the same line.
    if (row === null || Math.abs(top - rowTop) > 3) {
      row = [];
      rows.push(row);
      rowTop = top;
    }
    row.push(card);
  }
  return rows;
}

// Screen name → the function that builds its focus grid.
const GRID_BUILDERS = {
  home:    () => buildHomeGrid(),
  library: () => buildLibraryGrid(),
  detail:  () => buildDetailGrid(),
  player:  () => buildPlayerGrid(),
  search:  () => buildSearchGrid(),
  youtube: () => buildYtGrid(),
};

/**
 * Re-derive the focus grid after the layout changes.
 *
 * The auto-fill grids change their column count with the window, and a grid
 * built at one width navigates wrongly at another. Pressing F for fullscreen
 * is precisely that kind of change, which is why navigation could go astray
 * partway through a session. Focus is put back on whatever card the user was
 * on, at its new coordinates.
 */
function rebuildFocusForLayout() {
  if (App.modalOpen || App.sidebarOpen) return;   // those manage their own focus
  const builder = GRID_BUILDERS[App.screen];
  if (!builder) return;

  const focused = App.focusGrid[App.focusRow]?.[App.focusCol] || null;
  builder();
  if (!focused) return;
  for (let r = 0; r < App.focusGrid.length; r++) {
    const c = App.focusGrid[r].indexOf(focused);
    if (c >= 0) { setFocus(r, c); return; }
  }
}

let layoutRebuildTimer;
function scheduleFocusRebuild() {
  clearTimeout(layoutRebuildTimer);
  layoutRebuildTimer = setTimeout(rebuildFocusForLayout, 150);
}

// Rebuild when the window changes size — covers resizing and the fullscreen
// toggle, both of which re-flow the auto-fill grids.
if (window.ResizeObserver) {
  new ResizeObserver(scheduleFocusRebuild).observe(document.body);
}
window.addEventListener('resize', scheduleFocusRebuild);

/**
 * Last resort: notice a stale grid at the moment it would matter.
 *
 * Every grid records the width it was built at. If the window is a different
 * width by the time someone presses a direction, the row grouping predates
 * the current layout and gets rebuilt before the move is applied. This costs
 * one integer comparison per keypress and means navigation cannot stay broken
 * even if no resize notification ever arrives.
 */
function ensureFocusMatchesLayout() {
  if (App.gridBuiltWidth === window.innerWidth) return;
  if (App.modalOpen || App.sidebarOpen) return;
  rebuildFocusForLayout();
}

function pushFocus() {
  App.focusStack.push({ grid: App.focusGrid, r: App.focusRow, c: App.focusCol });
}
function popFocus() {
  const f = App.focusStack.pop();
  if (!f) return;
  App.focusGrid = f.grid;
  setFocus(f.r, f.c);
}

function goBack() {
  // If the sidebar is open, close it first
  if (App.sidebarOpen) { closeSidebar(); return; }
  // If a modal is open, close it first
  if (App.modalOpen) {
    closeModal();
    return;
  }
  // If screensaver is up, dismiss it
  if (App.ssActive) { hideScreensaver(); return; }

  if (App.screen === 'player') {
    stopPlayback();
  }
  const prev = App.history.pop();
  showScreen(prev || 'home', { _isBack: true });
}

// ── Screen Management ──────────────────────────────────────────────────────
function showScreen(name, opts = {}) {
  if (!opts._isBack && App.screen && App.screen !== name) {
    App.history.push(App.screen);
    if (App.history.length > 20) App.history.shift();
  }

  // Leaving home closes the sidebar (without trying to restore home focus,
  // since the new screen builds its own grid).
  if (App.sidebarOpen && name !== 'home') closeSidebar(false);

  document.querySelectorAll('.screen').forEach(s => s.classList.add('hidden'));
  const el = document.getElementById(`screen-${name}`);
  if (el) el.classList.remove('hidden');
  App.screen = name;
  // Hide the "Remote Connected" badge during playback — it shouldn't sit over
  // the video. It stays visible on every other screen.
  document.body.classList.toggle('playing', name === 'player');
  // The sidebar only exists on the home screen.
  document.body.classList.toggle('on-home', name === 'home');
  App.focusGrid = [];
  App.focusRow = 0;
  App.focusCol = 0;

  if (name === 'home')    buildHomeGrid();
  if (name === 'library') buildLibraryGrid();
  if (name === 'detail')  buildDetailGrid();
  if (name === 'player')  buildPlayerGrid();
  if (name === 'search')  buildSearchGrid();
  if (name === 'youtube') buildYtGrid();

  broadcastState();
  bumpIdle();   // any screen change resets the screensaver timer
}

// ── HOME SCREEN ────────────────────────────────────────────────────────────
async function loadHome() {
  try {
    const [onDeck, recent, libs] = await Promise.all([
      api('/api/on_deck'),
      api('/api/recently_added'),
      api('/api/libraries'),
    ]);

    App.libraries = libs;
    buildSidebar(libs);

    // Hero item
    const hero = onDeck[0] || recent[0];
    if (hero) renderHero(hero);

    // Continue Watching row
    renderRow('items-continue', onDeck.slice(0, 20), true);

    // Recently Added row (show watch indicators)
    renderRow('items-recent', recent.slice(0, 20), false, true);

    // Library rows
    const libContainer = document.getElementById('library-rows');
    libContainer.innerHTML = '';

    for (const lib of libs) {
      const items = await api(`/api/library/${lib.key}/all?size=20`);
      const wrap = document.createElement('div');
      wrap.className = 'row-section';
      wrap.innerHTML = `
        <div class="row-header">
          <h2 class="row-title">${lib.title}</h2>
          <button class="see-all-btn focusable" data-libkey="${lib.key}">Browse All →</button>
        </div>
        <div class="row-items" id="items-lib-${lib.key}"></div>
      `;
      libContainer.appendChild(wrap);
      renderRow(`items-lib-${lib.key}`, items.slice(0, 20), false, true);

      wrap.querySelector('.see-all-btn').addEventListener('click', () => openLibrary(lib));
    }

    // Collections & Playlists — load lazily, hide if empty
    loadCollectionsAndPlaylists();

    // Pre-fetch backdrops for screensaver
    api('/api/random_art?n=40').then(arr => { App.ssArt = arr || []; }).catch(()=>{});

    buildHomeGrid();
    hideLoading();
  } catch (e) {
    console.error('loadHome error:', e);
    showToast('⚠ Could not load your Jellyfin library. Check config.py');
    hideLoading();
  }
}

async function loadCollectionsAndPlaylists() {
  try {
    const cols = await api('/api/collections');
    if (cols && cols.length) {
      document.getElementById('row-collections').classList.remove('hidden');
      renderRow('items-collections', cols.slice(0, 20), false, false, 'collection');
    }
  } catch (_) {}
  try {
    const pls = await api('/api/playlists');
    if (pls && pls.length) {
      document.getElementById('row-playlists').classList.remove('hidden');
      renderRow('items-playlists', pls.slice(0, 20), false, false, 'playlist');
    }
  } catch (_) {}
  buildHomeGrid();
}

function renderHero(item) {
  App.heroItem = item;
  const backdrop = document.getElementById('hero-backdrop');
  if (item.art) {
    backdrop.style.backgroundImage = `url("${imgUrl(item.art, 1920, 1080)}")`;
  }
  // Episodes are billed by their show, with the episode as context
  const isEp = item.type === 'episode';
  document.getElementById('hero-title').textContent =
    (isEp ? item.grandparentTitle : item.title) || item.title || '';
  const epInfo = isEp
    ? `S${item.parentIndex ?? '?'} E${item.index ?? '?'} · ${item.title || ''}`
    : '';
  const year  = item.year || '';
  const dur   = item.duration ? formatTime(item.duration) : '';
  const rating = item.contentRating || '';
  document.getElementById('hero-meta').textContent =
    [epInfo, year, dur, rating].filter(Boolean).join(' · ');
  document.getElementById('hero-summary').textContent = item.summary || '';

  const badge = document.getElementById('hero-badge');
  badge.textContent = item.viewOffset ? 'CONTINUE WATCHING'
    : item.type === 'episode' ? 'NEXT EPISODE' : 'FEATURED';
}

/**
 * @param showProgress  show a progress fill for Continue Watching
 * @param showWatched   draw a watched checkmark + partial bar based on viewCount/viewOffset
 * @param kind          'item' | 'collection' | 'playlist'
 */
function renderRow(containerId, items, showProgress = false, showWatched = false, kind = 'item') {
  const container = document.getElementById(containerId);
  if (!container || !items?.length) return;
  container.innerHTML = '';

  items.forEach(item => {
    const card = createPosterCard(item, { showProgress, showWatched, kind });
    container.appendChild(card);
  });
}

// Pick the right poster for a card. Episodes carry a low-res 16:9 video
// frame in `thumb` — for poster slots the show poster (grandparentThumb)
// is what a poster row should show; seasons fall back to the show poster.
function posterThumbPath(item) {
  if (item.type === 'episode') {
    return item.grandparentThumb || item.parentThumb || item.thumb;
  }
  if (item.type === 'season') {
    return item.thumb || item.parentThumb;
  }
  return item.thumb || item.parentThumb || item.grandparentThumb;
}

function createPosterCard(item, opts = {}) {
  const { showProgress = false, showWatched = false, kind = 'item' } = opts;
  const card = document.createElement('div');
  card.className = 'poster-card focusable';
  card.tabIndex = 0;
  card.dataset.key = item.ratingKey;
  card.dataset.type = item.type;
  card.dataset.kind = kind;

  const thumbPath = posterThumbPath(item);
  const thumb = imgUrl(thumbPath, 300, 450);
  let progressHtml = '';
  let badgeHtml = '';

  // In-progress bar — applies to either "Continue Watching" row or watched-indicator overlay
  if ((showProgress || showWatched) && item.viewOffset && item.duration) {
    const pct = Math.max(2, Math.round((item.viewOffset / item.duration) * 100));
    progressHtml = `<div class="card-progress"><div class="card-progress-fill" style="width:${pct}%"></div></div>`;
  }

  // Watched checkmark — viewCount > 0 means fully watched at least once
  if (showWatched && item.viewCount && !item.viewOffset) {
    badgeHtml = `<div class="card-watched">✓</div>`;
  }
  // For episodes inside shows, the show-level "lastViewedAt" is not reliable;
  // fall back to leafCount/viewedLeafCount for shows
  if (showWatched && item.type === 'show' &&
      item.viewedLeafCount && item.leafCount &&
      item.viewedLeafCount >= item.leafCount) {
    badgeHtml = `<div class="card-watched">✓</div>`;
  }

  // Episodes on a poster card are labelled by their show
  const label = item.type === 'episode'
    ? (item.grandparentTitle || item.title || '')
    : (item.title || item.grandparentTitle || '');
  const sub = item.type === 'episode' && (item.parentIndex != null || item.index != null)
    ? `S${item.parentIndex ?? '?'} · E${item.index ?? '?'}`
    : '';

  card.innerHTML = `
    ${thumbPath
      ? `<img src="${thumb}" alt="${label}" loading="lazy">`
      : `<div class="poster-fallback">${label}</div>`}
    ${badgeHtml}
    ${progressHtml}
    <div class="card-title">${label}${sub ? ` <span class="card-sub">${sub}</span>` : ''}</div>
  `;

  // If the proxy/Jellyfin fails to serve the image, swap in a titled placeholder
  // (setting src='' just re-fires the error event in a loop).
  const img = card.querySelector('img');
  if (img) {
    img.addEventListener('error', () => {
      const fb = document.createElement('div');
      fb.className = 'poster-fallback';
      fb.textContent = label;
      img.replaceWith(fb);
    }, { once: true });
  }

  if (kind === 'collection') {
    card.addEventListener('click', () => openCollection(item));
  } else if (kind === 'playlist') {
    card.addEventListener('click', () => openPlaylist(item));
  } else {
    card.addEventListener('click', () => openItem(item));
  }
  return card;
}

function buildHomeGrid() {
  App.focusGrid = [];

  // Row 0: top bar (search)
  const topbar = [...document.querySelectorAll('#home-topbar .topbar-btn')];
  if (topbar.length) App.focusGrid.push(topbar);

  // Row 1: hero buttons
  const heroBtns = [...document.querySelectorAll('#hero-buttons .hero-btn')];
  if (heroBtns.length) App.focusGrid.push(heroBtns);

  // Row per content section. The "Browse All" button goes FIRST so it's
  // reachable with one press (Left from the first card) instead of being
  // stranded after ~20 posters.
  document.querySelectorAll('#screen-home .row-section:not(.hidden)').forEach(section => {
    const cards  = [...section.querySelectorAll('.poster-card, .app-card')];
    const seeAll = section.querySelector('.see-all-btn');
    if (cards.length) {
      const rowItems = seeAll ? [seeAll, ...cards] : cards;
      App.focusGrid.push(rowItems);
    }
  });

  // Default focus on hero "Play"
  setFocus(1, 0);
}

// ── SEARCH ─────────────────────────────────────────────────────────────────
const OSK_LAYOUT = [
  ['A','B','C','D','E','F','G','H','I','J'],
  ['K','L','M','N','O','P','Q','R','S','T'],
  ['U','V','W','X','Y','Z','-',"'",'.',':'],
  ['0','1','2','3','4','5','6','7','8','9'],
  ['Space','Backspace','Clear'],
];

let searchQuery = '';
let searchDebounce = null;

function openSearch() {
  searchQuery = '';
  renderSearchInput();
  document.getElementById('search-results').innerHTML =
    '<div class="lib-loading">Start typing to search your library</div>';
  buildOSK();
  showScreen('search');
}

function buildOSK(containerId = 'osk', onPress = oskPress) {
  const osk = document.getElementById(containerId);
  osk.innerHTML = '';
  OSK_LAYOUT.forEach((row, ri) => {
    const r = document.createElement('div');
    r.className = 'osk-row';
    row.forEach(key => {
      const b = document.createElement('button');
      b.className = 'osk-key focusable';
      let label = key, klass = '';
      if (key === 'Space')     { label = '␣'; klass = 'osk-wide'; }
      if (key === 'Backspace') { label = '⌫'; klass = 'osk-wide'; }
      if (key === 'Clear')     { label = 'Clr'; klass = 'osk-wide'; }
      b.className += ' ' + klass;
      b.textContent = label;
      b.dataset.key = key;
      b.onclick = () => onPress(key);
      r.appendChild(b);
    });
    osk.appendChild(r);
  });
}

function oskPress(key) {
  if (key === 'Backspace') {
    searchQuery = searchQuery.slice(0, -1);
  } else if (key === 'Clear') {
    searchQuery = '';
  } else if (key === 'Space') {
    searchQuery += ' ';
  } else {
    searchQuery += key.toLowerCase();
  }
  renderSearchInput();
  scheduleSearch();
}

function renderSearchInput() {
  document.getElementById('search-input-text').textContent = searchQuery;
}

function scheduleSearch() {
  clearTimeout(searchDebounce);
  if (!searchQuery || searchQuery.trim().length < 2) {
    document.getElementById('search-results').innerHTML =
      '<div class="lib-loading">Type at least 2 characters…</div>';
    return;
  }
  searchDebounce = setTimeout(runSearch, 220);
}

async function runSearch() {
  const q = searchQuery.trim();
  const results = document.getElementById('search-results');
  results.innerHTML = '<div class="lib-loading">Searching…</div>';
  try {
    const items = await api(`/api/search?q=${encodeURIComponent(q)}`);
    if (!items.length) {
      results.innerHTML = `<div class="lib-loading">No results for "${q}"</div>`;
      return;
    }
    results.innerHTML = '';
    items.forEach(item => {
      const card = createPosterCard(item, { showWatched: true });
      results.appendChild(card);
    });
    buildSearchGrid(true);
  } catch (e) {
    results.innerHTML = '<div class="lib-loading">Search failed</div>';
  }
}

function buildSearchGrid(preserveFocus = false) {
  const prevRow = App.focusRow, prevCol = App.focusCol;
  const back   = document.querySelector('#screen-search .back-btn');
  back.onclick = () => goBack();

  const oskRows = [...document.querySelectorAll('#osk .osk-row')]
                  .map(r => [...r.querySelectorAll('.osk-key')]);

  const resultCards = [...document.querySelectorAll('#search-results .poster-card')];
  // Rows come from the rendered layout — this grid is auto-fill too, so its
  // column count changes with the window.
  const resultRows = rowsFromLayout(resultCards);

  // Build the grid: row 0 is back button.
  // Rows for OSK come from the left pane.  Result cards live in their own
  // rows that the user reaches by pressing RIGHT past the OSK columns.
  App.focusGrid = [[back]];

  // For each OSK row, append the corresponding result row (if any) on the right
  // so RIGHT key moves from OSK to results, LEFT key returns.
  const maxRows = Math.max(oskRows.length, resultRows.length);
  for (let i = 0; i < maxRows; i++) {
    const left  = oskRows[i] || [];
    const right = resultRows[i] || [];
    App.focusGrid.push([...left, ...right]);
  }

  // Results land while you're still typing and rebuild this grid; snapping
  // back to the first key each time would make the keyboard unusable.
  if (preserveFocus) setFocus(prevRow, prevCol);
  else               setFocus(1, 0);  // first OSK key
}

// ── YOUTUBE APP ────────────────────────────────────────────────────────────
// Ad-free: the backend resolves videos with yt-dlp and remuxes the raw
// media streams — no YouTube page, no player SDK, no ads.
let ytQuery = '';
let ytDebounce = null;

function openYouTube() {
  ytQuery = '';
  document.getElementById('yt-input-text').textContent = '';
  buildOSK('yt-osk', ytOskPress);
  renderYtRecent();
  showScreen('youtube');
}

function ytOskPress(key) {
  if (key === 'Backspace')  ytQuery = ytQuery.slice(0, -1);
  else if (key === 'Clear') ytQuery = '';
  else if (key === 'Space') ytQuery += ' ';
  else                      ytQuery += key.toLowerCase();
  document.getElementById('yt-input-text').textContent = ytQuery;

  clearTimeout(ytDebounce);
  if (!ytQuery || ytQuery.trim().length < 2) {
    renderYtRecent();
    return;
  }
  ytDebounce = setTimeout(runYtSearch, 400);
}

async function runYtSearch() {
  const q = ytQuery.trim();
  const results = document.getElementById('yt-results');
  document.getElementById('yt-results-title').textContent = 'Results';
  results.innerHTML = '<div class="lib-loading">Searching YouTube…</div>';
  try {
    const items = await api(`/api/yt/search?q=${encodeURIComponent(q)}`);
    if (items.error) throw new Error(items.error);
    if (!items.length) {
      results.innerHTML = `<div class="lib-loading">No results for "${q}"</div>`;
      return;
    }
    renderYtCards(items);
  } catch (e) {
    console.warn('yt search failed:', e);
    results.innerHTML = '<div class="lib-loading">Search failed — is yt-dlp installed?</div>';
  }
}

function renderYtCards(items, preserveFocus = true) {
  const results = document.getElementById('yt-results');
  results.innerHTML = '';
  items.forEach(v => {
    const card = document.createElement('div');
    card.className = 'yt-card focusable';
    card.tabIndex = 0;
    const dur = v.duration ? formatTime(v.duration) : '';
    card.innerHTML = `
      <div class="yt-thumb-wrap">
        <img src="${v.thumb}" alt="" loading="lazy">
        ${dur ? `<span class="yt-dur">${dur}</span>` : ''}
      </div>
      <div class="yt-card-title">${(v.title || '').replace(/</g, '&lt;')}</div>
      <div class="yt-card-channel">${(v.channel || '').replace(/</g, '&lt;')}</div>
    `;
    card.onclick = () => playYtVideo(v);
    results.appendChild(card);
  });
  buildYtGrid(preserveFocus);
}

// Recently watched — stored locally, newest first, max 12
// The 'pht_' storage keys predate the Jellyfin port and are kept as-is so
// upgrading doesn't wipe your saved recents and screen-fit preference.
function ytRecentList() {
  try { return JSON.parse(localStorage.getItem('pht_yt_recent') || '[]'); }
  catch (_) { return []; }
}

function ytRememberVideo(v) {
  const list = ytRecentList().filter(x => x.id !== v.id);
  list.unshift({ id: v.id, title: v.title, channel: v.channel,
                 duration: v.duration, thumb: v.thumb });
  localStorage.setItem('pht_yt_recent', JSON.stringify(list.slice(0, 12)));
}

function renderYtRecent(preserveFocus = true) {
  const recent = ytRecentList();
  document.getElementById('yt-results-title').textContent =
    recent.length ? 'Recently Watched' : 'Search YouTube';
  const results = document.getElementById('yt-results');
  if (!recent.length) {
    results.innerHTML = '<div class="lib-loading">Type to search — no ads, promise.</div>';
    buildYtGrid(preserveFocus);
    return;
  }
  renderYtCards(recent, preserveFocus);
}

function buildYtGrid(preserveFocus = false) {
  const back = document.getElementById('yt-back-btn');
  back.onclick = () => goBack();

  const oskRows = [...document.querySelectorAll('#yt-osk .osk-row')]
                  .map(r => [...r.querySelectorAll('.osk-key')]);
  const cards = [...document.querySelectorAll('#yt-results .yt-card')];
  const cardRows = rowsFromLayout(cards);

  const prevRow = App.focusRow, prevCol = App.focusCol;
  App.focusGrid = [[back]];
  const maxRows = Math.max(oskRows.length, cardRows.length);
  for (let i = 0; i < maxRows; i++) {
    App.focusGrid.push([...(oskRows[i] || []), ...(cardRows[i] || [])]);
  }
  // Results arrive while you're still typing, and every batch rebuilds this
  // grid — snapping back to the first key each time would make the keyboard
  // unusable. Keep the caller's position unless we're opening fresh.
  if (preserveFocus) setFocus(prevRow, prevCol);
  else               setFocus(1, 0);
}

// ── YouTube playback ───────────────────────────────────────────────────────
async function playYtVideo(v) {
  const video = document.getElementById('video-player');

  if (App.hls) { App.hls.destroy(); App.hls = null; }
  clearOneShotVideoListeners(video);
  removeSideloadedSubs();

  showScreen('player');
  document.getElementById('np-title').textContent = v.title || 'YouTube';
  document.getElementById('np-subtitle').textContent = v.channel || '';
  document.getElementById('pb-title-small').textContent = v.title || '';
  showToast(`▶ Loading: ${v.title}`);

  try {
    const info = await api(`/api/yt/resolve/${v.id}`);
    if (info.error) throw new Error(info.error);

    ytRememberVideo({ ...v, title: info.title || v.title,
                      channel: info.channel || v.channel,
                      duration: info.duration || v.duration });

    App.currentItem   = { ratingKey: null, title: info.title, type: 'yt' };
    App.ytPlayback    = { id: v.id, duration: info.duration, offset: 0 };
    App.playbackMode  = 'direct';
    App.streamDuration = info.duration;
    App.markers       = info.markers || [];
    App.activeMarker  = null;
    hideSkipButton();
    buildPlayerGrid();   // rebuild now that yt-mode is known

    // resolutionLabel, not raw height: a 2.39:1 upload is 2048x858, and
    // "858p" is a confusing way to say 1080p-class.
    const ytLabel = resolutionLabel(info.width, info.height);
    showQualityBadge(
      `${ytLabel} · YouTube · ad-free`.replace(/^ · /, ''), false);

    ytStartStream(0, true);
    setupVideoEvents(video, App.currentItem);
    startYtWatchdog();
  } catch (e) {
    console.error('playYtVideo error:', e);
    showToast('⚠ Could not play video');
  }
}

function ytStartStream(offsetSecs, autoplay = true) {
  const video = document.getElementById('video-player');
  App.ytPlayback.offset = offsetSecs;
  App._ytLastRestart = Date.now();      // grace window for the stall watchdog
  video.src = `/api/yt/stream/${App.ytPlayback.id}?t=${offsetSecs.toFixed(2)}`;
  video.load();
  if (autoplay) video.play().catch(console.warn);
}

// Force the current YouTube stream to reconnect from where the playhead is.
// Used by the stall watchdog and by a manual Play press on a starved stream.
function ytReconnect(reason) {
  if (!App.ytPlayback) return;
  const v = document.getElementById('video-player');
  const absPos = App.ytPlayback.offset + (v.currentTime || 0);
  if (reason) showToast('⟳ Reconnecting…');
  ytStartStream(absPos, true);
}

// Watchdog: if a YouTube stream starves (ffmpeg/googlevideo hiccup that slips
// past ffmpeg's own reconnect), the <video> freezes with no way to resume via
// Play. Detect "trying to play but the clock isn't moving" and restart the
// pipe at the current position so playback self-heals.
function startYtWatchdog() {
  stopYtWatchdog();
  const video = document.getElementById('video-player');
  let lastT = video.currentTime;
  let stalls = 0;
  App._ytWatch = setInterval(() => {
    if (!App.ytPlayback || App.screen !== 'player') return;
    if (video.paused || video.ended) { lastT = video.currentTime; stalls = 0; return; }
    // Let it finish naturally near the end instead of restarting past it.
    const durS = App.ytPlayback.duration / 1000;
    const absPos = App.ytPlayback.offset + video.currentTime;
    if (durS && absPos >= durS - 3) { lastT = video.currentTime; stalls = 0; return; }
    // Grace period right after a (re)start while the pipe re-buffers.
    if (Date.now() - (App._ytLastRestart || 0) < 6000) { lastT = video.currentTime; return; }
    if (Math.abs(video.currentTime - lastT) < 0.04) {
      if (++stalls >= 4) {          // ~4s wanting to play but frozen
        stalls = 0;
        console.warn('[JHT] YouTube stream stalled — reconnecting');
        ytReconnect(true);
      }
    } else {
      stalls = 0;
    }
    lastT = video.currentTime;
  }, 1000);
}

function stopYtWatchdog() {
  if (App._ytWatch) { clearInterval(App._ytWatch); App._ytWatch = null; }
}

// Seek by restarting the remux pipe at the target position
function ytSeekTo(targetSecs) {
  if (!App.ytPlayback) return;
  const durSecs = App.ytPlayback.duration / 1000;
  targetSecs = Math.max(0, Math.min(targetSecs, durSecs ? durSecs - 2 : targetSecs));
  ytStartStream(targetSecs, true);
}

// Current absolute position in ms, accounting for the pipe's start offset
function ytPositionMs() {
  const v = document.getElementById('video-player');
  return (App.ytPlayback.offset + v.currentTime) * 1000;
}

// ── COLLECTION / PLAYLIST drill-down ───────────────────────────────────────
async function openCollection(col) {
  document.getElementById('library-title').textContent = col.title;
  const grid = document.getElementById('library-grid');
  grid.innerHTML = '<div class="lib-loading">Loading collection…</div>';
  showScreen('library');
  // hide library toolbar in collection view
  document.getElementById('library-toolbar').classList.add('hidden');

  try {
    const items = await api(`/api/collection/${col.ratingKey}/children`);
    grid.innerHTML = '';
    items.forEach(item => {
      const card = createPosterCard(item, { showWatched: true });
      grid.appendChild(card);
    });
    buildLibraryGrid();
  } catch (e) {
    grid.innerHTML = '<div class="lib-loading">Could not load collection</div>';
  }
}

async function openPlaylist(pl) {
  document.getElementById('library-title').textContent = pl.title;
  const grid = document.getElementById('library-grid');
  grid.innerHTML = '<div class="lib-loading">Loading playlist…</div>';
  showScreen('library');
  document.getElementById('library-toolbar').classList.add('hidden');

  try {
    const items = await api(`/api/playlist/${pl.ratingKey}/items`);
    grid.innerHTML = '';
    items.forEach(item => {
      const card = createPosterCard(item, { showWatched: true });
      grid.appendChild(card);
    });
    buildLibraryGrid();
  } catch (e) {
    grid.innerHTML = '<div class="lib-loading">Could not load playlist</div>';
  }
}

// ── LIBRARY SCREEN ─────────────────────────────────────────────────────────
async function openLibrary(lib) {
  App.currentLib = lib;
  App.libFilters = { sort: 'titleSort', genre: 'all', decade: 'all', unwatched: '0' };
  document.getElementById('library-title').textContent = lib.title;
  document.getElementById('library-toolbar').classList.remove('hidden');
  updateToolbarLabels();
  showScreen('library');
  await reloadLibrary();
}

// Bumped on every library (re)load so a batch that arrives after the user has
// moved on or changed a filter can tell that it is stale and stop.
let libLoadToken = 0;

async function reloadLibrary() {
  const lib = App.currentLib;
  if (!lib) return;
  const token = ++libLoadToken;
  const grid = document.getElementById('library-grid');
  grid.innerHTML = '<div class="lib-loading">Loading library…</div>';

  const f = App.libFilters;
  const qp = new URLSearchParams({
    size: 200,
    start: 0,
    sort: f.sort,
  });
  if (f.genre   !== 'all') qp.set('genre',   f.genre);
  if (f.decade  !== 'all') qp.set('decade',  f.decade);
  if (f.unwatched === '1') qp.set('unwatched', '1');

  const BATCH = 200;
  let firstBatch = [];
  try {
    firstBatch = await api(`/api/library/${lib.key}/all?${qp.toString()}`);
  } catch (e) {
    grid.innerHTML = '<div class="lib-loading">Could not load library</div>';
    return;
  }
  if (token !== libLoadToken) return;

  const header = document.querySelector('#screen-library .screen-header');
  let countEl = header.querySelector('.lib-count');
  if (!countEl) {
    countEl = document.createElement('span');
    countEl.className = 'lib-count';
    header.appendChild(countEl);
  }

  const appendCards = (items) => {
    const frag = document.createDocumentFragment();
    items.forEach(it => frag.appendChild(createPosterCard(it, { showWatched: true })));
    grid.appendChild(frag);
  };

  // Paint the first batch straight away. A thousand-title library used to
  // fetch and render every card before showing anything, which is what made
  // "Browse All" feel like a freeze; the rest streams in behind this.
  App.libItems = firstBatch;
  grid.innerHTML = '';
  countEl.textContent = `${firstBatch.length} titles`;
  appendCards(firstBatch);
  buildLibraryGrid();

  let total = firstBatch.length;
  try {
    const countParams = new URLSearchParams();
    if (f.genre  !== 'all') countParams.set('genre',  f.genre);
    if (f.decade !== 'all') countParams.set('decade', f.decade);
    if (f.unwatched === '1') countParams.set('unwatched', '1');
    const meta = await fetch(`/api/library/${lib.key}/count?${countParams.toString()}`);
    if (meta.ok) total = (await meta.json()).total || total;
  } catch (_) {}
  if (token !== libLoadToken) return;

  if (total > firstBatch.length) {
    countEl.textContent = `${total} titles`;
    // One batch at a time, each appended in its own task, so the UI keeps
    // responding to the D-pad while the tail of the library arrives.
    for (let start = BATCH; start < total; start += BATCH) {
      const p = new URLSearchParams(qp);
      p.set('start', start);
      let batch;
      try {
        batch = await api(`/api/library/${lib.key}/all?${p.toString()}`);
      } catch (_) {
        break;
      }
      if (token !== libLoadToken) return;
      if (!batch.length) break;
      App.libItems = App.libItems.concat(batch);
      appendCards(batch);
      // Keep the focus model in step with the rows just added, without
      // yanking the user back to the first card.
      buildLibraryGrid(true);
      await new Promise(r => setTimeout(r, 0));
      if (token !== libLoadToken) return;
    }
    countEl.textContent = `${App.libItems.length} titles`;
  }
}

function updateToolbarLabels() {
  const f = App.libFilters;
  const sortLabels = {
    'titleSort':            'Title',
    'addedAt:desc':         'Recently Added',
    'originallyAvailableAt:desc': 'Release Date',
    'year:desc':            'Year',
    'rating:desc':          'Rating',
    'lastViewedAt:desc':    'Last Watched',
  };
  document.getElementById('btn-sort').textContent =
    `Sort: ${sortLabels[f.sort] || 'Title'} ▾`;
  document.getElementById('btn-filter-genre').textContent =
    `Genre: ${f.genre === 'all' ? 'All' : f.genre} ▾`;
  document.getElementById('btn-filter-decade').textContent =
    `Decade: ${f.decade === 'all' ? 'All' : f.decade + 's'} ▾`;
  document.getElementById('btn-filter-unwatched').textContent =
    `Unwatched only: ${f.unwatched === '1' ? 'On' : 'Off'}`;
}

function buildLibraryGrid(preserveFocus = false) {
  const prevRow = App.focusRow, prevCol = App.focusCol;
  const backBtn = document.querySelector('#screen-library .back-btn');
  const cards   = [...document.querySelectorAll('#library-grid .poster-card')];

  if (backBtn) backBtn.onclick = () => goBack();

  // Wire toolbar buttons
  const btnSort   = document.getElementById('btn-sort');
  const btnGenre  = document.getElementById('btn-filter-genre');
  const btnDecade = document.getElementById('btn-filter-decade');
  const btnUnw    = document.getElementById('btn-filter-unwatched');
  if (btnSort)   btnSort.onclick   = openSortPicker;
  if (btnGenre)  btnGenre.onclick  = openGenrePicker;
  if (btnDecade) btnDecade.onclick = openDecadePicker;
  if (btnUnw)    btnUnw.onclick    = () => {
    App.libFilters.unwatched = App.libFilters.unwatched === '1' ? '0' : '1';
    updateToolbarLabels();
    reloadLibrary();
  };

  App.focusGrid = [];
  // Row 0: back + toolbar buttons
  const toolbarVisible = !document.getElementById('library-toolbar').classList.contains('hidden');
  const topRow = backBtn ? [backBtn] : [];
  if (toolbarVisible) {
    topRow.push(btnSort, btnGenre, btnDecade, btnUnw);
  }
  if (topRow.length) App.focusGrid.push(topRow);

  // One focus row per rendered row, however many columns the grid chose.
  rowsFromLayout(cards).forEach(row => App.focusGrid.push(row));

  if (preserveFocus) setFocus(prevRow, prevCol);
  else               setFocus(cards.length ? 1 : 0, 0);
}

// ── Library sort & filter pickers ──────────────────────────────────────────
function openSortPicker() {
  openPicker('Sort by', [
    { value: 'titleSort',            label: 'Title (A–Z)' },
    { value: 'addedAt:desc',         label: 'Recently Added' },
    { value: 'originallyAvailableAt:desc', label: 'Release Date' },
    { value: 'year:desc',            label: 'Year (newest)' },
    { value: 'rating:desc',          label: 'Rating' },
    { value: 'lastViewedAt:desc',    label: 'Last Watched' },
  ], App.libFilters.sort, (v) => {
    App.libFilters.sort = v;
    updateToolbarLabels();
    reloadLibrary();
  });
}

async function openGenrePicker() {
  if (!App.currentLib) return;
  let genres = [];
  try { genres = await api(`/api/library/${App.currentLib.key}/genres`); } catch (_) {}
  const opts = [{ value: 'all', label: 'All Genres' }]
    .concat(genres.map(g => ({ value: g.title, label: g.title })));
  openPicker('Genre', opts, App.libFilters.genre, (v) => {
    App.libFilters.genre = v;
    updateToolbarLabels();
    reloadLibrary();
  });
}

async function openDecadePicker() {
  if (!App.currentLib) return;
  let decades = [];
  try { decades = await api(`/api/library/${App.currentLib.key}/decades`); } catch (_) {}
  const opts = [{ value: 'all', label: 'All Decades' }]
    .concat(decades.map(d => ({ value: d.key, label: d.title + 's' })));
  openPicker('Decade', opts, App.libFilters.decade, (v) => {
    App.libFilters.decade = v;
    updateToolbarLabels();
    reloadLibrary();
  });
}

function openPicker(title, options, current, onPick) {
  document.getElementById('picker-title').textContent = title;
  const list = document.getElementById('picker-list');
  list.innerHTML = '';
  options.forEach(opt => {
    const row = document.createElement('button');
    row.className = 'track-row focusable' + (String(opt.value) === String(current) ? ' selected' : '');
    row.innerHTML = `<span>${opt.label}</span><span class="track-tick">${String(opt.value) === String(current) ? '✓' : ''}</span>`;
    row.onclick = () => {
      closeModal();
      onPick(opt.value);
    };
    list.appendChild(row);
  });
  document.getElementById('btn-picker-close').onclick = closeModal;
  openModal('modal-picker', () => {
    const rows = [...list.querySelectorAll('.track-row')];
    const close = document.getElementById('btn-picker-close');
    App.focusGrid = rows.map(r => [r]).concat([[close]]);
    setFocus(0, 0);
  });
}

// ── DETAIL SCREEN ──────────────────────────────────────────────────────────
async function openItem(item) {
  if (item.type === 'movie' || item.type === 'show') {
    await showDetail(item);
  } else if (item.type === 'episode') {
    // Episodes go straight to playback (with resume confirmation)
    await maybeResume(item);
  } else {
    await showDetail(item);
  }
}

async function showDetail(item) {
  App.detailItem = item;

  let meta = item;
  try {
    meta = await api(`/api/metadata/${item.ratingKey}`);
    App.detailItem = meta;
  } catch (_) {}

  // Backdrop
  const bd = document.getElementById('detail-backdrop');
  if (meta.art) {
    bd.style.backgroundImage = `url("${imgUrl(meta.art, 1920, 1080)}")`;
  } else {
    bd.style.backgroundImage = `url("${imgUrl(meta.thumb, 1920, 1080)}")`;
  }

  document.getElementById('detail-poster').src = imgUrl(meta.thumb, 400, 600);
  document.getElementById('detail-rating-badge').textContent = meta.contentRating || '';
  document.getElementById('detail-title').textContent = meta.title || '';

  const year = meta.year || '';
  const dur  = meta.duration ? formatTime(meta.duration) : '';
  document.getElementById('detail-meta').textContent =
    [year, dur, meta.studio || ''].filter(Boolean).join(' · ');

  // Ratings — community score out of 10, plus the Rotten Tomatoes critic
  // percentage when Jellyfin has one (60%+ is "fresh").
  const ratingsEl = document.getElementById('detail-ratings');
  ratingsEl.innerHTML = '';
  if (meta.rating) {
    ratingsEl.innerHTML += `<span class="rating-badge rating-critic">⭐ ${Number(meta.rating).toFixed(1)}</span>`;
  }
  if (meta.criticRating != null) {
    const fresh = Number(meta.criticRating) >= 60;
    ratingsEl.innerHTML +=
      `<span class="rating-badge rating-rt ${fresh ? 'rt-fresh' : 'rt-rotten'}">` +
      `${fresh ? '🍅' : '🤢'} ${Number(meta.criticRating)}%</span>`;
  }

  document.getElementById('detail-summary').textContent = meta.summary || '';

  // Genres
  const genresEl = document.getElementById('detail-genres');
  genresEl.innerHTML = '';
  (meta.Genre || []).slice(0, 6).forEach(g => {
    const t = document.createElement('span');
    t.className = 'genre-tag';
    t.textContent = g.tag;
    genresEl.appendChild(t);
  });

  // Director(s) + Cast
  const directors = (meta.Director || []).map(d => d.tag).join(', ');
  const writers   = (meta.Writer || []).map(w => w.tag).join(', ');
  const cast      = (meta.Role || []).slice(0, 6).map(r => r.tag).join(', ');

  const dirEl  = document.getElementById('detail-directors');
  const castEl = document.getElementById('detail-cast');
  dirEl.innerHTML  = '';
  castEl.innerHTML = '';
  if (directors) {
    dirEl.innerHTML = `<span class="credit-label">Director:</span> ${directors}` +
      (writers ? `<span class="credit-sep"></span><span class="credit-label">Writer:</span> ${writers}` : '');
  } else if (writers) {
    dirEl.innerHTML = `<span class="credit-label">Writer:</span> ${writers}`;
  }
  if (cast) {
    castEl.innerHTML = `<span class="credit-label">Cast:</span> ${cast}`;
  }

  // Play button — shows resume confirmation if applicable
  const btnPlay = document.getElementById('btn-play-detail');
  btnPlay.onclick = () => {
    if (meta.type === 'show') {
      playNextEpisode(meta.ratingKey);
    } else {
      maybeResume(meta);
    }
  };

  // Mark watched button
  const btnMark = document.getElementById('btn-mark-watched');
  const isWatched = !!meta.viewCount;
  btnMark.textContent = isWatched ? '↺ Mark Unwatched' : '✓ Mark Watched';
  btnMark.onclick = async () => {
    try {
      await apiPost('/api/mark_watched', { rating_key: meta.ratingKey, watched: !isWatched });
      meta.viewCount = isWatched ? 0 : 1;
      meta.viewOffset = 0;
      showDetail(meta);   // re-render
    } catch (e) { showToast('⚠ Could not update watched state'); }
  };

  // Shows: load seasons
  const seasonsSec = document.getElementById('seasons-section');
  const episodesList = document.getElementById('episodes-list');
  const seasonTabs   = document.getElementById('season-tabs');

  if (meta.type === 'show') {
    seasonsSec.classList.remove('hidden');
    const seasons = await api(`/api/metadata/${meta.ratingKey}/children`);
    seasonTabs.innerHTML = '';
    episodesList.innerHTML = '';

    seasons.forEach((season, i) => {
      const tab = document.createElement('button');
      tab.className = 'season-tab focusable';
      tab.textContent = season.title;
      tab.onclick = () => {
        seasonTabs.querySelectorAll('.season-tab').forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        loadEpisodes(season.ratingKey, episodesList);
      };
      if (i === 0) { tab.classList.add('active'); loadEpisodes(season.ratingKey, episodesList); }
      seasonTabs.appendChild(tab);
    });
  } else {
    seasonsSec.classList.add('hidden');
  }

  showScreen('detail');
}

async function loadEpisodes(seasonKey, listEl) {
  listEl.innerHTML = '<div style="color:#666;padding:16px">Loading…</div>';
  const episodes = await api(`/api/metadata/${seasonKey}/children`);
  listEl.innerHTML = '';

  episodes.forEach(ep => {
    const item = document.createElement('div');
    item.className = 'episode-item focusable';
    item.tabIndex = 0;
    const dur   = ep.duration ? formatTime(ep.duration) : '';
    const watched = ep.viewCount ? '✓' : '';
    let progress = '';
    if (ep.viewOffset && ep.duration) {
      const pct = Math.round((ep.viewOffset / ep.duration) * 100);
      progress = `<div class="ep-progress"><div class="ep-progress-fill" style="width:${pct}%"></div></div>`;
    }
    item.innerHTML = `
      <span class="ep-num">E${ep.index || ''} ${watched}</span>
      <div class="ep-body">
        <div class="ep-t">${ep.title}</div>
        <div class="ep-d">${ep.summary || ''}</div>
        ${progress}
      </div>
      <span class="ep-dur">${dur}</span>
    `;
    item.onclick = () => maybeResume(ep);
    listEl.appendChild(item);
  });

  buildDetailGrid(true);
}

async function playNextEpisode(showKey) {
  try {
    const onDeck = await api('/api/on_deck');
    const ep = onDeck.find(i => i.grandparentRatingKey == showKey || i.showRatingKey == showKey);
    if (ep) return maybeResume(ep);
    const seasons = await api(`/api/metadata/${showKey}/children`);
    if (seasons.length) {
      const eps = await api(`/api/metadata/${seasons[0].ratingKey}/children`);
      if (eps.length) maybeResume(eps[0]);
    }
  } catch (e) { console.error(e); }
}

function buildDetailGrid(preserveFocus = false) {
  const prevRow = App.focusRow, prevCol = App.focusCol;
  App.focusGrid = [];

  const btns = [...document.querySelectorAll('#detail-buttons .hero-btn')];
  if (btns.length) App.focusGrid.push(btns);

  const tabs = [...document.querySelectorAll('#season-tabs .season-tab')];
  if (tabs.length) App.focusGrid.push(tabs);

  const eps = [...document.querySelectorAll('#episodes-list .episode-item')];
  eps.forEach(ep => App.focusGrid.push([ep]));

  // When re-building after a season-tab click, keep focus where the user was
  // instead of yanking it back to the Play button.
  if (preserveFocus) setFocus(prevRow, prevCol);
  else setFocus(0, 0);
}

// ── Resume confirmation ────────────────────────────────────────────────────
function maybeResume(item) {
  const off = item.viewOffset || 0;
  // < 30 s into a title? Skip the dialog and just play
  if (off < 30000) {
    return playItem(item, 0);
  }
  // Near the end? Treat as fresh start
  if (item.duration && off / item.duration > 0.95) {
    return playItem(item, 0);
  }
  document.getElementById('resume-title').textContent = item.title;
  document.getElementById('resume-summary').textContent =
    item.type === 'episode'
      ? `S${item.parentIndex || ''}E${item.index || ''} · ${item.grandparentTitle || ''}`
      : (item.summary || '').slice(0, 200);
  document.getElementById('resume-time').textContent = formatTime(off);

  document.getElementById('btn-resume').onclick     = () => { closeModal(); playItem(item, off / 1000); };
  document.getElementById('btn-startover').onclick  = () => { closeModal(); playItem(item, 0); };
  document.getElementById('btn-resume-cancel').onclick = () => closeModal();

  openModal('modal-resume', () => {
    App.focusGrid = [
      [document.getElementById('btn-resume')],
      [document.getElementById('btn-startover')],
      [document.getElementById('btn-resume-cancel')],
    ];
    setFocus(0, 0);
  });
}

// ── Playback mode decision ─────────────────────────────────────────────────
const BROWSER_VIDEO_CODECS = {
  'H264':   'video/mp4; codecs="avc1.42E01E"',
  'VP8':    'video/webm; codecs="vp8"',
  'VP9':    'video/webm; codecs="vp9"',
  'AV1':    'video/mp4; codecs="av01.0.05M.08"',
  'HEVC':   'video/mp4; codecs="hev1.1.6.L93.B0"',
  'H265':   'video/mp4; codecs="hev1.1.6.L93.B0"',
};

const BROWSER_AUDIO_OK = new Set([
  'AAC', 'MP3', 'OPUS', 'VORBIS', 'FLAC', 'PCM', 'ALAC',
  'MP2', 'WAV', 'AIFF',
]);

const AUDIO_NEEDS_REMUX = new Set([
  'AC3', 'EAC3', 'DTS', 'DTSHD', 'TRUEHD', 'DTSMA', 'ATMOS',
]);

function canBrowserPlayVideo(codec) {
  if (!codec) return false;
  const mime = BROWSER_VIDEO_CODECS[codec.toUpperCase()];
  if (!mime) return false;
  if (window.MediaSource && MediaSource.isTypeSupported) {
    return MediaSource.isTypeSupported(mime);
  }
  const v = document.createElement('video');
  return v.canPlayType(mime) !== '';
}

// Containers a browser <video> tag can play directly (progressive file).
// MKV / AVI / TS / WMV etc. cannot be direct-played even when their codecs are
// fine — they must be remuxed (Direct Stream), which is still no re-encode and
// stays perfectly smooth.  Checking this up front avoids a failed direct-play
// attempt that hitches before falling back.
const BROWSER_CONTAINERS = new Set(['MP4', 'M4V', 'MOV', 'WEBM']);

function decidePlaybackMode(quality, forcedQuality) {
  // If user manually picked a non-original quality, we MUST transcode.
  if (forcedQuality && forcedQuality !== 'original') {
    return 'transcode';
  }
  if (!quality) return 'transcode';

  const vc = (quality.videoCodec || '').toUpperCase();
  const ac = (quality.audioCodec || '').toUpperCase();
  const ct = (quality.container  || '').toUpperCase();

  const videoOk = canBrowserPlayVideo(vc);
  const audioOk = BROWSER_AUDIO_OK.has(ac);
  const containerOk = BROWSER_CONTAINERS.has(ct);

  // Video the browser can't decode at all → full transcode.
  if (!videoOk) return 'transcode';

  // Video is fine. Direct play only when BOTH the container and the audio are
  // browser-friendly; otherwise Direct Stream (remux container / audio, keep
  // the original video untouched) — smooth, low CPU, no quality loss.
  if (containerOk && audioOk) return 'direct';
  return 'directStream';
}

// Resolution buckets, widest first — mirrors RESOLUTION_LABELS in jellyfin.py.
// Width leads because a 2.35:1 film is letterboxed in the file itself, so a 4K
// scope release is 3840x1632; going by height alone would call it 1440p.
const RESOLUTION_LABELS = [
  [7600, 4300, '8K'],
  [3800, 2000, '4K'],
  [2500, 1400, '1440p'],
  [1800, 1000, '1080p'],
  [1200,  700, '720p'],
  [700,   400, '480p'],
  [400,   200, '360p'],
];

function resolutionLabel(width, height) {
  const w = Number(width) || 0, h = Number(height) || 0;
  for (const [minW, minH, label] of RESOLUTION_LABELS) {
    if (w >= minW || h >= minH) return label;
  }
  return h ? `${h}p` : '';
}

// The quality badge's icon/colour: false = Direct Play (▶), 'stream' =
// Direct Stream (⇄), anything else = Transcoding (⚙).
function badgeMode(modeLabel) {
  if (modeLabel === 'Direct Play')   return false;
  if (modeLabel === 'Direct Stream') return 'stream';
  return true;
}

// Which codec family an HLS CODECS string belongs to.
function hlsCodecFamily(codec) {
  const c = (codec || '').toLowerCase();
  if (c.startsWith('hvc1') || c.startsWith('hev1')) return 'HEVC';
  if (c.startsWith('avc1') || c.startsWith('avc3')) return 'H264';
  if (c.startsWith('vp09') || c.startsWith('vp9'))  return 'VP9';
  if (c.startsWith('av01'))                         return 'AV1';
  return '';
}

/**
 * Find the variant that really copies the source video.
 *
 * Jellyfin's copy playlist isn't a bitrate ladder — it offers the same picture
 * three ways: the untouched stream, a tone-mapped SDR version, and an H.264
 * fallback for players that can't decode the original. The last two are full
 * re-encodes. Every rung advertises the same BANDWIDTH, so hls.js's ABR has
 * nothing to choose on and settles on the last one — quietly turning a
 * "Direct Stream" into a transcode. Picking the rung by codec and dynamic
 * range instead gets the real copy.
 *
 * Returns a level index, or -1 to leave the choice to hls.js.
 */
function pickCopyLevel(levels, quality) {
  if (!levels || levels.length < 2 || !quality) return -1;
  let want = (quality.videoCodec || '').toUpperCase();
  if (want === 'H265') want = 'HEVC';

  const matches = levels
    .map((lvl, idx) => ({ lvl, idx }))
    .filter(({ lvl }) => hlsCodecFamily(lvl.videoCodec) === want);
  if (!matches.length) return -1;

  // An HDR source tone-mapped to SDR is a re-encode, so keep the rung whose
  // dynamic range matches what's in the file.
  const srcHdr = /HDR|DOVI|HLG|PQ/.test(quality.videoRange || '');
  const sameRange = matches.find(({ lvl }) => {
    const range = (lvl.attrs && lvl.attrs['VIDEO-RANGE']) || '';
    return /PQ|HLG|DOVI/i.test(range) === srcHdr;
  });
  return (sameRange || matches[0]).idx;
}

// ── HLS playback helper ────────────────────────────────────────────────────
function startHlsPlayback(video, hlsUrl, startAt, quality, modeLabel) {
  if (App.hls) { App.hls.destroy(); App.hls = null; }

  if (!Hls.isSupported()) {
    if (video.canPlayType('application/vnd.apple.mpegurl')) {
      video.src = hlsUrl;
      video.addEventListener('canplay', () => {
        video.currentTime = startAt;
        video.play().catch(console.warn);
      }, { once: true });
    } else {
      showToast('⚠ HLS not supported in this browser');
    }
    return;
  }

  // Buffer tuning for a LOCAL, high-bitrate Jellyfin server.  hls.js defaults are
  // built for the open internet (small buffers, aggressive eviction) and cause
  // micro-stalls at segment boundaries on high-bitrate local streams.  Here we
  // buffer well ahead and keep a little back-buffer so small seeks don't reload.
  App.hls = new Hls({
    enableWorker:        true,   // parse/demux off the main thread (smoother UI)
    lowLatencyMode:      false,
    maxBufferLength:     60,             // seconds of forward buffer to hold
    maxMaxBufferLength:  120,            // hard ceiling when bitrate is low
    maxBufferSize:       120 * 1000 * 1000,  // 120 MB — plenty for local 4K
    backBufferLength:    30,             // keep 30s behind for instant short rewinds
    maxBufferHole:       0.5,            // jump tiny gaps instead of stalling
    highBufferWatchdogPeriod: 1,
    // ABR: start at the best level (local link is fast) instead of laddering up.
    startLevel:          -1,
    abrEwmaDefaultEstimate: 50 * 1000 * 1000,  // assume ~50 Mbps so it picks high
    fragLoadingMaxRetry: 6,
  });
  App.hls.loadSource(hlsUrl);
  App.hls.attachMedia(video);

  App.hls.on(Hls.Events.MANIFEST_PARSED, () => {
    // On a copy stream, lock to the variant that isn't secretly a re-encode.
    if (modeLabel === 'Direct Stream') {
      const idx = pickCopyLevel(App.hls.levels, quality);
      if (idx >= 0) {
        console.log(`[JHT] Pinned copy variant ${idx}/${App.hls.levels.length - 1}`,
                    App.hls.levels[idx].videoCodec);
        App.hls.currentLevel = idx;
      }
    }
    video.currentTime = startAt;
    video.play().catch(console.warn);
  });

  App.hls.on(Hls.Events.LEVEL_SWITCHED, (_, data) => {
    const lvl = App.hls.levels[data.level];
    if (lvl && quality) {
      const actualMbps = (lvl.bitrate / 1000000).toFixed(1);
      const vLabel = resolutionLabel(lvl.width, lvl.height) ||
                     quality.resolution || '';
      showQualityBadge(
        `${vLabel} · ${quality.videoCodec || ''} · ${quality.audioCodec || ''} · ${actualMbps} Mbps · ${modeLabel}`,
        badgeMode(modeLabel)
      );
    }
  });

  // Fatal-error recovery, like real players: retry network loads, reset the
  // media pipeline on decode errors, and only give up after a few attempts.
  let recoveries = 0;
  App.hls.on(Hls.Events.ERROR, (_, data) => {
    if (!data.fatal) return;
    console.error('[JHT] HLS fatal error:', data);
    if (recoveries >= 3) {
      showToast('⚠ Stream error — try another title');
      return;
    }
    recoveries++;
    if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
      showToast('⚠ Network hiccup — reconnecting…');
      App.hls.startLoad();
    } else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
      showToast('⚠ Playback hiccup — recovering…');
      App.hls.recoverMediaError();
    } else {
      showToast('⚠ Stream error — try another title');
    }
  });
}

// ── PLAYER SCREEN ──────────────────────────────────────────────────────────
// Remove the one-shot canplay/error listeners from a previous playItem call.
// Without this they accumulate on the shared <video> element and a stale
// error fallback can restart old content at the wrong position.
function clearOneShotVideoListeners(video) {
  if (video._canplayHandler) {
    video.removeEventListener('canplay', video._canplayHandler);
    video._canplayHandler = null;
  }
  if (video._errorHandler) {
    video.removeEventListener('error', video._errorHandler);
    video._errorHandler = null;
  }
}

async function playItem(item, startAtSecs = null) {
  const video = document.getElementById('video-player');

  if (App.hls) { App.hls.destroy(); App.hls = null; }
  clearOneShotVideoListeners(video);
  removeSideloadedSubs();
  stopYtWatchdog();
  App.ytPlayback = null;

  // A chosen version belongs to one title. Reloads of the same item (quality
  // or track change) keep it; moving to a different item starts fresh.
  if (!App.currentItem || App.currentItem.ratingKey !== item.ratingKey) {
    App.currentMediaSource = null;
  }

  showScreen('player');

  document.getElementById('np-title').textContent = item.grandparentTitle || item.title || '';
  document.getElementById('np-subtitle').textContent =
    item.type === 'episode' ? `S${item.parentIndex}E${item.index} · ${item.title}` :
    item.year ? String(item.year) : '';
  document.getElementById('pb-title-small').textContent = item.title || '';

  showToast(`▶ Loading: ${item.title}`);

  try {
    const qp = new URLSearchParams();
    qp.set('resolution', App.currentQualityResolution || 'original');
    if (App.currentMediaSource) qp.set('media_source', App.currentMediaSource);
    const urls = await api(`/api/stream_url/${item.ratingKey}?${qp.toString()}`);
    if (urls.error) throw new Error(urls.error);

    App.streamUrls   = urls;
    App.audioTracks  = urls.audioTracks || [];
    App.subTracks    = urls.subTracks   || [];
    App.partId       = urls.partId;
    App.markers      = urls.markers     || [];
    App.streamVersions     = urls.versions || [];
    App.currentMediaSource = urls.mediaSourceId || null;
    App.activeMarker = null;
    hideSkipButton();

    const startAt = (startAtSecs != null) ? startAtSecs : (item.viewOffset || 0) / 1000;
    const mode = App.forceNextMode || decidePlaybackMode(urls.quality, App.currentQualityResolution);
    App.forceNextMode = null;
    App.playbackMode  = mode;
    console.log(`[JHT] Playback mode: ${mode}`, urls.quality);

    const q = urls.quality || {};
    const mbps = q.bitrateKbps ? (q.bitrateKbps / 1000).toFixed(1) + ' Mbps' : '';
    const baseLabel = [q.resolution, q.videoCodec, q.audioCodec, mbps].filter(Boolean).join(' · ');

    if (mode === 'direct') {
      showQualityBadge(baseLabel + ' · Direct Play', false);
      video.src = urls.direct;
      video.load();
      video._canplayHandler = () => {
        video._canplayHandler = null;
        video.currentTime = startAt;
        video.play().catch(console.warn);
      };
      video._errorHandler = (e) => {
        clearOneShotVideoListeners(video);
        console.warn('[JHT] Direct play failed, falling back to direct stream:', e);
        showToast('⚠ Direct play failed, switching to stream mode…');
        App.playbackMode = 'directStream';
        // If the failure happened mid-play, resume where we were, not at start
        const resumeAt = video.currentTime > 1 ? video.currentTime : startAt;
        startHlsPlayback(video, urls.directStream, resumeAt, urls.quality, 'Direct Stream');
      };
      video.addEventListener('canplay', video._canplayHandler, { once: true });
      video.addEventListener('error', video._errorHandler, { once: true });

    } else if (mode === 'directStream') {
      showQualityBadge(baseLabel + ' · Direct Stream', 'stream');
      startHlsPlayback(video, urls.directStream, startAt, urls.quality, 'Direct Stream');

    } else {
      showQualityBadge(baseLabel + ' · Transcoding', true);
      startHlsPlayback(video, urls.transcode, startAt, urls.quality, 'Transcoding');
    }

    App.currentItem = item;
    App.streamDuration = urls.duration;
    reportTimeline(item.ratingKey, 0, urls.duration, 'playing');
    setupVideoEvents(video, item);

    // Re-apply the subtitle track the server has marked as selected — keeps
    // subs on across track switches, quality reloads and Up Next episodes.
    // Skip in transcode mode, where the server burns them into the video.
    if (mode !== 'transcode') {
      const selSub = App.subTracks.find(
        s => s.id && s.selected && !IMAGE_SUB_CODECS.has((s.codec || '').toLowerCase())
      );
      if (selSub) {
        sideloadSubtitle(selSub).catch(err =>
          console.warn('[JHT] Could not restore subtitles:', err));
      }
    }

  } catch (e) {
    console.error('playItem error:', e);
    showToast('⚠ Could not start playback');
  }
}

function setupVideoEvents(video, item) {
  video.removeEventListener('timeupdate', video._timeupdate);
  video.removeEventListener('play', video._play);
  video.removeEventListener('pause', video._pause);
  video.removeEventListener('ended', video._ended);

  if (video._timelineInterval) clearInterval(video._timelineInterval);
  video._timelineInterval = setInterval(() => {
    if (!video.paused && !video.ended) {
      const pos = video.currentTime * 1000;
      const dur = video.duration ? video.duration * 1000 : (App.streamDuration || 0);
      reportTimeline(item.ratingKey, pos, dur, 'playing');
    }
  }, 10000);

  video._lastBroadcast = 0;
  video._timeupdate = () => {
    // YouTube pipes restart at an offset, so absolute position = offset + t
    const pos = App.ytPlayback ? ytPositionMs() : video.currentTime * 1000;
    const dur = App.ytPlayback ? App.ytPlayback.duration
              : (video.duration ? video.duration * 1000 : (App.streamDuration || 0));
    // Progress bar updates every tick (cheap, local DOM only).
    updateProgressUI(pos, dur);
    updateSkipButton(pos);
    // The remote only needs ~1 update/sec — throttle the WebSocket emit so it
    // doesn't compete with video decode on every timeupdate (~4x/sec).
    const now = (performance && performance.now) ? performance.now() : Date.now();
    if (now - video._lastBroadcast >= 1000) {
      video._lastBroadcast = now;
      broadcastState({
        playing:  !video.paused,
        position: pos,
        duration: dur,
        title:    item.title,
        thumb:    item.thumb,
      });
    }
  };

  video._play = () => {
    document.getElementById('btn-play-pause').textContent = '⏸';
    const pos = video.currentTime * 1000;
    const dur = video.duration ? video.duration * 1000 : (App.streamDuration || 0);
    reportTimeline(item.ratingKey, pos, dur, 'playing');
    broadcastState({ playing: true });
  };
  video._pause = () => {
    document.getElementById('btn-play-pause').textContent = '▶';
    const pos = video.currentTime * 1000;
    const dur = video.duration ? video.duration * 1000 : (App.streamDuration || 0);
    reportTimeline(item.ratingKey, pos, dur, 'paused');
    broadcastState({ playing: false });
  };
  video._ended = () => {
    clearInterval(video._timelineInterval);
    const dur = video.duration ? video.duration * 1000 : (App.streamDuration || 0);
    reportTimeline(item.ratingKey, dur, dur, 'stopped');

    // For episodes — try to queue the next episode with a 10s countdown
    if (item.type === 'episode') {
      tryShowNextUp(item);
    } else {
      showToast('✅ Finished!');
      setTimeout(() => {
        stopPlayback();
        const prev = App.history[App.history.length - 1] || 'home';
        showScreen(prev, { _isBack: true });
        App.history.pop();
      }, 2000);
    }
  };

  video.addEventListener('timeupdate', video._timeupdate);
  video.addEventListener('play', video._play);
  video.addEventListener('pause', video._pause);
  video.addEventListener('ended', video._ended);

  const pb = document.getElementById('progress-bar');
  pb.onclick = (e) => {
    // offsetX is relative to whichever child was clicked (fill/thumb), so
    // compute the fraction against the bar's own bounding box instead.
    const rect = pb.getBoundingClientRect();
    const pct = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    if (App.ytPlayback) {
      ytSeekTo(pct * (App.ytPlayback.duration / 1000));
      return;
    }
    if (!isFinite(video.duration)) return;
    video.currentTime = pct * video.duration;
  };

  resetOverlayTimer();
  video.addEventListener('mousemove', resetOverlayTimer);
}

function updateProgressUI(pos, dur) {
  const pct = dur ? (pos / dur * 100) : 0;
  document.getElementById('progress-filled').style.width = pct + '%';
  document.getElementById('progress-thumb').style.left   = pct + '%';
  document.getElementById('time-current').textContent = formatTime(pos);
  document.getElementById('time-total').textContent   = formatTime(dur);

  const video = document.getElementById('video-player');
  if (video.buffered.length && isFinite(video.duration) && video.duration > 0) {
    const bpct = (video.buffered.end(video.buffered.length - 1) / video.duration * 100);
    document.getElementById('progress-buffered').style.width = bpct + '%';
  } else {
    document.getElementById('progress-buffered').style.width = '0%';
  }
}

async function reportTimeline(key, pos, dur, state) {
  if (!key) return;   // YouTube playback has no Jellyfin item id
  try {
    await fetch('/api/timeline', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // `mode` lets the backend tell Jellyfin how we're playing, so its
      // dashboard reports Direct Play / Direct Stream / Transcode correctly.
      body: JSON.stringify({ rating_key: key, position: Math.round(pos),
                             duration: Math.round(dur), state,
                             mode: App.playbackMode }),
    });
  } catch (_) {}
}

function resetOverlayTimer() {
  const overlay = document.getElementById('player-overlay');
  overlay.classList.remove('overlay-hidden');
  overlay.classList.add('overlay-visible');
  clearTimeout(App.overlayTimer);
  App.overlayTimer = setTimeout(() => {
    overlay.classList.add('overlay-hidden');
    overlay.classList.remove('overlay-visible');
  }, 4000);
}

function stopPlayback() {
  const v = document.getElementById('video-player');
  if (!v) return;
  if (v._timelineInterval) {
    clearInterval(v._timelineInterval);
    v._timelineInterval = null;
  }
  const pos = v.currentTime * 1000;
  const dur = v.duration ? v.duration * 1000 : (App.streamDuration || 0);
  if (App.currentItem) reportTimeline(App.currentItem.ratingKey, pos, dur, 'stopped');
  clearOneShotVideoListeners(v);
  stopYtWatchdog();
  v.pause();
  // src='' would make the browser request the page URL itself
  v.removeAttribute('src');
  v.load();
  if (App.hls) { App.hls.destroy(); App.hls = null; }
  removeSideloadedSubs();
  App.currentItem = null;
  App.playbackMode = null;
  App.ytPlayback = null;
  App.currentMediaSource = null;
  App.streamVersions = [];
  document.getElementById('screen-player').classList.remove('yt-mode');
  App.markers = [];
  App.activeMarker = null;
  hideSkipButton();
  cancelNextUp();
}

function buildPlayerGrid() {
  // Track/quality pickers apply to library media only — hide for YouTube
  document.getElementById('screen-player')
          .classList.toggle('yt-mode', !!App.ytPlayback);
  const bottomRow = App.ytPlayback
    ? [
        document.getElementById('btn-aspect'),
        document.getElementById('btn-vol'),
      ]
    : [
        document.getElementById('btn-audio'),
        document.getElementById('btn-subs'),
        document.getElementById('btn-quality'),
        document.getElementById('btn-aspect'),
        document.getElementById('btn-vol'),
      ];
  App.focusGrid = [
    [document.getElementById('btn-player-back')],
    [
      document.getElementById('btn-seek-back'),
      document.getElementById('btn-play-pause'),
      document.getElementById('btn-seek-fwd'),
    ],
    bottomRow,
  ];
  setFocus(1, 1);

  document.getElementById('btn-player-back').onclick = () => goBack();
  document.getElementById('btn-play-pause').onclick = togglePlayPause;
  document.getElementById('btn-seek-back').onclick  = () => seekRelative(-10);
  document.getElementById('btn-seek-fwd').onclick   = () => seekRelative(+10);
  document.getElementById('btn-audio').onclick   = () => openTracksModal('audio');
  document.getElementById('btn-subs').onclick    = () => openTracksModal('subs');
  document.getElementById('btn-quality').onclick = openQualityModal;
  document.getElementById('btn-aspect').onclick  = cycleAspect;
  document.getElementById('btn-vol').onclick     = toggleMute;
  updateAspectButton();
}

function togglePlayPause() {
  const v = document.getElementById('video-player');
  if (v.paused) {
    v.play().catch(() => {});
    showToast('▶ Play');
  } else if (App.ytPlayback && v.readyState < 3) {
    // YouTube stream starved of data — a pause/play won't revive it, so
    // reconnect the pipe from the current position instead of pausing.
    ytReconnect(true);
  } else {
    v.pause();
    showToast('⏸ Pause');
  }
  resetOverlayTimer();
}

function seekRelative(secs) {
  const v = document.getElementById('video-player');
  if (App.ytPlayback) {
    ytSeekTo(App.ytPlayback.offset + v.currentTime + secs);
  } else {
    v.currentTime = Math.max(0, Math.min(v.currentTime + secs, v.duration || 999999));
  }
  resetOverlayTimer();
  showToast(`${secs > 0 ? '+' : ''}${secs}s`);
}

// ── Screen aspect / zoom ─────────────────────────────────────────────────────
// Cinematic titles are letterboxed (black bars top/bottom); 4:3 content is
// pillarboxed (bars at the sides). Those bars are dead zones for SignalRGB's
// screen-capture ambient lighting. These modes reshape the video so the
// picture fills the screen — no bars for SignalRGB to capture.
const ASPECT_MODES  = ['auto', 'fit', 'fill', 'zoom'];
const ASPECT_LABELS = { auto: 'Auto', fit: 'Fit', fill: 'Fill', zoom: 'Zoom' };

function updateAspectButton() {
  const b = document.getElementById('btn-aspect');
  if (b) b.textContent = '🖥 ' + (ASPECT_LABELS[App.aspectMode] || 'Auto');
}

function applyAspect() {
  const v = document.getElementById('video-player');
  if (!v) return;
  v.style.transform = '';
  v.style.transition = 'transform 0.2s ease';
  const mode = App.aspectMode || 'auto';

  if (mode === 'fit') {
    v.style.objectFit = 'contain';
  } else if (mode === 'fill') {
    v.style.objectFit = 'cover';           // fills + crops overflow
  } else if (mode === 'zoom') {
    v.style.objectFit = 'contain';
    v.style.transform = 'scale(1.18)';     // manual overscan
  } else {
    autoFillAspect(v);                     // 'auto'
  }
  updateAspectButton();
}

function autoFillAspect(v) {
  const vw = v.videoWidth, vh = v.videoHeight;
  const cw = v.clientWidth || window.innerWidth;
  const ch = v.clientHeight || window.innerHeight;
  v.style.transform = '';
  if (!vw || !vh || !cw || !ch) { v.style.objectFit = 'contain'; return; }

  const contentAspect = vw / vh;
  const screenAspect  = cw / ch;

  // Content shape differs from the screen → the display is adding bars.
  // 'cover' scales to fill and crops the overflow, removing them. Works for
  // every source with no pixel access (no cross-origin issues).
  if (Math.abs(contentAspect - screenAspect) > screenAspect * 0.02) {
    v.style.objectFit = 'cover';
    return;
  }

  // Shapes match — bars, if any, are baked into the video file. Try to see
  // them by sampling a frame (skipped silently if the frame isn't readable).
  v.style.objectFit = 'contain';
  const bar = detectBlackBars(v);
  if (bar && bar > 0.03) {
    const scale = Math.min(1 / (1 - 2 * bar) * 1.01, 1.6);
    v.style.transform = `scale(${scale.toFixed(3)})`;
  }
}

// Returns the black-bar fraction (0–0.25) at top & bottom, or null if the
// frame can't be read (cross-origin taint) or no clear bars are present.
function detectBlackBars(v) {
  try {
    if (!v.videoWidth) return null;
    const c = detectBlackBars._c || (detectBlackBars._c = document.createElement('canvas'));
    const W = 24, H = 64;
    c.width = W; c.height = H;
    const ctx = c.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(v, 0, 0, W, H);
    const d = ctx.getImageData(0, 0, W, H).data;   // throws if tainted
    const rowLum = (y) => {
      let s = 0;
      for (let x = 0; x < W; x++) { const i = (y * W + x) * 4; s += d[i] + d[i + 1] + d[i + 2]; }
      return s / (W * 3);
    };
    // Guard against a genuinely dark scene: the middle must have real picture.
    if (rowLum(H >> 1) < 24) return null;
    const black = (y) => rowLum(y) < 16;
    let top = 0; while (top < H / 2 && black(top)) top++;
    let bot = 0; while (bot < H / 2 && black(H - 1 - bot)) bot++;
    return Math.min(top, bot) / H;
  } catch (_) {
    return null;   // tainted canvas — fall back to intrinsic-aspect auto
  }
}

// After a title starts, re-check once for baked-in bars (needs a few seconds of
// real picture so the black-scene guard is reliable).
function scheduleBakedBarCheck() {
  clearTimeout(App._barTimer);
  if (App.aspectMode !== 'auto') return;
  App._barTimer = setTimeout(() => {
    if (App.screen === 'player' && App.aspectMode === 'auto') applyAspect();
  }, 4000);
}

function cycleAspect() {
  const i = ASPECT_MODES.indexOf(App.aspectMode);
  App.aspectMode = ASPECT_MODES[(i + 1) % ASPECT_MODES.length];
  try { localStorage.setItem('pht_aspect', App.aspectMode); } catch (_) {}
  applyAspect();
  showToast('Screen: ' + (ASPECT_LABELS[App.aspectMode] || 'Auto'));
  resetOverlayTimer();
}

function setVolume(val) {
  const v = document.getElementById('video-player');
  v.volume = Math.max(0, Math.min(1, val / 100));
  showToast(`🔊 ${Math.round(val)}%`);
}

function toggleMute() {
  const v = document.getElementById('video-player');
  if (!v) return;
  v.muted = !v.muted;
  showToast(v.muted ? '🔇 Muted' : '🔊 Unmuted');
}

// ── Skip Intro / Skip Credits (Jellyfin media segments) ──────────────────────────────
function updateSkipButton(posMs) {
  if (!App.markers.length || App.screen !== 'player') return;
  // Find a marker the playhead is inside (ignore the last second so the
  // button doesn't flash right as the marker ends).
  const m = App.markers.find(mk => posMs >= mk.start && posMs < mk.end - 1000) || null;
  if (m === App.activeMarker) return;
  App.activeMarker = m;
  const btn = document.getElementById('skip-marker-btn');
  if (m) {
    const SKIP_LABELS = {
      // Jellyfin media segments
      intro:      '⏭ Skip Intro',
      credits:    '⏭ Skip Credits',
      recap:      '⏭ Skip Recap',
      preview:    '⏭ Skip Preview',
      commercial: '⏭ Skip Ad',
      // SponsorBlock categories (YouTube)
      sponsor:    '⏭ Skip Sponsor',
      selfpromo:  '⏭ Skip Promo',
      outro:      '⏭ Skip Outro',
    };
    btn.textContent = SKIP_LABELS[m.type] || '⏭ Skip';
    btn.classList.remove('hidden');
    btn.onclick = skipCurrentMarker;
  } else {
    btn.classList.add('hidden');
  }
}

function hideSkipButton() {
  App.activeMarker = null;
  const btn = document.getElementById('skip-marker-btn');
  if (btn) btn.classList.add('hidden');
}

function skipCurrentMarker() {
  const m = App.activeMarker;
  if (!m) return;
  const v = document.getElementById('video-player');
  if (App.ytPlayback) {
    ytSeekTo(m.end / 1000);
  } else if (m.type === 'credits' && m.final && isFinite(v.duration)) {
    // Final credits marker: jump to the end so Next Up / finish logic kicks in
    v.currentTime = v.duration;
  } else {
    v.currentTime = m.end / 1000;
  }
  hideSkipButton();
  showToast('⏭ Skipped');
}

// ── Subtitle sideloading (fetch & render on the spot) ────────────────────
// Text subtitles are downloaded as WebVTT from the backend and rendered in
// our own overlay via a hidden <track>, so Direct Play / Direct Stream keep
// playing untouched. Only image subs (PGS/VobSub) need a burn-in transcode.
const IMAGE_SUB_CODECS = new Set([
  'pgs', 'hdmv_pgs_subtitle', 'vobsub', 'dvd_subtitle', 'dvb_subtitle',
]);

function subCueHtml(text) {
  // Escape everything, re-allow harmless inline tags (i/b/u), drop the rest
  // of the VTT/SRT markup (<v Speaker>, <c.yellow>, <font …>, …) entirely.
  const esc = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  return esc
    .replace(/&lt;(\/?)(i|b|u)&gt;/gi, '<$1$2>')
    .replace(/&lt;[^&]*?&gt;/g, '')
    .replace(/\n/g, '<br>');
}

function removeSideloadedSubs() {
  const v = document.getElementById('video-player');
  [...v.querySelectorAll('track')].forEach(t => {
    try { URL.revokeObjectURL(t.src); } catch (_) {}
    t.remove();
  });
  App.sideSubId = 0;
  const disp = document.getElementById('subtitle-display');
  disp.innerHTML = '';
}

async function sideloadSubtitle(t) {
  const v = document.getElementById('video-player');
  const r = await fetch(`/api/subtitle_vtt/${App.currentItem.ratingKey}/${t.id}`);
  if (!r.ok) throw new Error(`subtitle fetch failed (${r.status})`);
  const vtt = await r.text();

  removeSideloadedSubs();
  const el = document.createElement('track');
  el.kind = 'subtitles';
  el.label = t.title || 'Subtitles';
  el.src = URL.createObjectURL(new Blob([vtt], { type: 'text/vtt' }));
  v.appendChild(el);

  // 'hidden' keeps cue events firing while we render them ourselves —
  // native cue rendering is ugly and unstylable across browsers.
  const tt = el.track;
  tt.mode = 'hidden';
  tt.oncuechange = () => {
    const disp = document.getElementById('subtitle-display');
    const cues = [...(tt.activeCues || [])];
    disp.innerHTML = cues.map(c => subCueHtml(c.text)).join('<br>');
  };
  App.sideSubId = t.id;
}

// ── Track selector (audio/subs) ────────────────────────────────────────────
let currentTrackTab = 'subs';

function openTracksModal(initialTab) {
  if (App.ytPlayback) { showToast('Not available for YouTube'); return; }
  currentTrackTab = initialTab || 'subs';
  document.querySelectorAll('.track-tab').forEach(t => {
    t.classList.toggle('active', t.dataset.tracktab === currentTrackTab);
    t.onclick = () => { currentTrackTab = t.dataset.tracktab; renderTrackList(); };
  });
  renderTrackList();
  document.getElementById('btn-tracks-close').onclick = closeModal;
  openModal('modal-tracks', () => {
    rebuildTrackFocus();
  });
}

function renderTrackList() {
  const list = document.getElementById('track-list');
  list.innerHTML = '';
  const tracks = currentTrackTab === 'audio' ? App.audioTracks : App.subTracks;
  if (!tracks || !tracks.length) {
    list.innerHTML = '<div class="lib-loading">No tracks available</div>';
    return;
  }
  tracks.forEach(t => {
    const row = document.createElement('button');
    row.className = 'track-row focusable' + (t.selected ? ' selected' : '');
    const meta = [t.language, t.codec, t.channels ? `${t.channels}ch` : '', t.forced ? 'Forced' : '']
                 .filter(Boolean).join(' · ');
    row.innerHTML = `
      <span class="track-title">${t.title}</span>
      <span class="track-meta">${meta}</span>
      <span class="track-tick">${t.selected ? '✓' : ''}</span>
    `;
    row.onclick = async () => {
      try {
        const v   = document.getElementById('video-player');
        const pos = v.currentTime;

        // Persist the choice server-side either way — it is what the next
        // remux / transcode of this file is built from.
        const body = { part_id: App.partId };
        if (currentTrackTab === 'audio') body.audio_id = t.id;
        else                             body.sub_id   = t.id;
        await apiPost('/api/select_stream', body);
        tracks.forEach(x => x.selected = false);
        t.selected = true;
        closeModal();

        if (currentTrackTab === 'audio') {
          // The browser can't switch audio tracks inside a raw file — a
          // Direct Play stream must restart as a remux to hear the change.
          if (App.playbackMode === 'direct') App.forceNextMode = 'directStream';
          showToast('✓ Audio changed');
          playItem(App.currentItem, pos);
          return;
        }

        // ── Subtitles ──────────────────────────────────────────────
        if (!t.id) {                       // "Off"
          removeSideloadedSubs();
          if (App.playbackMode === 'transcode') {
            // Subs are burned into the current stream — restart clean
            playItem(App.currentItem, pos);
          } else {
            showToast('Subtitles off');
          }
          return;
        }

        const codec = (t.codec || '').toLowerCase();
        if (IMAGE_SUB_CODECS.has(codec)) {
          // Image-based subs can only be burned in by the server
          App.forceNextMode = 'transcode';
          showToast('✓ Subtitles on (burned in)');
          playItem(App.currentItem, pos);
          return;
        }

        if (App.playbackMode === 'transcode') {
          // Already transcoding — let Jellyfin burn the newly selected track
          showToast('✓ Subtitles changed');
          playItem(App.currentItem, pos);
          return;
        }

        // Direct Play / Direct Stream: download & render on the spot,
        // with no interruption to playback.
        showToast('⏳ Loading subtitles…');
        try {
          await sideloadSubtitle(t);
          showToast('✓ Subtitles on');
        } catch (err) {
          console.warn('[JHT] Subtitle sideload failed, burning in instead:', err);
          App.forceNextMode = 'transcode';
          playItem(App.currentItem, pos);
        }
      } catch (e) {
        showToast('⚠ Could not change track');
      }
    };
    list.appendChild(row);
  });
  rebuildTrackFocus();
}

function rebuildTrackFocus() {
  const tabs = [...document.querySelectorAll('.track-tab')];
  const rows = [...document.querySelectorAll('#track-list .track-row')];
  const close = document.getElementById('btn-tracks-close');
  App.focusGrid = [tabs].concat(rows.map(r => [r])).concat([[close]]);
  setFocus(1, 0);
}

// ── Quality selector ───────────────────────────────────────────────────────
const QUALITY_OPTIONS = [
  { value: 'original',     label: 'Original (max)' },
  { value: '3840x2160',    label: '4K (2160p)' },
  { value: '1920x1080',    label: '1080p' },
  { value: '1280x720',     label: '720p' },
  { value: '720x480',      label: '480p' },
];

// A section heading inside the quality list.
function qualityHeading(text) {
  const h = document.createElement('div');
  h.className = 'track-group-label';
  h.textContent = text;
  return h;
}

function openQualityModal() {
  if (App.ytPlayback) { showToast('Not available for YouTube'); return; }
  const list = document.getElementById('quality-list');
  list.innerHTML = '';

  // Versions — only when the title actually exists as more than one file,
  // e.g. a 1080p rip alongside a 4K remux. Picking one here is separate from
  // the resolution cap below: a version is a different source file, while a
  // resolution forces the server to transcode whatever version is playing.
  if (App.streamVersions.length > 1) {
    list.appendChild(qualityHeading('Version'));
    App.streamVersions.forEach(ver => {
      const active = ver.id === App.currentMediaSource;
      const row = document.createElement('button');
      row.className = 'track-row focusable' + (active ? ' selected' : '');
      row.innerHTML = `
        <span class="track-title">${ver.label}</span>
        <span class="track-tick">${active ? '✓' : ''}</span>
      `;
      row.onclick = () => {
        closeModal();
        if (active || !App.currentItem) return;
        App.currentMediaSource = ver.id;
        showToast(`Version: ${ver.label}`);
        const v = document.getElementById('video-player');
        playItem(App.currentItem, v.currentTime);
      };
      list.appendChild(row);
    });
    list.appendChild(qualityHeading('Quality'));
  }

  QUALITY_OPTIONS.forEach(opt => {
    const row = document.createElement('button');
    row.className = 'track-row focusable' +
      (opt.value === App.currentQualityResolution ? ' selected' : '');
    row.innerHTML = `
      <span class="track-title">${opt.label}</span>
      <span class="track-tick">${opt.value === App.currentQualityResolution ? '✓' : ''}</span>
    `;
    row.onclick = () => {
      App.currentQualityResolution = opt.value;
      showToast(`Quality: ${opt.label}`);
      closeModal();
      // Reload from current position with new quality
      if (App.currentItem) {
        const v = document.getElementById('video-player');
        const pos = v.currentTime;
        playItem(App.currentItem, pos);
      }
    };
    list.appendChild(row);
  });
  document.getElementById('btn-quality-close').onclick = closeModal;
  openModal('modal-quality', () => {
    const rows = [...list.querySelectorAll('.track-row')];
    const close = document.getElementById('btn-quality-close');
    App.focusGrid = rows.map(r => [r]).concat([[close]]);
    setFocus(0, 0);
  });
}

// ── Adjacent-episode lookup (shared by Next Up and remote Prev/Next) ──────
async function findAdjacentEpisode(currentEp, dir) {
  const seasonEps = await api(`/api/metadata/${currentEp.parentRatingKey}/children`);
  const idx = seasonEps.findIndex(e => e.ratingKey == currentEp.ratingKey);
  if (idx < 0) return null;

  const target = idx + dir;
  if (target >= 0 && target < seasonEps.length) return seasonEps[target];

  // Crossed a season boundary — look in the neighbouring season
  const seasons = await api(`/api/metadata/${currentEp.grandparentRatingKey}/children`);
  const seasonIdx = seasons.findIndex(s => s.ratingKey == currentEp.parentRatingKey);
  if (seasonIdx < 0) return null;
  const adjSeason = seasons[seasonIdx + dir];
  if (!adjSeason) return null;
  const adjEps = await api(`/api/metadata/${adjSeason.ratingKey}/children`);
  if (!adjEps.length) return null;
  return dir > 0 ? adjEps[0] : adjEps[adjEps.length - 1];
}

// ── Next-up countdown ──────────────────────────────────────────────────────
async function tryShowNextUp(currentEp) {
  try {
    const next = await findAdjacentEpisode(currentEp, +1);
    if (!next) {
      showToast('✅ Finished!');
      setTimeout(() => goBack(), 1500);
      return;
    }
    showNextUp(next);
  } catch (e) {
    console.warn('next-up failed:', e);
    setTimeout(() => goBack(), 1500);
  }
}

function showNextUp(next) {
  const el = document.getElementById('next-up');
  el.classList.remove('hidden');
  document.getElementById('next-up-thumb').src = imgUrl(next.thumb || next.parentThumb, 480, 270);
  document.getElementById('next-up-title').textContent = next.grandparentTitle || next.title;
  document.getElementById('next-up-subtitle').textContent =
    `S${next.parentIndex || ''}E${next.index || ''} · ${next.title || ''}`;
  document.getElementById('next-up-summary').textContent = (next.summary || '').slice(0, 220);

  let secs = 10;
  document.getElementById('next-up-counter').textContent = secs;
  document.getElementById('next-up-bar-fill').style.width = '0%';

  const startMs = Date.now();
  const totalMs = secs * 1000;

  App.nextUpInterval = setInterval(() => {
    const elapsed = Date.now() - startMs;
    const remain = Math.max(0, Math.ceil((totalMs - elapsed) / 1000));
    document.getElementById('next-up-counter').textContent = remain;
    document.getElementById('next-up-bar-fill').style.width = (100 * elapsed / totalMs) + '%';
    if (elapsed >= totalMs) {
      cancelNextUp();
      maybeResume(next);
    }
  }, 100);

  document.getElementById('btn-next-play').onclick = () => {
    cancelNextUp();
    maybeResume(next);
  };
  document.getElementById('btn-next-cancel').onclick = () => {
    cancelNextUp();
    stopPlayback();
    goBack();
  };

  // Focus the play button
  pushFocus();
  App.focusGrid = [
    [document.getElementById('btn-next-play')],
    [document.getElementById('btn-next-cancel')],
  ];
  setFocus(0, 0);
  App.modalOpen = 'next-up';
}

function cancelNextUp() {
  clearInterval(App.nextUpInterval);
  App.nextUpInterval = null;
  document.getElementById('next-up').classList.add('hidden');
  if (App.modalOpen === 'next-up') {
    App.modalOpen = null;
    popFocus();
  }
}

// ── Modal manager ──────────────────────────────────────────────────────────
function openModal(id, buildFocus) {
  const m = document.getElementById(id);
  if (!m) return;
  pushFocus();
  m.classList.remove('hidden');
  App.modalOpen = id;
  if (buildFocus) buildFocus();
}

function closeModal() {
  if (!App.modalOpen) return;
  const m = document.getElementById(App.modalOpen);
  if (m) m.classList.add('hidden');
  App.modalOpen = null;
  popFocus();
}

// ── WebSocket ──────────────────────────────────────────────────────────────
// Both transports, in this order. The server runs Socket.IO in threading mode
// (see app.py for why), which serves WebSockets but will not accept one as the
// *opening* transport — so pinning 'websocket' alone left the socket unable to
// connect at all, retrying forever, and the remote dead. Starting on polling
// connects immediately and Socket.IO then upgrades itself to a WebSocket, so
// this costs nothing: measured round-trip is 2-3 ms on a LAN.
const socket = io(window.SOCKET_URL, { transports: ['polling', 'websocket'] });

socket.on('connect',    () => updateConnBadge(true));
socket.on('disconnect', () => updateConnBadge(false));
socket.on('command',    (data) => handleCommand(data));

// OK/Enter skips the intro/credits only while the player overlay is idle-
// hidden; when the overlay is up, OK must keep activating the focused button.
function skipPromptTakesOk(overlayWasHidden) {
  return App.activeMarker && App.screen === 'player' &&
         !App.modalOpen && overlayWasHidden;
}

function handleCommand(data) {
  const { action, value } = data;
  const overlayWasHidden = document.getElementById('player-overlay')
                             .classList.contains('overlay-hidden');
  resetOverlayTimer();
  bumpIdle();
  if (App.ssActive) { hideScreensaver(); return; }

  const vid = document.getElementById('video-player');

  switch (action) {
    case 'up':     navigate('up');     break;
    case 'down':   navigate('down');   break;
    case 'left':   navigate('left');   break;
    case 'right':  navigate('right');  break;
    case 'select':
      if (skipPromptTakesOk(overlayWasHidden)) skipCurrentMarker();
      else selectFocused();
      break;
    case 'back':   goBack();           break;
    case 'home':
      if (App.screen === 'player') stopPlayback();
      App.history = [];
      showScreen('home', { _isBack: true });
      break;
    case 'search': openSearch();       break;

    case 'play':         vid?.play(); break;
    case 'pause':        vid?.pause(); break;
    case 'toggle_play':  togglePlayPause(); break;
    case 'stop':
      if (App.screen === 'player') {
        stopPlayback();
        App.history = [];
        showScreen('home', { _isBack: true });
      }
      break;
    case 'previous': playAdjacent(-1); break;
    case 'next':     playAdjacent(+1); break;
    case 'seek_back': seekRelative(-10); break;
    case 'seek_fwd':  seekRelative(+10); break;
    case 'seek':
      if (value !== undefined && App.ytPlayback) {
        ytSeekTo((value / 100) * (App.ytPlayback.duration / 1000));
      } else if (value !== undefined && vid && isFinite(vid.duration)) {
        vid.currentTime = (value / 100) * vid.duration;
      }
      break;
    case 'volume':   setVolume(value); break;
    case 'vol_up':   if (vid) setVolume(vid.volume * 100 + 10); break;
    case 'vol_down': if (vid) setVolume(vid.volume * 100 - 10); break;
    case 'mute':     toggleMute(); break;
    case 'toggle_subs': openTracksModal('subs'); break;
    case 'toggle_audio': openTracksModal('audio'); break;
    case 'aspect':
      if (App.screen === 'player') cycleAspect();
      break;
    case 'fullscreen':
      if (!document.fullscreenElement) document.documentElement.requestFullscreen();
      else document.exitFullscreen();
      break;
  }
}

// ── Prev / Next from the remote ────────────────────────────────────────────
// Matches real player behaviour: "previous" restarts the current title unless
// pressed within the first 5 seconds of an episode, where it goes to the
// previous episode; "next" jumps to the next episode.
async function playAdjacent(dir) {
  const vid = document.getElementById('video-player');
  const item = App.currentItem;
  if (App.screen !== 'player' || !item) return;

  if (dir < 0 && (vid.currentTime > 5 || item.type !== 'episode')) {
    vid.currentTime = 0;
    showToast('⏮ Restarted');
    return;
  }
  if (item.type !== 'episode') {
    showToast('⏭ No next title');
    return;
  }
  try {
    const adj = await findAdjacentEpisode(item, dir);
    if (adj) {
      playItem(adj, 0);
    } else {
      showToast(dir > 0 ? '⏭ No next episode' : '⏮ No previous episode');
    }
  } catch (e) {
    console.warn('playAdjacent failed:', e);
  }
}

function broadcastState(extra = {}) {
  const v = document.getElementById('video-player');
  const s = {
    screen:   App.screen,
    playing:  v ? !v.paused : false,
    title:    App.currentItem?.title || '',
    subtitle: App.currentItem?.type === 'episode'
              ? `S${App.currentItem?.parentIndex}E${App.currentItem?.index}`
              : (App.currentItem?.year ? String(App.currentItem.year) : ''),
    thumb:    App.currentItem?.thumb || '',
    position: App.ytPlayback ? ytPositionMs() : (v ? v.currentTime * 1000 : 0),
    duration: App.ytPlayback ? App.ytPlayback.duration
              : (v?.duration && isFinite(v.duration) ? v.duration * 1000
                 : (App.streamDuration || 0)),
    volume:   v ? Math.round(v.volume * 100) : 100,
    ...extra,
  };
  socket.emit('state_update', s);
}

// ── Keyboard navigation ────────────────────────────────────────────────────
document.addEventListener('keydown', (e) => {
  bumpIdle();
  if (App.ssActive) { hideScreensaver(); return; }

  // OSK on search / YouTube screens accepts typed characters
  const oskTarget = App.screen === 'search'  ? oskPress
                  : App.screen === 'youtube' ? ytOskPress
                  : null;
  if (oskTarget && !App.modalOpen) {
    if (/^[a-zA-Z0-9 ]$/.test(e.key)) {
      e.preventDefault();
      oskTarget(e.key === ' ' ? 'Space' : e.key.toUpperCase());
      return;
    }
    const q = App.screen === 'search' ? searchQuery : ytQuery;
    if (e.key === 'Backspace' && q.length) {
      e.preventDefault();
      oskTarget('Backspace');
      return;
    }
  }

  switch (e.key) {
    case 'ArrowUp':    e.preventDefault(); navigate('up');    break;
    case 'ArrowDown':  e.preventDefault(); navigate('down');  break;
    case 'ArrowLeft':  e.preventDefault(); navigate('left');  break;
    case 'ArrowRight': e.preventDefault(); navigate('right'); break;
    case 'Enter': {
      e.preventDefault();
      const overlayWasHidden = document.getElementById('player-overlay')
                                 .classList.contains('overlay-hidden');
      if (skipPromptTakesOk(overlayWasHidden)) skipCurrentMarker();
      else selectFocused();
      break;
    }
    case 'Backspace':
    case 'Escape':     e.preventDefault(); goBack();          break;
    case ' ':
      // Space only controls playback inside the player
      if (App.screen === 'player') { e.preventDefault(); togglePlayPause(); }
      break;
    case 'f':
    case 'F':
      if (!document.fullscreenElement) document.documentElement.requestFullscreen();
      else document.exitFullscreen();
      break;
    case 'z':
    case 'Z':
      if (App.screen === 'player') { e.preventDefault(); cycleAspect(); }
      break;
    case 'l':
    case 'L':
      e.preventDefault();
      cycleLightMode();
      break;
    case '/':
      e.preventDefault();
      if (App.screen !== 'player') openSearch();
      break;
  }
  resetOverlayTimer();
});

// ── Aspect: apply on each new video and on resize ────────────────────────────
(() => {
  const v = document.getElementById('video-player');
  try { App.aspectMode = localStorage.getItem('pht_aspect') || 'auto'; } catch (_) {}
  if (v) {
    v.addEventListener('loadedmetadata', () => { applyAspect(); scheduleBakedBarCheck(); });
    window.addEventListener('resize', applyAspect);
  }
})();

// ── Ambient lights (WLED) ──────────────────────────────────────────────────
// L cycles the strips through the same three modes the phone remote offers,
// so the lights can be changed from the TV keyboard/remote without picking up
// a phone or alt-tabbing to SignalRGB.
const LIGHT_MODES = ['ambilight', 'manual', 'off'];
const LIGHT_MODE_LABELS = {
  ambilight: '🎬 Lights: Ambilight',
  manual:    '💡 Lights: Manual',
  off:       '🌙 Lights: Off',
};

async function cycleLightMode() {
  let st;
  try {
    st = await api('/api/wled/state');
  } catch (_) {
    return;                      // lights aren't reachable; stay quiet
  }
  if (!st.configured) {
    showToast('No WLED controller configured');
    return;
  }
  // An unknown mode (controllers disagreeing) resolves to the first one,
  // which also brings them back into step.
  const next = LIGHT_MODES[(LIGHT_MODES.indexOf(st.mode) + 1) % LIGHT_MODES.length];
  try {
    await apiPost('/api/wled/mode', { mode: next });
    showToast(LIGHT_MODE_LABELS[next] || `Lights: ${next}`);
  } catch (_) {
    showToast('⚠ Could not reach the lights');
  }
}

// ── UI Helpers ─────────────────────────────────────────────────────────────
let toastTimer;
function showToast(msg) {
  const toast = document.getElementById('toast');
  toast.textContent = msg;
  toast.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove('show'), 2200);
}

let qualityHideTimer;
function showQualityBadge(label, mode) {
  let badge = document.getElementById('quality-badge');
  if (!badge) {
    badge = document.createElement('div');
    badge.id = 'quality-badge';
    document.getElementById('screen-player').appendChild(badge);
  }
  const icon = (mode === false) ? '▶' : (mode === 'stream') ? '⇄' : '⚙';
  const cls  = (mode === false) ? 'direct' : (mode === 'stream') ? 'stream' : 'transcode';
  badge.textContent = icon + ' ' + label;
  badge.className = 'quality-badge quality-badge-visible ' + cls;
  clearTimeout(qualityHideTimer);
  qualityHideTimer = setTimeout(() => badge.classList.remove('quality-badge-visible'), 6000);
}

function updateConnBadge(connected) {
  const badge = document.getElementById('connection-status');
  const label = badge.querySelector('.conn-label');
  badge.className = 'conn-badge ' + (connected ? 'conn-connected' : 'conn-disconnected');
  label.textContent = connected ? 'Remote Connected' : 'Remote Disconnected';
}

function hideLoading() {
  const el = document.getElementById('loading-screen');
  el.classList.add('fade-out');
  setTimeout(() => el.classList.add('hidden'), 700);
}

// ── Hero / Topbar button wiring ────────────────────────────────────────────
document.querySelector('[data-action="play-hero"]')?.addEventListener('click', () => {
  if (App.heroItem) openItem(App.heroItem);
});
document.querySelector('[data-action="info-hero"]')?.addEventListener('click', () => {
  if (App.heroItem) showDetail(App.heroItem);
});
document.querySelector('[data-action="open-search"]')?.addEventListener('click', () => {
  openSearch();
});
document.getElementById('app-youtube')?.addEventListener('click', () => {
  openYouTube();
});

// ── Auto-fullscreen on TV page ─────────────────────────────────────────────
document.addEventListener('click', () => {
  if (!document.fullscreenElement) {
    document.documentElement.requestFullscreen().catch(() => {});
  }
}, { once: true });

// ── Screensaver ────────────────────────────────────────────────────────────
function bumpIdle() {
  if (App.ssActive) hideScreensaver();
  clearTimeout(App.ssIdleTimer);
  // Only the home screen idles into the screensaver (playback shouldn't dim)
  if (App.screen === 'home' && !App.modalOpen) {
    App.ssIdleTimer = setTimeout(showScreensaver, App.IDLE_MS);
  }
}

function showScreensaver() {
  if (!App.ssArt.length) return;
  App.ssActive = true;
  const ss = document.getElementById('screensaver');
  ss.classList.remove('hidden');
  rotateScreensaver();
  App.ssRotateTimer = setInterval(rotateScreensaver, 12000);
  tickClock();
  App.ssClockTimer = setInterval(tickClock, 30000);
}

function hideScreensaver() {
  App.ssActive = false;
  document.getElementById('screensaver').classList.add('hidden');
  clearInterval(App.ssRotateTimer);
  clearInterval(App.ssClockTimer);
}

function rotateScreensaver() {
  if (!App.ssArt.length) return;
  const pick = App.ssArt[Math.floor(Math.random() * App.ssArt.length)];
  const bd = document.getElementById('ss-backdrop');
  bd.style.backgroundImage = `url("${imgUrl(pick.art, 1920, 1080)}")`;
  document.getElementById('ss-title').textContent = pick.title || '';
  document.getElementById('ss-year').textContent  = pick.year || '';
}

function tickClock() {
  const d = new Date();
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  document.getElementById('ss-clock').textContent = `${hh}:${mm}`;
}

// Bump idle on any interaction
['mousemove','keydown','click','touchstart'].forEach(ev =>
  document.addEventListener(ev, bumpIdle, { passive: true })
);

// ── Init ───────────────────────────────────────────────────────────────────
showScreen('home');
loadHome();
