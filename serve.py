"""
Web Search module – serve.py
Provides socket events for adding pages, browsing/searching crawled pages,
rating them, and triggering crawls.

Architecture mirrors the text module: lean DB (hash + ratings + url),
all metadata in .md front-matter, FileManager for folder/file listing,
CommonFilters for search/sorting.
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
from modules.WebSearch.crawler import (
    SiteCrawler, HASH_ALGORITHM, parse_frontmatter,
    read_crawl_yaml, write_crawl_yaml, _blake2b,
)
from src.socket_events import CommonSocketEvents
from src.text_embedder import TextEmbedder
from modules.train.universal_train import UniversalEvaluator
import src.file_manager as file_manager
import rapidfuzz.fuzz
from src.utils import weighted_shuffle
from src.caching import TwoLevelCache
from src.common_filters import CommonFilters, _normalize_text
from src.recommendation_engine import sort_files_by_recommendation


# ── _WebSearchTextEngine ─────────────────────────────────────────────────
# Minimal engine adapter that gives CommonFilters / FileManager the
# interface they need.  Hash is computed from .md file bytes (matching
# the text module's BaseSearchEngine.get_file_hash pattern).

class _WebSearchTextEngine:
    def __init__(self, text_embedder, page_emb_cache, storage_dir):
        self._emb           = text_embedder
        self._cache         = page_emb_cache
        self.storage_dir    = storage_dir
        self._emb_dim_cache = None

    # ── FileManager / CommonFilters interface ────────────────────────

    def get_file_hash(self, path: str) -> str:
        """Compute blake2b hash from .md file bytes (same approach as BaseSearchEngine)."""
        if not os.path.exists(path):
            return ''
        st = os.stat(path, follow_symlinks=False)
        cache_key = f"HASH_OF_FILE::{path}::{st.st_size}::{st.st_mtime_ns}::{HASH_ALGORITHM}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        with open(path, 'rb') as f:
            file_hash = _blake2b(f.read())
        self._cache.set(cache_key, file_hash)
        return file_hash

    def get_hash_algorithm(self) -> str:
        return HASH_ALGORITHM

    def get_metadata(self, file_path: str) -> dict:
        """Return filesystem metadata (mirrors TextSearch._get_metadata)."""
        meta = {}
        try:
            meta['file_size'] = os.path.getsize(file_path)
            meta['creation_time'] = os.path.getctime(file_path)
            meta['modification_time'] = os.path.getmtime(file_path)
        except Exception:
            meta['file_size'] = None
            meta['creation_time'] = None
            meta['modification_time'] = None
        return meta

    def _get_media_folder(self) -> str:
        return self.storage_dir

    # ── Front-matter reading (cached) ────────────────────────────────

    def get_frontmatter(self, path: str) -> dict:
        """Read and cache the YAML front-matter from a .md file."""
        if not os.path.exists(path):
            return {}
        st = os.stat(path, follow_symlinks=False)
        cache_key = f"FRONTMATTER::{path}::{st.st_size}::{st.st_mtime_ns}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            with open(path, 'r', encoding='utf-8') as f:
                meta, _ = parse_frontmatter(f.read())
        except Exception:
            meta = {}
        self._cache.set(cache_key, meta)
        return meta

    def get_title_and_url(self, path: str) -> tuple[str, str]:
        """Return (title, url) from front-matter, falling back to basename."""
        meta = self.get_frontmatter(path)
        return (meta.get('title', os.path.basename(path)),
                meta.get('url', ''))

    # ── Embedding interface (for semantic search) ────────────────────

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
            file_hash = self.get_file_hash(path)
            cache_key = f'emb:{file_hash or path}'
            cached = self._cache.get(cache_key)
            if cached is not None:
                rows.append(cached)
                continue
            try:
                with open(path, 'r', encoding='utf-8') as fh:
                    _, body = parse_frontmatter(fh.read())
                emb = self._emb.embed_text(body)
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


# ── Module-level helpers ─────────────────────────────────────────────────

def _get_text_preview(file_path: str) -> str:
    """Return preview from front-matter, or first ~300 chars of body."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            meta, body = parse_frontmatter(f.read())
        preview = meta.get('preview', '')
        if preview:
            return preview
        text = body[:300].strip()
        if len(body) > 300:
            text += '…'
        return text
    except Exception:
        return ''


def init_socket_events(socketio: SocketIO, app: Flask = None, cfg=None, data_folder='./project_data'):
    common_socket_events = CommonSocketEvents(socketio, module_name="WebSearch")

    # ── Storage directory ────────────────────────────────────────────────
    storage_dir = OmegaConf.select(cfg, "WebSearch.storage_directory",
                                   default="/mnt/project_config/modules/WebSearch")
    os.makedirs(storage_dir, exist_ok=True)

    # ── Page embedding cache (for semantic search + front-matter + hashes)
    page_emb_cache = TwoLevelCache(
        cache_dir=os.path.join(cfg.main.cache_path, 'WebSearch'),
        name='page_embeddings',
    )

    # ── Crawler settings ─────────────────────────────────────────────────
    crawl_delay = OmegaConf.select(cfg, "WebSearch.crawl_delay", default=1.0)
    max_pages_per_site = OmegaConf.select(cfg, "WebSearch.max_pages_per_site", default=50)

    # ── Text embedder ────────────────────────────────────────────────────
    common_socket_events.show_loading_status('Initializing text embedder for WebSearch…')
    text_embedder = TextEmbedder(cfg=cfg)
    text_embedder.initiate(models_folder=cfg.main.embedding_models_path)

    ws_engine = _WebSearchTextEngine(text_embedder, page_emb_cache, storage_dir)

    # ── FileManager (same pattern as text module) ────────────────────────
    common_socket_events.show_loading_status('Setting up WebSearch file manager…')
    ws_file_manager = file_manager.FileManager(
        cfg=cfg,
        media_directory=storage_dir,
        engine=ws_engine,
        module_name="WebSearch",
        media_formats={'.md'},
        socketio=socketio,
        db_schema=db_models.WebPage,
    )

    def _update_model_ratings(file_paths):
        """Bridge: CommonFilters passes abs .md paths; we score the matching WebPages."""
        for abs_path in file_paths:
            file_hash = ws_engine.get_file_hash(abs_path)
            if not file_hash:
                continue
            page = db_models.WebPage.query.filter_by(hash=file_hash).first()
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
        # Only update the WebSearch page's own crawl-status label.
        # Background progress is shown in the Task Manager modal instead.
        socketio.emit('emit_WebSearch_crawl_progress', {'message': msg})

    crawler = SiteCrawler(
        storage_dir=storage_dir,
        crawl_delay=crawl_delay,
        max_pages=max_pages_per_site,
        status_callback=_crawl_status,
    )

    # ── Task manager reference ───────────────────────────────────────────
    task_manager = app.task_manager

    # ── Scoring state ────────────────────────────────────────────────────
    _scoring_state = {'last_hash': None, 'in_progress': False}

    # ── Helpers ──────────────────────────────────────────────────────────

    def _score_page(file_path: str):
        """Return a model rating for a single .md file (or None)."""
        full_path = os.path.join(storage_dir, file_path) if not os.path.isabs(file_path) else file_path
        if not os.path.exists(full_path):
            return None
        try:
            with open(full_path, 'r', encoding='utf-8') as f:
                _, body = parse_frontmatter(f.read())
            if not body or len(body.strip()) < 10:
                return None
            chunk_embeddings = text_embedder.embed_text(body)
            ratings = evaluator.predict([chunk_embeddings])
            return float(ratings[0])
        except Exception as exc:
            print(f"[WebSearch] scoring error for {file_path}: {exc}")
            return None

    def _score_and_update(page_id: int):
        """Score a page and persist the result."""
        with app.app_context():
            page = db_models.WebPage.query.get(page_id)
            if page is None or page.file_path is None:
                return
            if page.model_rating is not None and page.model_hash == evaluator.hash:
                return
            rating = _score_page(page.file_path)
            if rating is not None:
                page.model_rating = rating
                page.model_hash = evaluator.hash
                db_models.db.session.commit()

    def _task_score_pages(ctx):
        """Task: score all unscored / stale pages in the DB."""
        current_hash = evaluator.hash
        try:
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
                ctx.check()
                ctx.update((i + 1) / total, f"Scoring page {i + 1}/{total}…")
                if page.file_path is None:
                    continue
                rating = _score_page(page.file_path)
                if rating is not None:
                    page.model_rating = rating
                    page.model_hash = current_hash
            db_models.db.session.commit()
            print(f"[WebSearch] Scoring complete ({total} pages).")
            _scoring_state['last_hash'] = current_hash
        finally:
            _scoring_state['in_progress'] = False

    def _maybe_trigger_rescore():
        if _scoring_state['in_progress']:
            return
        if evaluator.hash is None:
            return
        if _scoring_state['last_hash'] != evaluator.hash:
            _scoring_state['in_progress'] = True
            task_manager.submit('WebSearch: score unscored pages', _task_score_pages)

    def _touch_last_viewed(page):
        """Update last_viewed timestamp on a WebPage."""
        page.last_viewed = datetime.datetime.utcnow()
        db_models.db.session.commit()

    # ── Socket handlers ──────────────────────────────────────────────────

    @socketio.on('emit_WebSearch_add_page')
    def handle_add_page(data):
        """Add (and crawl) a single page by URL."""
        url = data.get('url', '').strip()
        if not url:
            return {'error': 'No URL provided'}
        user_rating = data.get('user_rating', None)

        def _task_add(ctx):
            ctx.update(0.1, f'Crawling {url}…')
            ctx.check()
            page_info = crawler.crawl_single_page(url, app=app)
            if page_info:
                ctx.check()
                ctx.update(0.5, 'Scoring page…')
                _score_and_update(page_info['id'])
                if user_rating is not None:
                    page = db_models.WebPage.query.get(page_info['id'])
                    if page:
                        page.user_rating = float(user_rating)
                        page.user_rating_date = datetime.datetime.utcnow()
                        db_models.db.session.commit()
                page = db_models.WebPage.query.get(page_info['id'])
                socketio.emit('emit_WebSearch_page_added', page.as_dict() if page else page_info)
            ctx.update(1.0, 'Done')

        task_manager.submit(f'Add page: {url}', _task_add)

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

        def _task_crawl(ctx):
            pages_done = [0]

            def _ctx_status(msg):
                pages_done[0] += 1
                ctx.check()
                ctx.update(min(0.9, pages_done[0] / max(max_pages, 1)), msg)
                _crawl_status(msg)

            site_crawler = SiteCrawler(
                storage_dir=storage_dir,
                crawl_delay=float(custom_delay) if custom_delay is not None else crawl_delay,
                max_pages=max_pages,
                status_callback=_ctx_status,
            )
            results = site_crawler.crawl_site(url, app=app, sublinks_only=sublinks_only)
            if seed_user_rating is not None:
                seed_page = db_models.WebPage.query.filter_by(url=url).first()
                if seed_page:
                    seed_page.user_rating = float(seed_user_rating)
                    seed_page.user_rating_date = datetime.datetime.utcnow()
                    db_models.db.session.commit()
            for i, info in enumerate(results):
                ctx.check()
                ctx.update(0.9 + 0.1 * (i + 1) / max(len(results), 1),
                           f'Scoring page {i + 1}/{len(results)}…')
                _score_and_update(info['id'])
            ctx.update(1.0, f'Done – {len(results)} pages crawled')
            socketio.emit('emit_WebSearch_crawl_done', {})

        task_manager.submit(f'Crawl site: {url}', _task_crawl)

    @socketio.on('emit_WebSearch_recrawl_folder')
    def handle_recrawl_folder(data):
        """
        Recrawl based on .crawl.yaml in the given folder path.
        If no .crawl.yaml is found, uses the folder name as domain.
        """
        folder_path = data.get('path', '').strip()
        max_pages = data.get('max_pages', max_pages_per_site)
        custom_delay = data.get('crawl_delay', None)
        url_override = data.get('url', '').strip()

        abs_folder = (
            os.path.abspath(os.path.join(storage_dir, '..', folder_path))
            if folder_path else storage_dir
        )
        crawl_conf = read_crawl_yaml(abs_folder) or {}

        seed_url = url_override or crawl_conf.get('seed_url', '')
        if not seed_url:
            # Bulk mode: recrawl each immediate sub-folder that has a .crawl.yaml
            def _task_bulk_recrawl(ctx):
                total = 0
                try:
                    entries = sorted(os.scandir(abs_folder), key=lambda e: e.name)
                except OSError:
                    entries = []
                for entry in entries:
                    ctx.check()
                    if not entry.is_dir():
                        continue
                    sub_conf = read_crawl_yaml(entry.path)
                    if not sub_conf or not sub_conf.get('seed_url'):
                        continue
                    sub_seed = sub_conf['seed_url']
                    sub_sublinks = sub_conf.get('sublinks_only', False)
                    sub_delay = (
                        float(custom_delay) if custom_delay is not None
                        else sub_conf.get('crawl_delay', crawl_delay)
                    )
                    sub_max = max_pages or sub_conf.get('max_pages', max_pages_per_site)
                    ctx.update(0.0, f'Recrawling {sub_seed}…')
                    _crawl_status(f'Recrawling {sub_seed} …')

                    def _sub_status(msg):
                        ctx.check()
                        _crawl_status(msg)

                    site_crawler = SiteCrawler(
                        storage_dir=storage_dir,
                        crawl_delay=sub_delay,
                        max_pages=sub_max,
                        status_callback=_sub_status,
                    )
                    changed = site_crawler.crawl_site(
                        sub_seed, app=app, sublinks_only=sub_sublinks, recrawl=True
                    )
                    for info in changed:
                        ctx.check()
                        _score_and_update(info['id'])
                    total += len(changed)
                _crawl_status(f'Bulk recrawl complete – {total} pages updated.')
                socketio.emit('emit_WebSearch_crawl_done', {})

            task_manager.submit(f'Bulk recrawl: {folder_path or "/"}', _task_bulk_recrawl)
            return

        sublinks_only = data.get('sublinks_only', crawl_conf.get('sublinks_only', False))
        delay = float(custom_delay) if custom_delay is not None else crawl_conf.get('crawl_delay', crawl_delay)
        max_p = max_pages or crawl_conf.get('max_pages', max_pages_per_site)

        def _task_recrawl(ctx):
            pages_done = [0]

            def _ctx_status(msg):
                pages_done[0] += 1
                ctx.check()
                ctx.update(min(0.9, pages_done[0] / max(max_p, 1)), msg)
                _crawl_status(msg)

            site_crawler = SiteCrawler(
                storage_dir=storage_dir,
                crawl_delay=delay,
                max_pages=max_p,
                status_callback=_ctx_status,
            )
            changed = site_crawler.crawl_site(
                seed_url, app=app, sublinks_only=sublinks_only, recrawl=True
            )
            for i, info in enumerate(changed):
                ctx.check()
                ctx.update(0.9 + 0.1 * (i + 1) / max(len(changed), 1),
                           f'Scoring page {i + 1}/{len(changed)}…')
                _score_and_update(info['id'])
            ctx.update(1.0, f'Recrawl done – {len(changed)} pages updated')
            socketio.emit('emit_WebSearch_crawl_done', {})

        task_manager.submit(f'Recrawl: {seed_url}', _task_recrawl)

    @socketio.on('emit_WebSearch_read_crawl_yaml')
    def handle_read_crawl_yaml(data):
        """Return .crawl.yaml contents for a folder path (relative to storage_dir parent).
        If no .crawl.yaml at this level but sub-folders have one, returns
        {'mode': 'bulk', 'sites': [<subfolder-names>]}."""
        folder_path = (data.get('path', '') or '').strip()
        abs_folder = (
            os.path.abspath(os.path.join(storage_dir, '..', folder_path))
            if folder_path else storage_dir
        )
        conf = read_crawl_yaml(abs_folder)
        if conf:
            return conf
        # No .crawl.yaml at this level — scan immediate sub-directories
        sites = []
        try:
            for entry in sorted(os.scandir(abs_folder), key=lambda e: e.name):
                if entry.is_dir() and read_crawl_yaml(entry.path):
                    sites.append(entry.name)
        except OSError:
            pass
        if sites:
            return {'mode': 'bulk', 'sites': sites}
        return {}

    @socketio.on('emit_WebSearch_get_folders')
    def handle_get_folders(data):
        path = data.get('path', '') if data else ''
        return ws_file_manager.get_folders(path)

    @socketio.on('emit_WebSearch_get_files')
    def handle_get_files(input_data):
        """Return paginated, scored file list — mirrors text module pattern."""

        # Define available filters
        def _filter_fuzzy_title(files, text_query, **__):
            q = _normalize_text(text_query)
            q_raw = text_query.strip().lower()
            scorer = rapidfuzz.fuzz.token_set_ratio if ' ' in q else rapidfuzz.fuzz.WRatio
            scores = []
            for f in files:
                title, url = ws_engine.get_title_and_url(f)
                s_title = scorer(q, _normalize_text(title))
                s_url   = scorer(q, _normalize_text(url))
                combined = max(1.3 * s_title, s_url)
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

        def _filter_recommendation(files, *_, **__):
            files_data = []
            for f in files:
                file_hash = ws_engine.get_file_hash(f)
                db_item = db_models.WebPage.query.filter_by(hash=file_hash).first() if file_hash else None
                files_data.append({
                    'user_rating': db_item.user_rating if db_item else None,
                    'model_rating': db_item.model_rating if db_item else None,
                    'full_play_count': 1,
                    'skip_count': 0,
                    'last_played': db_item.last_viewed if db_item else None,
                })
            return np.array(sort_files_by_recommendation(files, files_data), dtype=np.float32)

        def _filter_recent(files, *_, **__):
            ts = []
            for f in files:
                meta = ws_engine.get_frontmatter(f)
                try:
                    dt = datetime.datetime.fromisoformat(str(meta.get('crawl_date', '')))
                    ts.append(dt.timestamp())
                except Exception:
                    ts.append(0.0)
            ts = np.array(ts, dtype=np.float32)
            rng = ts.max() - ts.min() if len(ts) else 0
            return (ts - ts.min()) / (rng + 1e-8)

        filters = {
            'by_text': _filter_by_text,
            'rating': ws_filters.filter_by_rating,
            'recommendation': _filter_recommendation,
            'recent': _filter_recent,
        }

        def get_file_info(full_path, file_hash):
            db_item = db_models.WebPage.query.filter_by(hash=file_hash).first()
            meta = ws_engine.get_frontmatter(full_path)

            return {
                'user_rating': db_item.user_rating if db_item else None,
                'model_rating': db_item.model_rating if db_item else None,
                'last_viewed': db_item.last_viewed.isoformat() if db_item and db_item.last_viewed else None,
                'url': meta.get('url', ''),
                'domain': meta.get('domain', ''),
                'title': meta.get('title', ''),
                'preview_text': meta.get('preview', _get_text_preview(full_path)),
                'crawl_date': meta.get('crawl_date', ''),
                'last_crawl_date': meta.get('last_crawl_date', ''),
                'file_data': ws_engine.get_metadata(full_path),
            }

        def update_model_ratings(file_paths):
            _update_model_ratings(file_paths)

        # Build params dict with only the keys FileManager.get_files() accepts
        _allowed = {'path','pagination','limit','text_query','seed','filters',
                    'get_file_info','update_model_ratings','mode','order',
                    'temperature','evaluator_hash'}
        input_params = {k: v for k, v in input_data.items() if k in _allowed}
        input_params.update({
            'filters': filters,
            'get_file_info': get_file_info,
            'update_model_ratings': update_model_ratings,
            'evaluator_hash': evaluator.hash,
        })

        _maybe_trigger_rescore()

        return ws_file_manager.get_files(**input_params)

    @socketio.on('emit_WebSearch_set_rating')
    def handle_set_rating(data):
        """Set a user rating for a page (by hash, matching text module pattern)."""
        file_hash = data.get('hash')
        file_path = data.get('file_path')
        rating = data.get('rating')
        if rating is None:
            return {'error': 'rating required'}

        # Find or create the DB row
        page = None
        if file_hash:
            page = db_models.WebPage.query.filter_by(hash=file_hash).first()
        if page is None and file_path:
            # Try by file_path
            page = db_models.WebPage.query.filter_by(file_path=file_path).first()
        if page is None and file_hash:
            # Create new row
            page = db_models.WebPage(
                hash=file_hash,
                hash_algorithm=HASH_ALGORITHM,
                file_path=file_path,
            )
            db_models.db.session.add(page)

        if page is None:
            return {'error': 'Could not find or create page record'}

        page.user_rating = float(rating)
        page.user_rating_date = datetime.datetime.utcnow()
        _touch_last_viewed(page)
        return page.as_dict()

    @socketio.on('emit_WebSearch_get_page_content')
    def handle_get_page_content(data):
        """Return the markdown content of a stored page (body without front-matter)."""
        file_path = data.get('file_path', '')
        full_path = os.path.join(storage_dir, file_path) if file_path else ''

        if not full_path or not os.path.exists(full_path):
            return {'error': 'Page not found'}

        with open(full_path, 'r', encoding='utf-8') as f:
            meta, body = parse_frontmatter(f.read())

        # Update last_viewed
        file_hash = ws_engine.get_file_hash(full_path)
        if file_hash:
            page = db_models.WebPage.query.filter_by(hash=file_hash).first()
            if page:
                _touch_last_viewed(page)

        result = {
            'file_path': file_path,
            'content': body,
            'url': meta.get('url', ''),
            'title': meta.get('title', ''),
        }
        socketio.emit('emit_WebSearch_show_page_content', result)
        return result

    @socketio.on('emit_WebSearch_mark_viewed')
    def handle_mark_viewed(data):
        """Mark a page as viewed (e.g. when user clicks the external link)."""
        file_hash = data.get('hash', '')
        if file_hash:
            page = db_models.WebPage.query.filter_by(hash=file_hash).first()
            if page:
                _touch_last_viewed(page)

    def _task_restore_missing(ctx):
        """Task: re-crawl any WebPage records whose .md file is missing on disk."""
        all_pages = db_models.WebPage.query.filter(
            db_models.WebPage.file_path.isnot(None),
            db_models.WebPage.url.isnot(None),
        ).all()
        missing = [
            p for p in all_pages
            if not os.path.exists(os.path.join(storage_dir, p.file_path))
        ]
        if not missing:
            return
        print(f"[WebSearch] {len(missing)} .md file(s) missing — restoring…")
        for i, page in enumerate(missing):
            ctx.check()
            ctx.update((i + 1) / len(missing),
                       f'Restoring {i + 1}/{len(missing)}: {page.url}')
            try:
                crawler.crawl_single_page(page.url, app=app)
            except Exception as exc:
                print(f"[WebSearch] Could not restore {page.url}: {exc}")
        print(f"[WebSearch] Restoration complete — {len(missing)} page(s) processed.")

    # ── Startup background tasks ─────────────────────────────────────────
    task_manager.submit('WebSearch: restore missing pages', _task_restore_missing)
    if os.path.exists(evaluator_path):
        _scoring_state['in_progress'] = True
        task_manager.submit('WebSearch: score unscored pages', _task_score_pages)

    common_socket_events.show_loading_status('Web Search module ready!')
