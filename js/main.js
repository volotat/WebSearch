import StarRatingComponent from '/modules/StarRating.js';
import FolderViewComponent from '/modules/FolderViewComponent.js';
import ContextMenuComponent from '/modules/ContextMenuComponent.js';

// ── State ────────────────────────────────────────────────────────────────
let currentPage = 1;
const PAGE_LIMIT = 14;
let currentDomain = null;   // null = all sites
let currentPath = '';        // md_file_path prefix for folder filtering
let currentOrder = 'rating';
// Add-page modal state
let _addModalRating = null;
let _addModalStarInstance = null;

// Shared context menu instance
const _contextMenu = new ContextMenuComponent();
// ── Helpers ──────────────────────────────────────────────────────────────

function truncate(str, max = 120) {
  if (!str) return '';
  return str.length > max ? str.slice(0, max) + '…' : str;
}

/**
 * Build a single wide card element for a WebPage record.
 */
function buildCard(page) {
  const card = document.createElement('div');
  card.className = 'ws-card';
  card.dataset.pageId = page.id;

  // ── Body (title + url + preview) ──────────────────────────────────
  const body = document.createElement('div');
  body.className = 'ws-card-body';

  // Title (clickable → opens modal)
  const titleEl = document.createElement('div');
  titleEl.className = 'ws-card-title';
  titleEl.style.whiteSpace = 'normal';
  const titleLink = document.createElement('a');
  titleLink.href = '#';
  titleLink.textContent = page.title || page.url;
  titleLink.addEventListener('click', (e) => {
    e.preventDefault();
    openPageModal(page);
  });
  titleEl.appendChild(titleLink);
  body.appendChild(titleEl);

  // URL (opens actual page in new tab)
  const urlEl = document.createElement('div');
  urlEl.className = 'ws-card-url';
  const urlLink = document.createElement('a');
  urlLink.href = page.url;
  urlLink.target = '_blank';
  urlLink.rel = 'noopener';
  urlLink.textContent = page.url;
  urlEl.appendChild(urlLink);
  body.appendChild(urlEl);

  // Preview text
  const previewEl = document.createElement('div');
  previewEl.className = 'ws-card-preview';
  previewEl.textContent = page.preview_text || '';
  body.appendChild(previewEl);

  card.appendChild(body);

  // ── Star rating ───────────────────────────────────────────────────
  const ratingWrap = document.createElement('div');
  ratingWrap.className = 'ws-card-rating';

  const hasUserRating = page.user_rating !== null && page.user_rating !== undefined;
  const displayRating = hasUserRating ? page.user_rating : page.model_rating;

  const starRating = new StarRatingComponent({
    initialRating: displayRating,
    callback: (rating) => {
      socket.emit('emit_WebSearch_set_rating', {
        page_id: page.id,
        rating: rating,
      }, (resp) => {
        if (resp && !resp.error) {
          page.user_rating = rating;
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

  for (let i = 1; i <= totalPages; i++) {
    if (i === 1 || i === totalPages || Math.abs(i - page) <= 2) {
      const li = document.createElement('li');
      const a = document.createElement('a');
      a.className = 'pagination-link' + (i === page ? ' is-current' : '');
      a.textContent = i;
      a.href = '#';
      a.addEventListener('click', (e) => {
        e.preventDefault();
        currentPage = i;
        fetchPages();
      });
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

function fetchPages() {
  const payload = {
    page: currentPage,
    limit: PAGE_LIMIT,
    order: currentOrder,
  };
  if (currentDomain) payload.domain = currentDomain;
  if (currentPath) payload.path = currentPath;

  socket.emit('emit_WebSearch_get_pages', payload, (response) => {
    const container = document.getElementById('ws_pages_container');
    container.innerHTML = '';

    if (!response || !response.pages) return;

    response.pages.forEach((page) => {
      container.appendChild(buildCard(page));
    });

    renderPagination(response.page, response.total, response.limit);
    document.querySelector('.search-status').textContent =
      `${response.total} page${response.total !== 1 ? 's' : ''}`;
  });
}

function fetchSites() {
  socket.emit('emit_WebSearch_get_sites', {}, (sites) => {
    const list = document.getElementById('ws_sites_list');

    // Keep the "All Sites" entry
    list.innerHTML = '<li><a class="ws-site-item is-active" data-site-id="">All Sites</a></li>';

    if (!sites) return;

    sites.forEach((site) => {
      const li = document.createElement('li');
      const a = document.createElement('a');
      a.className = 'ws-site-item';
      a.dataset.domain = site.domain;
      const status = site.crawl_status === 'crawling' ? ' ⧗' : '';
      a.innerHTML = `${site.domain}${status} <span class="ws-site-count">[${site.pages}]</span>`;
      // Right-click context menu
      a.addEventListener('contextmenu', (e) => {
        e.preventDefault();
        _contextMenu.show(e.pageX, e.pageY, [
          {
            label: '&#x21BB; Recrawl site',
            action: () => openRecrawlModal(site.domain),
          },
        ]);
      });
      li.appendChild(a);
      list.appendChild(li);
    });

    // Re-bind click handlers
    bindSiteClicks();
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

    // Toggle: if clicking the already-active folder, go back to site root
    currentPath = (currentPath === clickedPath) ? '' : clickedPath;
    currentPage = 1;
    fetchPages();
    fetchFolders();  // re-render tree with new active path
  });
}

function fetchFolders() {
  const treeContainer = document.getElementById('ws_folder_tree');
  if (!currentDomain) {
    treeContainer.innerHTML = '';
    return;
  }

  const payload = { domain: currentDomain };

  socket.emit('emit_WebSearch_get_folders', payload, (foldersDict) => {
    treeContainer.innerHTML = '';

    if (!foldersDict || foldersDict.total_files === 0) return;

    const folderView = new FolderViewComponent(foldersDict, currentPath);
    treeContainer.appendChild(folderView.getDOMElement());
    _bindFolderTreeClicks();
  });
}

function bindSiteClicks() {
  document.querySelectorAll('.ws-site-item').forEach((el) => {
    el.addEventListener('click', (e) => {
      e.preventDefault();
      document.querySelectorAll('.ws-site-item').forEach(s => s.classList.remove('is-active'));
      el.classList.add('is-active');
      currentDomain = el.dataset.domain || null;
      currentPath = '';  // reset folder path when switching sites
      currentPage = 1;
      fetchPages();
      fetchFolders();
    });
  });
}

// ── Modal ────────────────────────────────────────────────────────────────

function openPageModal(page) {
  const modal = document.getElementById('ws_page_modal');
  document.getElementById('ws_modal_title').textContent = page.title || page.url;
  document.getElementById('ws_modal_link').href = page.url;
  document.getElementById('ws_modal_body').innerHTML = 'Loading…';

  // Star rating in modal
  const ratingContainer = document.getElementById('ws_modal_rating');
  ratingContainer.innerHTML = '';

  const hasUserRating = page.user_rating !== null && page.user_rating !== undefined;
  const displayRating = hasUserRating ? page.user_rating : page.model_rating;

  const modalStarRating = new StarRatingComponent({
    initialRating: displayRating,
    callback: (rating) => {
      socket.emit('emit_WebSearch_set_rating', {
        page_id: page.id,
        rating: rating,
      }, (resp) => {
        if (resp && !resp.error) {
          page.user_rating = rating;
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
  socket.emit('emit_WebSearch_get_page_content', { page_id: page.id }, (resp) => {
    if (resp && resp.content) {
      marked.setOptions({ breaks: true, gfm: true });
      const html = DOMPurify.sanitize(marked.parse(resp.content));
      document.getElementById('ws_modal_body').innerHTML = html;
    } else {
      document.getElementById('ws_modal_body').textContent = 'Could not load content.';
    }
  });
}

// ── Recrawl modal ───────────────────────────────────────────────────────────────────

function openRecrawlModal(domain) {
  document.getElementById('ws_recrawl_url').value = `https://${domain}`;
  document.getElementById('ws_recrawl_sublinks_cb').checked = false;
  document.getElementById('ws_recrawl_delay_input').value = '0.5';
  document.getElementById('ws_recrawl_max_pages_input').value = '5000';
  document.getElementById('ws_recrawl_modal').classList.add('is-active');
  setTimeout(() => document.getElementById('ws_recrawl_url').focus(), 50);
}

function closeRecrawlModal() {
  document.getElementById('ws_recrawl_modal').classList.remove('is-active');
}

function confirmRecrawlModal() {
  const url = document.getElementById('ws_recrawl_url').value.trim();
  if (!url) {
    document.getElementById('ws_recrawl_url').focus();
    return;
  }
  const sublinksOnly = document.getElementById('ws_recrawl_sublinks_cb').checked;
  const crawlDelay = parseFloat(document.getElementById('ws_recrawl_delay_input').value) || 0.5;
  const maxPages = parseInt(document.getElementById('ws_recrawl_max_pages_input').value, 10) || 5000;
  closeRecrawlModal();
  socket.emit('emit_WebSearch_recrawl_site', {
    url,
    sublinks_only: sublinksOnly,
    crawl_delay: crawlDelay,
    max_pages: maxPages,
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
    // size: 26,
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
  fetchSites();
  fetchPages();

  // ── Recrawl modal controls ────────────────────────────────────────────
  document.querySelectorAll('.ws-recrawl-modal-close').forEach((el) => {
    el.addEventListener('click', closeRecrawlModal);
  });
  document.getElementById('ws_recrawl_modal_confirm').addEventListener('click', confirmRecrawlModal);
  document.getElementById('ws_recrawl_modal_cancel').addEventListener('click', closeRecrawlModal);
  document.getElementById('ws_recrawl_url').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') confirmRecrawlModal();
  });

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

  // ── Order buttons ─────────────────────────────────────────────────
  document.querySelectorAll('.ws-order-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.ws-order-btn').forEach(b => b.classList.remove('is-link'));
      btn.classList.add('is-link');
      currentOrder = btn.dataset.order;
      currentPage = 1;
      fetchPages();
    });
  });

  // ── Close modal ───────────────────────────────────────────────────
  document.querySelectorAll('.ws-modal-close').forEach((el) => {
    el.addEventListener('click', () => {
      document.getElementById('ws_page_modal').classList.remove('is-active');
    });
  });

  // ── Live events from server ───────────────────────────────────────
  socket.on('emit_WebSearch_page_added', (_page) => {
    fetchSites();
    fetchFolders();
    fetchPages();
  });

  socket.on('emit_WebSearch_crawl_progress', (data) => {
    document.getElementById('ws_crawl_status').textContent = data.message || '';
  });

  socket.on('emit_WebSearch_show_sites', (_sites) => {
    fetchSites();
    fetchFolders();
    fetchPages();
  });

  socket.on('emit_show_search_status', (status) => {
    document.querySelector('.search-status').textContent = status;
  });
});
