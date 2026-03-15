"""
Web Search module – serve.py
Provides socket events for adding pages, browsing/searching crawled pages,
rating them, and triggering crawls.
"""

import os
import datetime
import threading
from urllib.parse import urlparse

from flask import Flask
from flask_socketio import SocketIO

from omegaconf import OmegaConf

import numpy as np

import pages.web_search.db_models as db_models
from pages.web_search.crawler import SiteCrawler
from src.socket_events import CommonSocketEvents
from src.text_embedder import TextEmbedder
from pages.train.universal_train import UniversalEvaluator

# ── Event catalogue ──────────────────────────────────────────────────────
#
# Incoming (client → server):
#   emit_web_search_add_page          {url}
#   emit_web_search_crawl_site        {url, max_pages?}
#   emit_web_search_get_sites
#   emit_web_search_get_pages         {page, limit, domain?, text_query?, order?}
#   emit_web_search_set_rating        {page_id, rating}
#   emit_web_search_get_page_content  {page_id}
#
# Outgoing (server → client):
#   emit_web_search_show_sites        [{domain, pages, last_crawl_date}, …]
#   emit_web_search_show_pages        {pages: [], total: int, page: int}
#   emit_web_search_page_added        {page dict}
#   emit_web_search_crawl_progress    {message}
#   emit_web_search_show_page_content {page_id, content}
#   emit_show_search_status           (via CommonSocketEvents)
# ─────────────────────────────────────────────────────────────────────────


def init_socket_events(socketio: SocketIO, app: Flask = None, cfg=None, data_folder='./project_data'):
    common_socket_events = CommonSocketEvents(socketio, module_name="web_search")

    # ── Storage directory ────────────────────────────────────────────────
    storage_dir = OmegaConf.select(cfg, "web_search.storage_directory",
                                   default="/mnt/project_config/modules/web_search")
    os.makedirs(storage_dir, exist_ok=True)

    # ── Crawler settings ─────────────────────────────────────────────────
    crawl_delay = OmegaConf.select(cfg, "web_search.crawl_delay", default=1.0)
    max_pages_per_site = OmegaConf.select(cfg, "web_search.max_pages_per_site", default=50)

    # ── Text embedder (for scoring) ─────────────────────────────────────
    common_socket_events.show_loading_status('Initializing text embedder for web_search…')
    text_embedder = TextEmbedder(cfg=cfg)
    text_embedder.initiate(models_folder=cfg.main.embedding_models_path)

    # ── Universal evaluator ──────────────────────────────────────────────
    common_socket_events.show_loading_status('Loading universal evaluator for web_search…')
    evaluator = UniversalEvaluator()
    evaluator_path = os.path.join(cfg.main.personal_models_path, 'universal_evaluator.pt')
    if os.path.exists(evaluator_path):
        evaluator.load(evaluator_path)
    else:
        print("[web_search] universal_evaluator.pt not found – model scoring disabled.")

    # ── Crawler instance ─────────────────────────────────────────────────
    def _crawl_status(msg):
        socketio.emit('emit_web_search_crawl_progress', {'message': msg})
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
            print(f"[web_search] scoring error for {md_file_path}: {exc}")
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
                print(f"[web_search] Re-scoring {total} pages with evaluator {current_hash}…")
                for i, page in enumerate(pages):
                    if page.md_file_path is None:
                        continue
                    rating = _score_page(page.md_file_path)
                    if rating is not None:
                        page.model_rating = rating
                        page.model_hash = current_hash
                    common_socket_events.show_search_status(
                        f"[web_search] Scoring pages… {i + 1}/{total}"
                    )
                db_models.db.session.commit()
                print(f"[web_search] Scoring complete ({total} pages).")
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

    @socketio.on('emit_web_search_add_page')
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
                    socketio.emit('emit_web_search_page_added', page.as_dict() if page else page_info)

        thread = threading.Thread(target=_do_add, daemon=True)
        thread.start()

    @socketio.on('emit_web_search_crawl_site')
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
            socketio.emit('emit_web_search_show_sites', _build_sites_list())

        thread = threading.Thread(target=_do_crawl, daemon=True)
        thread.start()

    @socketio.on('emit_web_search_recrawl_site')
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
            socketio.emit('emit_web_search_show_sites', _build_sites_list())

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

    @socketio.on('emit_web_search_get_sites')
    def handle_get_sites(data=None):
        return _build_sites_list()

    @socketio.on('emit_web_search_get_folders')
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

    @socketio.on('emit_web_search_get_pages')
    def handle_get_pages(data):
        """
        Return paginated list of pages.
        Accepts: page (1-based), limit, domain (optional), path (optional),
                 order (optional).
        Pages without any rating (neither user nor model) are excluded.
        """
        page_num = max(1, data.get('page', 1))
        limit = min(data.get('limit', 20), 100)
        domain = data.get('domain', None)
        path = data.get('path', '')  # md_file_path prefix filter
        order = data.get('order', 'rating')  # rating | recent | alpha

        query = db_models.WebPage.query

        # Hide pages that have no rating at all
        query = query.filter(
            (db_models.WebPage.user_rating.isnot(None)) |
            (db_models.WebPage.model_rating.isnot(None))
        )

        if domain:
            query = query.filter_by(domain=domain)

        # Filter by md_file_path prefix (folder navigation)
        if path:
            query = query.filter(db_models.WebPage.md_file_path.like(f'{path}%'))

        # Ordering
        if order == 'recent':
            query = query.order_by(db_models.WebPage.crawl_date.desc())
        elif order == 'alpha':
            query = query.order_by(db_models.WebPage.title.asc())
        else:
            # Default: highest effective rating first (user > model)
            effective_rating = db_models.db.func.coalesce(
                db_models.WebPage.user_rating,
                db_models.WebPage.model_rating,
                0,
            )
            query = query.order_by(effective_rating.desc())

        total = query.count()
        pages = query.offset((page_num - 1) * limit).limit(limit).all()

        # Kick off background rescoring if the evaluator was retrained
        _maybe_trigger_rescore()

        return {
            'pages': [p.as_dict() for p in pages],
            'total': total,
            'page': page_num,
            'limit': limit,
        }

    @socketio.on('emit_web_search_set_rating')
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

    @socketio.on('emit_web_search_get_page_content')
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

        socketio.emit('emit_web_search_show_page_content', {
            'page_id': page_id,
            'content': content,
        })
        return {'page_id': page_id, 'content': content}

    # ── Startup scoring (non-blocking) ───────────────────────────────────
    # Launch in a background thread so the module becomes available immediately.
    # Progress is shown in the status bar via show_search_status.
    if os.path.exists(evaluator_path):
        thread = threading.Thread(target=_bulk_score_unscored, daemon=True)
        thread.start()

    common_socket_events.show_loading_status('Web Search module ready!')
