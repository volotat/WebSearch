import StarRatingComponent from '/modules/StarRating.js';
import FolderViewComponent from '/modules/FolderViewComponent.js';
import ContextMenuComponent from '/modules/ContextMenuComponent.js';
import SearchBarComponent from '/modules/SearchBarComponent.js';

// ── State (initialised from URL on every page load) ────────────────────
const PAGE_LIMIT = 14;
const _urlParams = new URLSearchParams(window.location.search);
let currentPage = parseInt(_urlParams.get('page')) || 1;
let currentPath = decodeURIComponent(_urlParams.get('path') || '');
let searchState = {
  text_query:  decodeURIComponent(_urlParams.get('text_query') || ''),
  mode:        _urlParams.get('mode')        || 'file-name',
  order:       _urlParams.get('order')       || 'most-relevant',
  temperature: parseFloat(_urlParams.get('temperature')) || 0,
  seed:        _urlParams.get('seed')        || null,
};

// Add-page modal state
let _addModalRating = null;
let _addModalStarInstance = null;

// Recrawl modal state
let _recrawlIsBulk = false;

// Shared context menu instance
const _contextMenu = new ContextMenuComponent();

// ── Helpers ──────────────────────────────────────────────────────────────

function truncate(str, max = 120) {
  if (!str) return '';
  return str.length > max ? str.slice(0, max) + '…' : str;
}

/**
 * Navigate to the current page with updated URL params.
 */
function _navigateTo(updates) {
  const p = new URLSearchParams(window.location.search);
  for (const [k, v] of Object.entries(updates)) {
    if (v === null || v === undefined || v === '') {
      p.delete(k);
    } else {
      p.set(k, String(v));
    }
  }
  window.location.search = p.toString();
}

/**
 * Build a single wide card element for a file entry returned by FileManager.
 * fileData = { file_path, hash, base_name, file_size, file_info: {…} }
 */
function buildCard(fileData) {
  const info = fileData.file_info || {};
  const card = document.createElement('div');
  card.className = 'ws-card';
  card.dataset.filePath = fileData.file_path;

  // ── Body (title + url + preview) ──────────────────────────────────
  const body = document.createElement('div');
  body.className = 'ws-card-body';

  // Title (clickable → opens modal)
  const titleEl = document.createElement('div');
  titleEl.className = 'ws-card-title';
  titleEl.style.whiteSpace = 'normal';
  const titleLink = document.createElement('a');
  titleLink.href = '#';
  titleLink.textContent = info.title || fileData.base_name || fileData.file_path;
  titleLink.addEventListener('click', (e) => {
    e.preventDefault();
    openPageModal(fileData);
  });
  titleEl.appendChild(titleLink);
  body.appendChild(titleEl);

  // URL (opens actual page in new tab)
  if (info.url) {
    const urlEl = document.createElement('div');
    urlEl.className = 'ws-card-url';
    const urlLink = document.createElement('a');
    urlLink.href = info.url;
    urlLink.target = '_blank';
    urlLink.rel = 'noopener';
    urlLink.textContent = info.url;
    urlLink.addEventListener('click', () => {
      // Mark as viewed
      if (fileData.hash) {
        socket.emit('emit_WebSearch_mark_viewed', { hash: fileData.hash });
      }
    });
    urlEl.appendChild(urlLink);
    body.appendChild(urlEl);
  }

  // Preview text
  const previewEl = document.createElement('div');
  previewEl.className = 'ws-card-preview';
  previewEl.textContent = info.preview_text || '';
  body.appendChild(previewEl);

  card.appendChild(body);

  // ── Star rating ───────────────────────────────────────────────────
  const ratingWrap = document.createElement('div');
  ratingWrap.className = 'ws-card-rating';

  const hasUserRating = info.user_rating !== null && info.user_rating !== undefined;
  const displayRating = hasUserRating ? info.user_rating : info.model_rating;

  const starRating = new StarRatingComponent({
    initialRating: displayRating,
    callback: (rating) => {
      socket.emit('emit_WebSearch_set_rating', {
        hash: fileData.hash,
        file_path: fileData.file_path,
        rating: rating,
      }, (resp) => {
        if (resp && !resp.error) {
          info.user_rating = rating;
          starRating.isUserRated = true;
          starRating.updateAllContainers();
        }
      });
    },
  });
  starRating.isUserRated = hasUserRating;

  const starEl = starRating.issueNewHtmlComponent({
    containerType: 'span',
    size: 22,
    isActive: true,
  });
  ratingWrap.appendChild(starEl);
  card.appendChild(ratingWrap);

  return card;
}

// ── Pagination ───────────────────────────────────────────────────────────

function renderPagination(page, total, limit) {
  const container = document.getElementById('ws_pagination');
  container.innerHTML = '';
  const totalPages = Math.ceil(total / limit);
  if (totalPages <= 1) return;

  const _pageUrl = (num) => {
    const p = new URLSearchParams(window.location.search);
    p.set('page', String(num));
    return '?' + p.toString();
  };

  for (let i = 1; i <= totalPages; i++) {
    if (i === 1 || i === totalPages || Math.abs(i - page) <= 2) {
      const li = document.createElement('li');
      const a = document.createElement('a');
      a.className = 'pagination-link' + (i === page ? ' is-current' : '');
      a.textContent = i;
      a.href = _pageUrl(i);
      li.appendChild(a);
      container.appendChild(li);
    } else if (
      (i === 2 && page > 4) ||
      (i === totalPages - 1 && page < totalPages - 3)
    ) {
      const li = document.createElement('li');
      li.innerHTML = '<span class="pagination-ellipsis">&hellip;</span>';
      container.appendChild(li);
    }
  }
}

// ── Data fetching ────────────────────────────────────────────────────────

function fetchFiles() {
  const payload = {
    page: currentPage,
    limit: PAGE_LIMIT,
    pagination: (currentPage - 1) * PAGE_LIMIT,
    text_query:  searchState.text_query,
    mode:        searchState.mode,
    order:       searchState.order,
    temperature: searchState.temperature,
    seed:        searchState.seed,
  };
  if (currentPath) payload.path = currentPath;

  socket.emit('emit_WebSearch_get_files', payload, (response) => {
    const container = document.getElementById('ws_pages_container');
    container.innerHTML = '';

    if (!response || !response.files_data) return;

    response.files_data.forEach((fileData) => {
      container.appendChild(buildCard(fileData));
    });

    const total = response.total_files || 0;
    renderPagination(currentPage, total, PAGE_LIMIT);
    document.querySelector('.search-status').textContent =
      `${total} page${total !== 1 ? 's' : ''}`;
  });
}

function fetchFolders() {
  socket.emit('emit_WebSearch_get_folders', { path: currentPath }, (response) => {
    const treeContainer = document.getElementById('ws_folder_tree');
    treeContainer.innerHTML = '';

    if (!response || !response.folders) return;

    const folderView = new FolderViewComponent(response.folders, response.folder_path || currentPath, false);
    treeContainer.appendChild(folderView.getDOMElement());
    _bindFolderTreeClicks();
  });
}

// Set up folder tree click interception once (delegated)
let _folderTreeListenerBound = false;
function _bindFolderTreeClicks() {
  if (_folderTreeListenerBound) return;
  _folderTreeListenerBound = true;

  document.getElementById('ws_folder_tree').addEventListener('click', (e) => {
    const link = e.target.closest('a');
    if (!link) return;
    e.preventDefault();

    // Extract path from href like "?path=encoded_path"
    const href = link.getAttribute('href') || '';
    const params = new URLSearchParams(href.replace(/^\?/, ''));
    const clickedPath = params.get('path') || '';

    // Toggle: if clicking the already-active folder, go back to root
    const newPath = (currentPath === clickedPath) ? '' : clickedPath;
    _navigateTo({ path: newPath || null, page: 1 });
  });

  // Right-click on folder links → recrawl option
  document.getElementById('ws_folder_tree').addEventListener('contextmenu', (e) => {
    const link = e.target.closest('a');
    if (!link) return;
    e.preventDefault();

    const href = link.getAttribute('href') || '';
    const params = new URLSearchParams(href.replace(/^\?/, ''));
    const folderPath = params.get('path') || '';

    _contextMenu.show(e.pageX, e.pageY, [
      {
        label: '&#x21BB; Recrawl folder',
        action: () => openRecrawlModal(folderPath),
      },
    ]);
  });
}

// ── Modal ────────────────────────────────────────────────────────────────

function openPageModal(fileData) {
  const info = fileData.file_info || {};
  const modal = document.getElementById('ws_page_modal');
  document.getElementById('ws_modal_title').textContent = info.title || fileData.base_name || fileData.file_path;
  document.getElementById('ws_modal_link').href = info.url || '#';
  document.getElementById('ws_modal_body').innerHTML = 'Loading…';

  // Star rating in modal
  const ratingContainer = document.getElementById('ws_modal_rating');
  ratingContainer.innerHTML = '';

  const hasUserRating = info.user_rating !== null && info.user_rating !== undefined;
  const displayRating = hasUserRating ? info.user_rating : info.model_rating;

  const modalStarRating = new StarRatingComponent({
    initialRating: displayRating,
    callback: (rating) => {
      socket.emit('emit_WebSearch_set_rating', {
        hash: fileData.hash,
        file_path: fileData.file_path,
        rating: rating,
      }, (resp) => {
        if (resp && !resp.error) {
          info.user_rating = rating;
          modalStarRating.isUserRated = true;
          modalStarRating.updateAllContainers();
        }
      });
    },
  });
  modalStarRating.isUserRated = hasUserRating;

  const starEl = modalStarRating.issueNewHtmlComponent({
    containerType: 'span',
    size: 26,
    isActive: true,
  });
  ratingContainer.appendChild(starEl);

  modal.classList.add('is-active');

  // Fetch markdown content
  socket.emit('emit_WebSearch_get_page_content', { file_path: fileData.file_path }, (resp) => {
    if (resp && resp.content) {
      marked.setOptions({ breaks: true, gfm: true });
      const html = DOMPurify.sanitize(marked.parse(resp.content));
      document.getElementById('ws_modal_body').innerHTML = html;
    } else {
      document.getElementById('ws_modal_body').textContent = 'Could not load content.';
    }
  });
}

// ── Recrawl modal (folder-based) ────────────────────────────────────────────────

function openRecrawlModal(folderPath) {
  _recrawlIsBulk = false;
  document.getElementById('ws_recrawl_folder').value = folderPath || '/';
  document.getElementById('ws_recrawl_url').value = '';
  document.getElementById('ws_recrawl_sublinks_cb').checked = false;
  document.getElementById('ws_recrawl_delay_input').value = '0.5';
  document.getElementById('ws_recrawl_max_pages_input').value = '300';
  document.getElementById('ws_recrawl_bulk_notice').style.display = 'none';
  document.getElementById('ws_recrawl_single_fields').style.display = '';

  document.getElementById('ws_recrawl_modal').classList.add('is-active');

  // Pre-populate from .crawl.yaml stored in the folder
  socket.emit('emit_WebSearch_read_crawl_yaml', { path: folderPath }, (conf) => {
    if (!conf) return;

    if (conf.mode === 'bulk') {
      // Top-level folder with multiple sub-sites — switch to bulk mode
      _recrawlIsBulk = true;
      document.getElementById('ws_recrawl_single_fields').style.display = 'none';
      const notice = document.getElementById('ws_recrawl_bulk_notice');
      notice.style.display = '';
      const ul = document.getElementById('ws_recrawl_bulk_sites');
      ul.innerHTML = (conf.sites || []).map(s => `<li>${s}</li>`).join('');
      return;
    }

    if (conf.seed_url) document.getElementById('ws_recrawl_url').value = conf.seed_url;
    if (conf.sublinks_only !== undefined) document.getElementById('ws_recrawl_sublinks_cb').checked = !!conf.sublinks_only;
    if (conf.crawl_delay !== undefined) document.getElementById('ws_recrawl_delay_input').value = conf.crawl_delay;
    if (conf.max_pages !== undefined) document.getElementById('ws_recrawl_max_pages_input').value = conf.max_pages;
    setTimeout(() => document.getElementById('ws_recrawl_url').focus(), 50);
  });
}

function closeRecrawlModal() {
  document.getElementById('ws_recrawl_modal').classList.remove('is-active');
}

function confirmRecrawlModal() {
  const folderPath = document.getElementById('ws_recrawl_folder').value.trim();
  const crawlDelay = parseFloat(document.getElementById('ws_recrawl_delay_input').value) || 0.5;
  const maxPages = parseInt(document.getElementById('ws_recrawl_max_pages_input').value, 10) || 300;
  closeRecrawlModal();

  const payload = { path: folderPath, crawl_delay: crawlDelay, max_pages: maxPages };
  if (!_recrawlIsBulk) {
    payload.url = document.getElementById('ws_recrawl_url').value.trim();
    payload.sublinks_only = document.getElementById('ws_recrawl_sublinks_cb').checked;
  }

  socket.emit('emit_WebSearch_recrawl_folder', payload, (resp) => {
    if (resp && resp.error) {
      alert('Recrawl error: ' + resp.error);
    }
  });
}

// ── Add-page modal ─────────────────────────────────────────────────────────────────

function openAddModal() {
  _addModalRating = null;
  _addModalStarInstance = null;

  // Reset all fields
  document.getElementById('ws_add_modal_url').value = '';
  document.getElementById('ws_add_crawl_cb').checked = false;
  document.getElementById('ws_add_sublinks_cb').checked = false;
  document.getElementById('ws_add_sublinks_cb').disabled = true;
  document.getElementById('ws_add_delay_input').value = '0.5';
  document.getElementById('ws_add_delay_input').disabled = true;
  document.getElementById('ws_add_max_pages_input').value = '5000';
  document.getElementById('ws_add_max_pages_input').disabled = true;

  // Build optional star rating
  const ratingContainer = document.getElementById('ws_add_modal_rating');
  ratingContainer.innerHTML = '';
  _addModalStarInstance = new StarRatingComponent({
    initialRating: null,
    callback: (rating) => {
      _addModalRating = rating;
    },
  });
  const starEl = _addModalStarInstance.issueNewHtmlComponent({
    containerType: 'span',
    isActive: true,
  });
  ratingContainer.appendChild(starEl);

  document.getElementById('ws_add_modal').classList.add('is-active');
  setTimeout(() => document.getElementById('ws_add_modal_url').focus(), 50);
}

function closeAddModal() {
  document.getElementById('ws_add_modal').classList.remove('is-active');
}

function confirmAddModal() {
  const url = document.getElementById('ws_add_modal_url').value.trim();
  if (!url) {
    document.getElementById('ws_add_modal_url').focus();
    return;
  }

  const shouldCrawl = document.getElementById('ws_add_crawl_cb').checked;
  const sublinksOnly = document.getElementById('ws_add_sublinks_cb').checked;
  const crawlDelay = parseFloat(document.getElementById('ws_add_delay_input').value) || 0.5;
  const maxPages = parseInt(document.getElementById('ws_add_max_pages_input').value, 10) || 5000;
  const rating = _addModalRating;

  closeAddModal();

  if (shouldCrawl) {
    const payload = { url, sublinks_only: sublinksOnly, crawl_delay: crawlDelay, max_pages: maxPages };
    if (rating !== null) payload.seed_user_rating = rating;
    socket.emit('emit_WebSearch_crawl_site', payload);
  } else {
    const payload = { url };
    if (rating !== null) payload.user_rating = rating;
    socket.emit('emit_WebSearch_add_page', payload);
  }
}

// ── Init ─────────────────────────────────────────────────────────────────

$(document).ready(function () {
  // Fetch initial data
  fetchFolders();
  fetchFiles();

  // ── Recrawl modal controls ────────────────────────────────────────────
  document.querySelectorAll('.ws-recrawl-modal-close').forEach((el) => {
    el.addEventListener('click', closeRecrawlModal);
  });
  document.getElementById('ws_recrawl_modal_confirm').addEventListener('click', confirmRecrawlModal);
  document.getElementById('ws_recrawl_modal_cancel').addEventListener('click', closeRecrawlModal);

  // ── Add page button → opens modal ────────────────────────────────
  document.getElementById('ws_add_page_btn').addEventListener('click', openAddModal);

  // ── Add-page modal controls ───────────────────────────────────────
  document.querySelectorAll('.ws-add-modal-close').forEach((el) => {
    el.addEventListener('click', closeAddModal);
  });
  document.getElementById('ws_add_modal_confirm').addEventListener('click', confirmAddModal);
  document.getElementById('ws_add_modal_cancel').addEventListener('click', closeAddModal);

  // Allow Enter in the URL field to confirm
  document.getElementById('ws_add_modal_url').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') confirmAddModal();
  });

  // Toggle crawl-dependent controls when crawl checkbox is toggled
  document.getElementById('ws_add_crawl_cb').addEventListener('change', function () {
    const on = this.checked;
    document.getElementById('ws_add_sublinks_cb').disabled = !on;
    document.getElementById('ws_add_delay_input').disabled = !on;
    document.getElementById('ws_add_max_pages_input').disabled = !on;
    if (!on) {
      document.getElementById('ws_add_sublinks_cb').checked = false;
    }
  });

  // ── Search bar ────────────────────────────────────────────────────
  const _searchBar = new SearchBarComponent({
    container: document.getElementById('ws_search_bar'),
    enableModes: ['file-name', 'semantic-content'],
    showOrder: true,
    showTemperature: true,
    temperatures: [0, 0.2, 1, 2],
    keywords: ['rating', 'recommendation', 'recent'],
    autoSyncUrl: true,
    ensureDefaultsInUrl: true,
  });
  // Sync searchState from the component (which has already read/normalised URL params)
  Object.assign(searchState, _searchBar.getState());
  _searchBar.searchInput.placeholder =
    'Search pages by title/URL, or enter: rating · recommendation · recent';

  // ── Close modal ───────────────────────────────────────────────────
  document.querySelectorAll('.ws-modal-close').forEach((el) => {
    el.addEventListener('click', () => {
      document.getElementById('ws_page_modal').classList.remove('is-active');
    });
  });

  // ── Live events from server ───────────────────────────────────────
  socket.on('emit_WebSearch_page_added', () => {
    fetchFolders();
    fetchFiles();
  });

  socket.on('emit_WebSearch_crawl_progress', (data) => {
    document.getElementById('ws_crawl_status').textContent = data.message || '';
  });

  socket.on('emit_WebSearch_crawl_done', () => {
    fetchFolders();
    fetchFiles();
  });

  socket.on('emit_show_search_status', (status) => {
    document.querySelector('.search-status').textContent = status;
  });
});
