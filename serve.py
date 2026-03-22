"""
Web Search module – serve.py
Provides socket events for adding pages, browsing/searching crawled pages,
rating them, and triggering crawls.
"""

import os
import datetime
import threading
from urllib.parse import urlparse

import torch
from flask import Flask
from flask_socketio import SocketIO

from omegaconf import OmegaConf

import numpy as np

import modules.WebSearch.db_models as db_models
from modules.WebSearch.crawler import SiteCrawler
from src.socket_events import CommonSocketEvents
from src.text_embedder import TextEmbedder
from modules.train.universal_train import UniversalEvaluator
import rapidfuzz.fuzz
from src.utils import weighted_shuffle
from src.caching import TwoLevelCache
from src.common_filters import CommonFilters, _normalize_text
from src.recommendation_engine import sort_files_by_recommendation

# ── Event catalogue ──────────────────────────────────────────────────────
#
# Incoming (client → server):
#   emit_WebSearch_add_page          {url}
#   emit_WebSearch_crawl_site        {url, max_pages?}
#   emit_WebSearch_get_sites
#   emit_WebSearch_get_pages         {page, limit, domain?, text_query?, order?}
#   emit_WebSearch_set_rating        {page_id, rating}
#   emit_WebSearch_get_page_content  {page_id}
#
# Outgoing (server → client):
#   emit_WebSearch_show_sites        [{domain, pages, last_crawl_date}, …]
#   emit_WebSearch_show_pages        {pages: [], total: int, page: int}
#   emit_WebSearch_page_added        {page dict}
#   emit_WebSearch_crawl_progress    {message}
#   emit_WebSearch_show_page_content {page_id, content}
#   emit_show_search_status           (via CommonSocketEvents)
# ─────────────────────────────────────────────────────────────────────────


# ── _WebSearchTextEngine ─────────────────────────────────────────────────
# A minimal engine adapter that gives CommonFilters the interface it needs
# to operate on WebSearch .md files — exactly the same way TextSearch does
# for the text module, just pointing at a different folder.
#
# Hash strategy: re-uses the blake2b hash the crawler already stored in
# WebPage.hash so that CommonFilters.filter_by_rating can resolve ratings
# via the existing WebPage table without any schema changes.  Hashes are
# seeded per request from the already-fetched page list (no extra I/O).

class _WebSearchTextEngine:
    def __init__(self, text_embedder, page_emb_cache, storage_dir):
        self._emb           = text_embedder
        self._cache         = page_emb_cache
        self.storage_dir    = storage_dir
        self._path_hash     = {}   # abs_path → WebPage.hash, refreshed per request
        self._emb_dim_cache = None

    def seed_hashes(self, pages):
        """Prime the path→hash map from an already-fetched WebPage list."""
        self._path_hash = {
            os.path.join(self.storage_dir, p.md_file_path): p.hash
            for p in pages if p.md_file_path and p.hash
        }

    def seed_titles(self, path_to_page):
        """Prime the path→(title, url) map so fuzzy search can match human-readable text."""
        self._path_title = {
            path: (page.title or '', page.url or '')
            for path, page in path_to_page.items()
        }

    def get_title_and_url(self, path):
        """Return (title, url) for a path, falling back to basename."""
        return getattr(self, '_path_title', {}).get(path, (os.path.basename(path), ''))

    def get_file_hash(self, path: str) -> str:
        return self._path_hash.get(path, '')

    def get_hash_algorithm(self) -> str:
        return 'blake2b:v1'

    def process_text(self, text: str):
        return np.array(self._emb.embed_text(text))

    def _emb_dim(self) -> int:
        if self._emb_dim_cache is None:
            try:   self._emb_dim_cache = self._emb.embedding_dim or 1024
            except Exception: self._emb_dim_cache = 1024
        return self._emb_dim_cache

    def process_files(self, file_paths, callback=None, media_folder=None):
        rows = []
        for path in file_paths:
            cache_key = f'emb:{self._path_hash.get(path, path)}'
            cached = self._cache.get(cache_key)
            if cached is not None:
                rows.append(cached)
                continue
            try:
                with open(path, 'r', encoding='utf-8') as fh:
                    content = fh.read()
                emb = self._emb.embed_text(content)
                vec = np.array(emb).mean(axis=0) if emb is not None and len(emb) else np.zeros(self._emb_dim())
            except Exception:
                vec = np.zeros(self._emb_dim())
            self._cache.set(cache_key, vec)
            rows.append(vec)
        return torch.tensor(np.stack(rows), dtype=torch.float32) if rows else torch.zeros((0, self._emb_dim()))

    def compare(self, embeds_files, embeds_text):
        ef = embeds_files.numpy() if isinstance(embeds_files, torch.Tensor) else np.array(embeds_files)
        qt = np.array(embeds_text)
        if qt.ndim > 1:
            qt = qt.mean(axis=0)
        norms = np.linalg.norm(ef, axis=1) * np.linalg.norm(qt)
        return np.dot(ef, qt) / np.maximum(norms, 1e-8)


def init_socket_events(socketio: SocketIO, app: Flask = None, cfg=None, data_folder='./project_data'):
    common_socket_events = CommonSocketEvents(socketio, module_name="WebSearch")

    # ── Storage directory ────────────────────────────────────────────────
    storage_dir = OmegaConf.select(cfg, "WebSearch.storage_directory",
                                   default="/mnt/project_config/modules/WebSearch")
    os.makedirs(storage_dir, exist_ok=True)

    # ── Page embedding cache (for semantic search) ───────────────────────
    page_emb_cache = TwoLevelCache(
        cache_dir=os.path.join(cfg.main.cache_path, 'WebSearch'),
        name='page_embeddings',
    )

    # ── Crawler settings ─────────────────────────────────────────────────
    crawl_delay = OmegaConf.select(cfg, "WebSearch.crawl_delay", default=1.0)
    max_pages_per_site = OmegaConf.select(cfg, "WebSearch.max_pages_per_site", default=50)

    # ── Text embedder (for scoring) ─────────────────────────────────────
    common_socket_events.show_loading_status('Initializing text embedder for WebSearch…')
    text_embedder = TextEmbedder(cfg=cfg)
    text_embedder.initiate(models_folder=cfg.main.embedding_models_path)

    ws_engine = _WebSearchTextEngine(text_embedder, page_emb_cache, storage_dir)

    def _update_model_ratings(file_paths):
        """Bridge: CommonFilters passes abs .md paths; we score the matching WebPages."""
        for abs_path in file_paths:
            rel = os.path.relpath(abs_path, storage_dir)
            page = db_models.WebPage.query.filter_by(md_file_path=rel).first()
            if page:
                _score_and_update(page.id)

    ws_filters = CommonFilters(
        engine=ws_engine,
        metadata_engine=None,
        common_socket_events=common_socket_events,
        media_directory=storage_dir,
        db_schema=db_models.WebPage,
        update_model_ratings_func=_update_model_ratings,
    )

    # ── Universal evaluator ──────────────────────────────────────────────
    common_socket_events.show_loading_status('Loading universal evaluator for WebSearch…')
    evaluator = UniversalEvaluator()
    evaluator_path = os.path.join(cfg.main.personal_models_path, 'universal_evaluator.pt')
    if os.path.exists(evaluator_path):
        evaluator.load(evaluator_path)
    else:
        print("[WebSearch] universal_evaluator.pt not found – model scoring disabled.")

    # ── Crawler instance ─────────────────────────────────────────────────
    def _crawl_status(msg):
        socketio.emit('emit_WebSearch_crawl_progress', {'message': msg})
        common_socket_events.show_search_status(msg)

    crawler = SiteCrawler(
        storage_dir=storage_dir,
        crawl_delay=crawl_delay,
        max_pages=max_pages_per_site,
        status_callback=_crawl_status,
    )

    # ── In-memory crawl status (session-only, resets on restart) ─────────
    # domain → 'idle' | 'crawling'
    _crawl_status_map = {}

    # ── Scoring state ────────────────────────────────────────────────────
    # Tracks the evaluator hash used for the last bulk-score run and whether
    # a background scoring thread is currently active.  Both are accessed
    # only from the main thread or under the GIL so no explicit lock needed.
    _scoring_state = {'last_hash': None, 'in_progress': False}

    # ── Helpers ──────────────────────────────────────────────────────────

    def _score_page(md_file_path: str):
        """Return a model rating for a single .md file (or None)."""
        full_path = os.path.join(storage_dir, md_file_path)
        if not os.path.exists(full_path):
            return None
        try:
            with open(full_path, 'r', encoding='utf-8') as f:
                content = f.read()
            if not content or len(content.strip()) < 10:
                return None
            chunk_embeddings = text_embedder.embed_text(content)  # np.ndarray [chunks, dim]
            # evaluator.predict expects list-of-lists: [[np.ndarray, …], …]
            ratings = evaluator.predict([chunk_embeddings])
            return float(ratings[0])
        except Exception as exc:
            print(f"[WebSearch] scoring error for {md_file_path}: {exc}")
            return None

    def _score_and_update(page_id: int):
        """Score a page and persist the result."""
        with app.app_context():
            page = db_models.WebPage.query.get(page_id)
            if page is None or page.md_file_path is None:
                return
            # Skip if already scored with current evaluator
            if page.model_rating is not None and page.model_hash == evaluator.hash:
                return
            rating = _score_page(page.md_file_path)
            if rating is not None:
                page.model_rating = rating
                page.model_hash = evaluator.hash
                db_models.db.session.commit()

    def _bulk_score_unscored():
        """
        Score all pages that lack a model rating or have a stale model_hash.
        Runs in a background thread — never blocks module startup or page requests.
        Shows progress in the status bar while the module is already operational.
        """
        if _scoring_state['in_progress']:
            return  # another thread is already running
        _scoring_state['in_progress'] = True
        current_hash = evaluator.hash
        try:
            with app.app_context():
                pages = db_models.WebPage.query.filter(
                    (db_models.WebPage.model_rating.is_(None)) |
                    (db_models.WebPage.model_hash != current_hash)
                ).all()
                total = len(pages)
                if total == 0:
                    _scoring_state['last_hash'] = current_hash
                    return
                print(f"[WebSearch] Re-scoring {total} pages with evaluator {current_hash}…")
                for i, page in enumerate(pages):
                    if page.md_file_path is None:
                        continue
                    rating = _score_page(page.md_file_path)
                    if rating is not None:
                        page.model_rating = rating
                        page.model_hash = current_hash
                    common_socket_events.show_search_status(
                        f"[WebSearch] Scoring pages… {i + 1}/{total}"
                    )
                db_models.db.session.commit()
                print(f"[WebSearch] Scoring complete ({total} pages).")
                _scoring_state['last_hash'] = current_hash
        finally:
            _scoring_state['in_progress'] = False

    def _maybe_trigger_rescore():
        """
        Called from get_pages.  If the evaluator hash changed since the last
        bulk-score run and no scoring thread is already active, start one.
        """
        if _scoring_state['in_progress']:
            return
        if evaluator.hash is None:
            return
        if _scoring_state['last_hash'] != evaluator.hash:
            thread = threading.Thread(target=_bulk_score_unscored, daemon=True)
            thread.start()

    # ── Socket handlers ──────────────────────────────────────────────────

    @socketio.on('emit_WebSearch_add_page')
    def handle_add_page(data):
        """Add (and crawl) a single page by URL."""
        url = data.get('url', '').strip()
        if not url:
            return {'error': 'No URL provided'}
        user_rating = data.get('user_rating', None)

        def _do_add():
            with app.app_context():
                page_info = crawler.crawl_single_page(url, app=app)
                if page_info:
                    # Score the newly added page
                    _score_and_update(page_info['id'])
                    # Apply optional user rating supplied at add time
                    if user_rating is not None:
                        page = db_models.WebPage.query.get(page_info['id'])
                        if page:
                            page.user_rating = float(user_rating)
                            page.user_rating_date = datetime.datetime.utcnow()
                            db_models.db.session.commit()
                    # Refetch from DB so we have model_rating (and any user_rating just set)
                    page = db_models.WebPage.query.get(page_info['id'])
                    socketio.emit('emit_WebSearch_page_added', page.as_dict() if page else page_info)

        thread = threading.Thread(target=_do_add, daemon=True)
        thread.start()

    @socketio.on('emit_WebSearch_crawl_site')
    def handle_crawl_site(data):
        """BFS-crawl a site starting from a seed URL."""
        url = data.get('url', '').strip()
        if not url:
            return {'error': 'No URL provided'}
        max_pages = data.get('max_pages', max_pages_per_site)
        custom_delay = data.get('crawl_delay', None)
        sublinks_only = bool(data.get('sublinks_only', False))
        seed_user_rating = data.get('seed_user_rating', None)
        domain = urlparse(url).netloc

        def _do_crawl():
            _crawl_status_map[domain] = 'crawling'
            # Create a per-request crawler so custom delay/max_pages
            # don’t clobber any concurrent shared state.
            site_crawler = SiteCrawler(
                storage_dir=storage_dir,
                crawl_delay=float(custom_delay) if custom_delay is not None else crawl_delay,
                max_pages=max_pages,
                status_callback=_crawl_status,
            )
            with app.app_context():
                results = site_crawler.crawl_site(url, app=app, sublinks_only=sublinks_only)
                # Apply optional user rating to the seed URL page
                if seed_user_rating is not None:
                    # Normalise: strip trailing slash like the crawler does
                    parsed_seed = urlparse(url)
                    normalised_seed = (
                        f"{parsed_seed.scheme}://{parsed_seed.netloc}"
                        f"{parsed_seed.path.rstrip('/') or '/'}"
                    )
                    seed_page = db_models.WebPage.query.filter(
                        db_models.WebPage.url.in_([url, normalised_seed])
                    ).first()
                    if seed_page:
                        seed_page.user_rating = float(seed_user_rating)
                        seed_page.user_rating_date = datetime.datetime.utcnow()
                        db_models.db.session.commit()
                # Batch-score all new pages
                for info in results:
                    _score_and_update(info['id'])
                _crawl_status(f"Scoring complete – {len(results)} pages crawled.")
            _crawl_status_map[domain] = 'idle'
            # Send updated sites list
            socketio.emit('emit_WebSearch_show_sites', _build_sites_list())

        thread = threading.Thread(target=_do_crawl, daemon=True)
        thread.start()

    @socketio.on('emit_WebSearch_recrawl_site')
    def handle_recrawl_site(data):
        """
        Incrementally recrawl a site.
        Pages whose raw HTTP content hash hasn't changed are skipped entirely
        (markitdown is never invoked), but their links are still followed so
        newly-linked pages on index/listing pages are discovered.
        """
        url = data.get('url', '').strip()
        if not url:
            return {'error': 'No URL provided'}
        max_pages = data.get('max_pages', max_pages_per_site)
        custom_delay = data.get('crawl_delay', None)
        sublinks_only = bool(data.get('sublinks_only', False))
        domain = urlparse(url).netloc

        def _do_recrawl():
            _crawl_status_map[domain] = 'crawling'
            site_crawler = SiteCrawler(
                storage_dir=storage_dir,
                crawl_delay=float(custom_delay) if custom_delay is not None else crawl_delay,
                max_pages=max_pages,
                status_callback=_crawl_status,
            )
            with app.app_context():
                changed = site_crawler.crawl_site(
                    url, app=app, sublinks_only=sublinks_only, recrawl=True
                )
                # Only score changed / new pages
                for info in changed:
                    _score_and_update(info['id'])
                _crawl_status(
                    f"Recrawl complete – {len(changed)} pages updated."
                )
            _crawl_status_map[domain] = 'idle'
            socketio.emit('emit_WebSearch_show_sites', _build_sites_list())

        thread = threading.Thread(target=_do_recrawl, daemon=True)
        thread.start()

    def _build_sites_list():
        """
        Compute per-domain stats live from WebPage.
        Returns a list of dicts sorted by most-recently-crawled first.
        """
        rows = (
            db_models.db.session
            .query(
                db_models.WebPage.domain,
                db_models.db.func.count(db_models.WebPage.id).label('pages'),
                db_models.db.func.max(db_models.WebPage.last_crawl_date).label('last_crawl_date'),
            )
            .group_by(db_models.WebPage.domain)
            .order_by(db_models.db.func.max(db_models.WebPage.last_crawl_date).desc())
            .all()
        )
        result = []
        for row in rows:
            result.append({
                'domain': row.domain,
                'pages': row.pages,
                'last_crawl_date': row.last_crawl_date.isoformat() if row.last_crawl_date else None,
                'crawl_status': _crawl_status_map.get(row.domain, 'idle'),
            })
        return result

    @socketio.on('emit_WebSearch_get_sites')
    def handle_get_sites(data=None):
        return _build_sites_list()

    @socketio.on('emit_WebSearch_get_folders')
    def handle_get_folders(data=None):
        """
        Build a folder tree dict from md_file_path values.
        Returns the same structure FolderViewComponent expects:
        {name, num_files, total_files, subfolders: {key: {...}, ...}}

        Only pages that have at least one rating are counted.
        """
        query = db_models.WebPage.query.filter(
            (db_models.WebPage.user_rating.isnot(None)) |
            (db_models.WebPage.model_rating.isnot(None))
        )
        if data and data.get('domain'):
            query = query.filter_by(domain=data['domain'])

        pages = query.with_entities(db_models.WebPage.md_file_path).all()
        paths = [p.md_file_path for p in pages if p.md_file_path]

        # Build tree
        root = {'name': 'All', 'num_files': 0, 'total_files': 0, 'subfolders': {}}

        for md_path in paths:
            parts = md_path.split('/')
            # Last part is the filename; everything before is the folder hierarchy
            folders = parts[:-1]
            node = root
            for folder in folders:
                if folder not in node['subfolders']:
                    node['subfolders'][folder] = {
                        'name': folder,
                        'num_files': 0,
                        'total_files': 0,
                        'subfolders': {},
                    }
                node = node['subfolders'][folder]
                node['total_files'] += 1
            # Leaf folder gets the direct file count
            node['num_files'] += 1
            root['total_files'] += 1

        # root.num_files = files directly in root (paths with no folder)
        root['num_files'] = sum(1 for p in paths if '/' not in p)

        return root

    @socketio.on('emit_WebSearch_get_pages')
    def handle_get_pages(data):
        """Return a paginated, scored list of rated pages."""
        page_n      = max(1, data.get('page', 1))
        limit       = min(data.get('limit', 20), 100)
        text_query  = (data.get('text_query', '') or '').strip()
        mode        = data.get('mode', 'file-name')
        order       = data.get('order', 'most-relevant')
        temperature = float(data.get('temperature', 0))
        seed        = data.get('seed', None)

        if seed is not None:
            try: np.random.seed(int(seed) % (2 ** 32))
            except (ValueError, OverflowError): pass

        # Build candidate set (rated pages, scoped to domain / folder path)
        base_q = db_models.WebPage.query.filter(
            (db_models.WebPage.user_rating.isnot(None)) |
            (db_models.WebPage.model_rating.isnot(None))
        )
        if data.get('domain'): base_q = base_q.filter_by(domain=data['domain'])
        if data.get('path'):   base_q = base_q.filter(db_models.WebPage.md_file_path.like(f"{data['path']}%"))
        all_pages = base_q.all()
        if not all_pages:
            return {'pages': [], 'total': 0, 'page': page_n, 'limit': limit}

        # Map absolute .md path → WebPage (mirrors media_directory → file in other modules)
        all_files    = [os.path.join(storage_dir, p.md_file_path) for p in all_pages if p.md_file_path]
        path_to_page = {os.path.join(storage_dir, p.md_file_path): p for p in all_pages if p.md_file_path}

        # Align engine hashes so CommonFilters.filter_by_rating resolves correctly
        ws_engine.seed_hashes(all_pages)
        ws_engine.seed_titles(path_to_page)

        # WebSearch-specific extra filters (recommendation, recency)
        def _filter_recommendation(files, *_, **__):
            files_data = [
                {'user_rating': path_to_page[f].user_rating, 'model_rating': path_to_page[f].model_rating,
                 'full_play_count': 1, 'skip_count': 0, 'last_played': path_to_page[f].last_crawl_date}
                for f in files if f in path_to_page
            ]
            return np.array(sort_files_by_recommendation(files, files_data), dtype=np.float32)

        def _filter_recent(files, *_, **__):
            ts = np.array([path_to_page[f].crawl_date.timestamp()
                           if f in path_to_page and path_to_page[f].crawl_date else 0.0
                           for f in files], dtype=np.float32)
            rng = ts.max() - ts.min()
            return (ts - ts.min()) / (rng + 1e-8)

        # Fuzzy file-name filter: match against page title + URL instead of
        # the on-disk .md filename (which is a meaningless blake2b hash).
        def _filter_fuzzy_title(files, text_query, **__):  # noqa: E306
            q = _normalize_text(text_query)
            q_raw = text_query.strip().lower()
            scorer = rapidfuzz.fuzz.token_set_ratio if ' ' in q else rapidfuzz.fuzz.WRatio
            scores = []
            for f in files:
                title, url = ws_engine.get_title_and_url(f)
                s_title = scorer(q, _normalize_text(title))
                s_url   = scorer(q, _normalize_text(url))
                combined = max(1.3 * s_title, s_url)
                # exact-match boost (same priority logic as CommonFilters)
                priority = 0
                if q_raw and q_raw in title.lower():
                    priority = 3
                elif q_raw and q_raw in url.lower():
                    priority = 2
                scores.append(priority * 10.0 + combined)
            return np.array(scores, dtype=np.float32) / 100.0

        def _filter_by_text(files, text_query, mode='file-name', **kw):
            if mode == 'file-name':
                return _filter_fuzzy_title(files, text_query)
            return ws_filters.filter_by_text(files, text_query, mode=mode, **kw)

        # Filter dispatch table – same pattern as text module
        filters = {
            'by_text':        _filter_by_text,
            'rating':         ws_filters.filter_by_rating,
            'recommendation': _filter_recommendation,
            'recent':         _filter_recent,
        }

        # Route to appropriate filter (mirrors FileManager.get_files logic)
        query       = text_query or 'rating'
        filter_name = query.split()[0]
        if filter_name in filters and filter_name != 'by_text':
            scores = filters[filter_name](all_files, query)
        else:
            scores = filters['by_text'](all_files, query, mode=mode)

        indices = weighted_shuffle(scores, temperature=temperature)
        if order == 'least-relevant':
            indices = list(reversed(indices))
        sorted_files = [all_files[i] for i in indices]
        offset = (page_n - 1) * limit

        _maybe_trigger_rescore()

        return {
            'pages': [path_to_page[f].as_dict() for f in sorted_files[offset:offset + limit] if f in path_to_page],
            'total': len(sorted_files),
            'page':  page_n,
            'limit': limit,
        }

    @socketio.on('emit_WebSearch_set_rating')
    def handle_set_rating(data):
        """Set a user rating for a page."""
        page_id = data.get('page_id')
        rating = data.get('rating')
        if page_id is None or rating is None:
            return {'error': 'page_id and rating required'}

        page = db_models.WebPage.query.get(page_id)
        if page is None:
            return {'error': 'Page not found'}

        page.user_rating = float(rating)
        page.user_rating_date = datetime.datetime.utcnow()
        db_models.db.session.commit()
        return page.as_dict()

    @socketio.on('emit_WebSearch_get_page_content')
    def handle_get_page_content(data):
        """Return the markdown content of a stored page."""
        page_id = data.get('page_id')
        page = db_models.WebPage.query.get(page_id)
        if page is None or page.md_file_path is None:
            return {'error': 'Page not found'}

        full_path = os.path.join(storage_dir, page.md_file_path)
        content = ''
        if os.path.exists(full_path):
            with open(full_path, 'r', encoding='utf-8') as f:
                content = f.read()

        socketio.emit('emit_WebSearch_show_page_content', {
            'page_id': page_id,
            'content': content,
        })
        return {'page_id': page_id, 'content': content}

    def _restore_missing_md_files():
        """
        Background startup task: find every WebPage record whose .md file no
        longer exists on disk and silently re-crawl it to restore the file.

        Runs once at module startup in a daemon thread so it never blocks page
        requests.  No user action is ever required — the recovery is fully
        automatic.  Pages whose URLs are no longer reachable are skipped
        gracefully (the DB record is preserved with whatever metadata was
        already there).
        """
        with app.app_context():
            all_pages = db_models.WebPage.query.filter(
                db_models.WebPage.md_file_path.isnot(None)
            ).all()

            missing = [
                p for p in all_pages
                if not os.path.exists(os.path.join(storage_dir, p.md_file_path))
            ]

        if not missing:
            return

        print(f"[WebSearch] {len(missing)} .md file(s) missing — restoring in background…")
        for i, page in enumerate(missing):
            common_socket_events.show_search_status(
                f"[WebSearch] Restoring missing page {i + 1}/{len(missing)}: {page.url}"
            )
            try:
                crawler.crawl_single_page(page.url, app=app)
            except Exception as exc:
                print(f"[WebSearch] Could not restore {page.url}: {exc}")

        common_socket_events.show_search_status("")
        print(f"[WebSearch] Restoration complete — {len(missing)} page(s) processed.")

    # ── Startup background tasks (non-blocking) ──────────────────────────
    # Both threads are daemon threads; they die with the process and never
    # block module startup or page requests.
    # 1. Restore any .md files that were deleted from disk.
    threading.Thread(target=_restore_missing_md_files, daemon=True).start()
    # 2. Score pages that lack a model rating or have a stale model_hash.
    if os.path.exists(evaluator_path):
        threading.Thread(target=_bulk_score_unscored, daemon=True).start()

    common_socket_events.show_loading_status('Web Search module ready!')
