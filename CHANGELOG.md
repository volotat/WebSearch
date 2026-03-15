# Web Search Module - Changelog

### Version 0.1.1 (14.03.2026)
*   **Crawler**
    *   Incremental Recrawl: `SiteCrawler.crawl_site()` gains a `recrawl` parameter. When `True`, the raw HTTP response bytes are hashed (BLAKE2b) immediately after fetching, before markitdown is ever invoked. If the hash matches the stored value the page is skipped entirely — but its links are still extracted from the freshly-fetched HTML so index/listing pages (e.g. `/articles`, `/r/LocalLLaMA/`) can still surface new content.
    *   BLAKE2b hashing: Replaced MD5-of-markdown content hashing with BLAKE2b-128 (`hashlib.blake2b(digest_size=16)`) of raw HTTP response bytes (`blake2b:v1`). Hashing raw bytes is faster (no markitdown needed for the comparison), and BLAKE2b is significantly faster than MD5. `hash_algorithm` column is updated on every write so old `md5:v1` rows are transparently upgraded on next crawl.
    *   Sublinks-Only Mode: `SiteCrawler.crawl_site()` gains a `sublinks_only` parameter. When `True`, BFS only queues links whose URL path is equal to or starts with the seed URL's path, effectively scoping the crawl to a subtree of the site.
    *   Link Filtering: Added `_SKIP_EXTENSIONS` blocklist - image, audio/video, font, CSS/JS, and archive links are now discarded at link-extraction time, before any HTTP request is made. Prevents wasted requests and `max_pages` budget consumption on binary resources.
    *   Document Support: PDF, DOC/DOCX, XLS/XLSX, PPT/PPTX linked from crawled pages are now fetched and converted to Markdown via markitdown. The correct temp-file extension is derived from the URL path first, then from the `Content-Type` header (`_DOCUMENT_MIME_TO_EXT` / `_temp_ext_for()`), ensuring markitdown selects the right converter. Documents produce no BFS links and have an empty title (URL shown as fallback in the UI). The rest of the pipeline - scoring, training, storage - is unchanged.
    *   Document Support: `_fetch_and_store` refactored to branch on `is_html`: HTML path is unchanged; non-HTML resources are accepted only if they resolve to a supported document extension, otherwise dropped silently.
*   **Backend**
    *   Per-Request Crawl Settings: `emit_web_search_crawl_site` now accepts `crawl_delay`, `max_pages`, and `sublinks_only` fields from the client. A fresh `SiteCrawler` instance is created per crawl request so custom delays and page limits do not affect concurrent crawls.
    *   Recrawl socket event: New `emit_web_search_recrawl_site` handler (`{url, max_pages?, crawl_delay?, sublinks_only?}`). Runs `crawl_site(..., recrawl=True)` in a background thread; only changed/new pages are passed to the scorer.
    *   Background Scoring: `_bulk_score_unscored()` is now launched in a background daemon thread at startup instead of running synchronously, so the module becomes available immediately while scoring progresses in the status bar.
    *   Background Scoring: Added `_maybe_trigger_rescore()`, called on every `emit_web_search_get_pages` request. It compares `evaluator.hash` against the hash used in the last completed bulk-score run and starts a new background scoring thread if they differ - pages are automatically re-scored after retraining the universal evaluator without requiring a container restart.
    *   Background Scoring: Added `_scoring_state` dict (`last_hash`, `in_progress`) to track scoring thread lifecycle and prevent concurrent duplicate runs.
*   **Frontend**
    *   Recrawl context menu + modal: Right-clicking a domain in the sidebar opens a context menu (`ContextMenuComponent`) with a "↻ Recrawl site" option.
    *   Recrawl context menu + modal: Selecting it opens a dedicated `#ws_recrawl_modal` with: editable seed URL (pre-filled from the domain), sublinks-only checkbox, crawl delay input, max pages input; Confirm/Cancel buttons.
    *   Add Page Modal: Replaced the URL text-input / `+` button / "Crawl Site" button in the sidebar with a single `+ Add page` button that opens a dedicated modal.
    *   Add Page Modal: Modal contains: a URL input field; an optional `StarRatingComponent` so the user can rate the page at add time; a "Start crawling website from this page" checkbox; a "Crawl only sublinks" checkbox (disabled until crawling is enabled) that restricts BFS to pages whose URL path starts with the seed URL's path (e.g. a specific subreddit); a configurable crawl-delay number input (disabled until crawling is enabled, default 0.5 s, with a hint to use ≥ 3 s for rate-limited sites like Reddit); a "Max pages to crawl" number input (disabled until crawling is enabled, default 5000); Confirm and Cancel buttons.
    *   Add Page Modal: An optional user rating supplied in the modal is persisted immediately after the page is fetched (`emit_web_search_add_page`) or applied to the seed URL after a crawl completes (`emit_web_search_crawl_site`).
    
### Version 0.1.0 (10.03.2026)
Initial implementation.
*   **Core Crawler:**
    *   `crawler.py` - `SiteCrawler` class with two entry points: `crawl_single_page()` (fetch one URL) and `crawl_site()` (BFS, same-domain only, configurable page limit).
    *   HTML→Markdown conversion via `markitdown`; HTML written to a temp file since `convert_stream()` is not available in the installed version.
    *   Link extraction via `BeautifulSoup`; only same-domain `http/https` links are followed.
    *   Content hash (`md5:v1`) computed on the Markdown text; re-crawling a page updates the hash and `last_crawl_date` without creating a duplicate row.
    *   Polite crawl delay (`crawl_delay` config, default 0.5 s); configurable `max_pages_per_site` (default 5000).
    *   Stored `.md` files are organized as `{storage_dir}/{domain}/{url_path}.md`, mirroring the URL structure on disk.
*   **Database Model:**
    *   `WebPage` table (`db_models.py`): `url` (unique), `domain`, `md_file_path`, `title`, `preview_text`, `user_rating`, `user_rating_date`, `model_rating`, `model_hash`, `crawl_date`, `last_crawl_date`.
    *   `domain` is stored directly on `WebPage`; there is no separate `WebSite` table - per-domain statistics are computed live with a `GROUP BY domain` query, so counts are always exact regardless of how a page was added.
    *   Alembic migration `a3f8c12d9e01` creates the table; removes the earlier `WebSite` table and adds `domain` directly to `WebPage`.
*   **Backend (`serve.py`):**
    *   `emit_web_search_add_page` - crawls a single URL in a background thread, scores it, emits `emit_web_search_page_added`.
    *   `emit_web_search_crawl_site` - BFS-crawls a domain in a background thread, batch-scores all new pages, emits updated sites list when done.
    *   `emit_web_search_get_sites` - returns live per-domain stats (`domain`, `pages`, `last_crawl_date`, `crawl_status`) via `GROUP BY`; in-memory `_crawl_status_map` tracks actively crawling domains.
    *   `emit_web_search_get_pages` - paginated, filterable by `domain` and `path` (md_file_path prefix), sortable by `rating` / `recent` / `alpha`; pages with no rating are excluded from results.
    *   `emit_web_search_get_folders` - builds a `FolderViewComponent`-compatible folder tree dict from rated pages' `md_file_path` values, scoped to a single domain.
    *   `emit_web_search_set_rating` - persists a user rating and returns the updated page dict.
    *   `emit_web_search_get_page_content` - reads and returns the stored `.md` file content.
    *   Automatic bulk scoring at startup via `_bulk_score_unscored()`.
*   **Scoring:**
    *   Uses the shared `TextEmbedder` + `UniversalEvaluator` (same model as all other modules).
    *   `_score_page()` reads the `.md` file, embeds it with `text_embedder.embed_text()`, and calls `evaluator.predict()`.
    *   `_score_and_update()` wraps the above with a staleness check (`model_hash` comparison) and DB persistence.
*   **Universal Evaluator Training Integration:**
    *   `train.py` exposes `get_training_pairs(cfg, text_embedder, status_callback)` - queries all user-rated pages, reads each `.md` file, embeds it, and yields `(chunk_embeddings, user_rating)` pairs.
    *   Auto-discovered by `universal_train._gather_from_module_train_files()` - no changes to core training code needed.
*   **Frontend:**
    *   Wide card layout (title + URL + preview on the left, star rating on the right).
    *   Left sidebar: domain list (with live page counts) + folder tree (`FolderViewComponent`). Clicking a domain scopes the feed; clicking a folder filters by `md_file_path` prefix; clicking the active folder deselects it.
    *   Page content viewed in a modal with rendered Markdown (`marked.js` + `DOMPurify`); the modal includes a star rating widget.
    *   Order buttons: By Rating (default) / Most Recent / Alphabetical. Pagination with ellipsis for large result sets.
*   **Module Self-Containment:**
    *   `pages/web_search/requirements.txt` - `markitdown`, `beautifulsoup4`.
    *   `pages/web_search/config.defaults.yaml` - `storage_directory`, `crawl_delay`, `max_pages_per_site` with sensible defaults.
