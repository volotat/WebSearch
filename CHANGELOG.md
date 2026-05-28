# Web Search Module - Changelog

### Version 0.4.1.5 [for Anagnorisis ≥ 0.4.1] (29.05.2026)
*   **Background Rating Priority:**
    *   The background scoring task now processes pages that have never been rated before re-scoring pages whose rating is simply outdated. Previously both groups were mixed, so a newly crawled page could wait behind a large re-rating queue. Now freshly crawled pages always get their first score as soon as possible.
*   **Fixed missing `markitdown` dependency**
    *   Added `markitdown` to `requirements.txt`; its absence caused the WebSearch module to fail on startup.

### Version 0.3.10.4 [for Anagnorisis > 0.3.10] (05.04.2026)
*   **Background Processing via Task Manager:**
    *   All long-running operations (add page, crawl site, bulk recrawl, single-folder recrawl, score unscored pages, restore missing Markdown files) are now submitted to the centralised `TaskManager` queue (`app.task_manager.submit()`) instead of being launched as bare `threading.Thread` daemon threads.
    *   Each task receives a `TaskContext` and calls `ctx.check()` / `ctx.update()` for cooperative pause, cancel, and live progress reporting.
    *   Crawl progress is no longer written to the global search-status bar (`show_search_status`); it is reported exclusively through the Task Manager modal visible on all pages.

### Version 0.3.7.3 [for Anagnorisis > 0.3.7]  (02.04.2026)
*   **Architecture**
    *   Metadata moved out of the database into `.md` file front-matter (hugo-style `---` blocks). Fields `domain`, `url_path`, `title`, `preview_text`, `crawl_date`, `last_crawl_date` are no longer DB columns — they are written and read from each file's YAML header. The DB schema now mirrors the text/music/images modules exactly.
    *   Added `last_viewed` column (analogous to `last_played` in the music module). Updated on every rating change, page-content open, and external-link click.
    *   Added `raw_hash` column — stores the BLAKE2b hash of the raw HTTP response bytes directly in the DB for fast recrawl change-detection without reading `.md` files from disk.
    *   `url` column retained in the DB for fast recrawl matching.
    *   `file_path` replaces `md_file_path` as the column name, consistent with other modules.
*   **Crawler**
    *   Front-matter support: `_fetch_and_store()` writes a YAML header (`url`, `domain`, `title`, `preview`, `raw_hash`, `crawl_date`, `last_crawl_date`) to each `.md` file. `parse_frontmatter()` / `build_frontmatter()` helpers handle reading and writing.
    *   Hash source changed: the BLAKE2b hash stored in the DB is computed from the full `.md` file bytes (front-matter + body). `raw_hash` (hash of raw HTTP response) is stored both in the front-matter and in the DB column for fast recrawl comparison.
    *   `.crawl.yaml` files: written progressively during crawling into every folder (and parent folders up to the domain root) as pages are saved. This ensures crawl metadata is preserved even if a crawl is interrupted, and allows recrawling any subfolder independently. Each file records `seed_url`, `sublinks_only`, `crawl_delay`, `max_pages`, and `last_crawl`.
    *   Recrawl fast-path: compares `raw_hash` from the DB column directly instead of reading `.md` file front-matter, avoiding disk I/O for unchanged pages.
    *   Recrawl preservation: `user_rating` and `last_viewed` are always retained; `model_rating` is invalidated only when page content has actually changed.
    *   `crawl_date` is preserved from existing front-matter on recrawl so the original timestamp is not overwritten.
*   **Backend**
    *   `FileManager` integration: replaced the bespoke folder/file listing with `FileManager` (same class used by all other modules). `media_formats={'.md'}`, `db_schema=WebPage`.
    *   `_WebSearchTextEngine` adapter: bridges `FileManager` / `CommonFilters` to WebSearch specifics — computes BLAKE2b hash from `.md` file bytes, reads front-matter from a two-level cache, provides semantic embedding via `TextEmbedder`.
    *   `get_file_info()` callback reads `title`, `url`, `preview`, `crawl_date`, `last_viewed` from front-matter + DB, returning them as `file_info.*` fields in the standard `files_data` response format.
    *   New `emit_WebSearch_recrawl_folder` handler: reads `.crawl.yaml` from the target folder and re-crawls using the stored parameters.
    *   New `emit_WebSearch_mark_viewed` handler: updates `last_viewed` when the user clicks an external link.
    *   `emit_WebSearch_set_rating` and `emit_WebSearch_get_page_content` now also update `last_viewed`.
    *   Removed `emit_WebSearch_get_sites` and `emit_WebSearch_get_pages`; replaced by `emit_WebSearch_get_folders` and `emit_WebSearch_get_files` (FileManager-standard naming and response format).
*   **Frontend**
    *   Sidebar replaced: the domain list (`#ws_sites_list`) is removed. The sidebar now contains only the `+ Add page` button and a `FolderViewComponent` showing the full filesystem folder tree - selecting any folder shows pages from that folder and all subfolders.
    *   Right-click on any folder shows a "↻ Recrawl folder" context menu option, opening a modal pre-populated from the folder's `.crawl.yaml`.
    *   Fixed double context menu appearing on folder right-click (disabled `FolderViewComponent`'s built-in file-management menu, kept only the WebSearch-specific recrawl menu).
    *   Card data now reads from `fileData.file_info.*` (FileManager response format) instead of flat page fields.
    *   External-link clicks emit `emit_WebSearch_mark_viewed` to update `last_viewed`.

### Version 0.3.7.2 [for Anagnorisis > 0.3.7] (21.03.2026)
*   **Search & Filtering**
    *   Search bar integration: replaced the static order buttons with `SearchBarComponent` (shared with text/images/music/video modules). Supports fuzzy title/URL search (`file-name` mode) and semantic content search (`semantic-content` mode), plus keyword shortcuts: `rating`, `recommendation`, `recent`.
    *   URL-driven navigation: switched from SPA-style socket-on-demand to the same URL-reload pattern used by all other modules (`autoSyncUrl: true`, `ensureDefaultsInUrl: true`). Search mode, order, temperature, seed, page, domain, and folder path are all persisted in the URL — browser back/forward and bookmarks work correctly. Pagination links are real `href` URLs; site and folder clicks call `_navigateTo()` to rewrite the URL and reload.
    *   Fuzzy search fix: `file-name` mode now matches against `page.title` (1.3× weight) and `page.url` instead of using only the on-disk `.md` filename. Implemented as a local `_filter_fuzzy_title()` closure in `handle_get_pages` using `rapidfuzz` and `_normalize_text` already available from `CommonFilters`. Semantic search (`semantic-content` mode) is unchanged.
    *   Recommendation sort: added `recommendation` keyword routed to `sort_files_by_recommendation` (same engine as other modules).
    *   `CommonFilters` reuse: removed the bespoke `WebSearchFilters` class; WebSearch now uses `CommonFilters` directly via a `_WebSearchTextEngine` adapter. The adapter's `seed_hashes()` / `seed_titles()` methods are called per request to map `.md` paths to the DB-stored blake2b hash and human-readable title/URL without extra I/O.
*   **Crawler**
    *   HTML boilerplate stripping: before passing HTML to markitdown, a `_strip_boilerplate()` pre-processing step removes semantic layout tags (`<nav>`, `<header>`, `<footer>`, `<aside>`, `<form>`, `<dialog>`), elements with boilerplate ARIA roles (`navigation`, `banner`, `contentinfo`, etc.), and elements whose `class` or `id` matches common patterns (`sidebar`, `breadcrumb`, `cookie`, `social-share`, `related-posts`, etc.). Falls back to the original HTML silently on any error. Non-HTML documents (PDF, DOCX, …) are unaffected.
    *   HTML conversion uses `markitdown.convert_stream()` with a `StreamInfo` object instead of writing a temp file for HTML pages.
*   **Reliability**
    *   Automatic `.md` file restoration: a `_restore_missing_md_files()` background daemon thread runs at every startup. It compares `md_file_path` values in the DB against the filesystem and silently re-crawls any URL whose file is missing. Unreachable URLs are skipped gracefully (DB record retained).

### Version 0.3.7.1 [for Anagnorisis > 0.3.7] (14.03.2026)
*   **Crawler**
    *   Incremental Recrawl: `SiteCrawler.crawl_site()` gains a `recrawl` parameter. When `True`, the raw HTTP response bytes are hashed (BLAKE2b) immediately after fetching, before markitdown is ever invoked. If the hash matches the stored value the page is skipped entirely — but its links are still extracted from the freshly-fetched HTML so index/listing pages (e.g. `/articles`, `/r/LocalLLaMA/`) can still surface new content.
    *   BLAKE2b hashing: Replaced MD5-of-markdown content hashing with BLAKE2b-128 (`hashlib.blake2b(digest_size=16)`) of raw HTTP response bytes (`blake2b:v1`). Hashing raw bytes is faster (no markitdown needed for the comparison), and BLAKE2b is significantly faster than MD5. `hash_algorithm` column is updated on every write so old `md5:v1` rows are transparently upgraded on next crawl.
    *   Sublinks-Only Mode: `SiteCrawler.crawl_site()` gains a `sublinks_only` parameter. When `True`, BFS only queues links whose URL path is equal to or starts with the seed URL's path, effectively scoping the crawl to a subtree of the site.
    *   Link Filtering: Added `_SKIP_EXTENSIONS` blocklist - image, audio/video, font, CSS/JS, and archive links are now discarded at link-extraction time, before any HTTP request is made. Prevents wasted requests and `max_pages` budget consumption on binary resources.
    *   Document Support: PDF, DOC/DOCX, XLS/XLSX, PPT/PPTX linked from crawled pages are now fetched and converted to Markdown via markitdown. The correct temp-file extension is derived from the URL path first, then from the `Content-Type` header (`_DOCUMENT_MIME_TO_EXT` / `_temp_ext_for()`), ensuring markitdown selects the right converter. Documents produce no BFS links and have an empty title (URL shown as fallback in the UI). The rest of the pipeline - scoring, training, storage - is unchanged.
    *   Document Support: `_fetch_and_store` refactored to branch on `is_html`: HTML path is unchanged; non-HTML resources are accepted only if they resolve to a supported document extension, otherwise dropped silently.
*   **Backend**
    *   Per-Request Crawl Settings: `emit_WebSearch_crawl_site` now accepts `crawl_delay`, `max_pages`, and `sublinks_only` fields from the client. A fresh `SiteCrawler` instance is created per crawl request so custom delays and page limits do not affect concurrent crawls.
    *   Recrawl socket event: New `emit_WebSearch_recrawl_site` handler (`{url, max_pages?, crawl_delay?, sublinks_only?}`). Runs `crawl_site(..., recrawl=True)` in a background thread; only changed/new pages are passed to the scorer.
    *   Background Scoring: `_bulk_score_unscored()` is now launched in a background daemon thread at startup instead of running synchronously, so the module becomes available immediately while scoring progresses in the status bar.
    *   Background Scoring: Added `_maybe_trigger_rescore()`, called on every `emit_WebSearch_get_pages` request. It compares `evaluator.hash` against the hash used in the last completed bulk-score run and starts a new background scoring thread if they differ - pages are automatically re-scored after retraining the universal evaluator without requiring a container restart.
    *   Background Scoring: Added `_scoring_state` dict (`last_hash`, `in_progress`) to track scoring thread lifecycle and prevent concurrent duplicate runs.
*   **Frontend**
    *   Recrawl context menu + modal: Right-clicking a domain in the sidebar opens a context menu (`ContextMenuComponent`) with a "↻ Recrawl site" option.
    *   Recrawl context menu + modal: Selecting it opens a dedicated `#ws_recrawl_modal` with: editable seed URL (pre-filled from the domain), sublinks-only checkbox, crawl delay input, max pages input; Confirm/Cancel buttons.
    *   Add Page Modal: Replaced the URL text-input / `+` button / "Crawl Site" button in the sidebar with a single `+ Add page` button that opens a dedicated modal.
    *   Add Page Modal: Modal contains: a URL input field; an optional `StarRatingComponent` so the user can rate the page at add time; a "Start crawling website from this page" checkbox; a "Crawl only sublinks" checkbox (disabled until crawling is enabled) that restricts BFS to pages whose URL path starts with the seed URL's path (e.g. a specific subreddit); a configurable crawl-delay number input (disabled until crawling is enabled, default 0.5 s, with a hint to use ≥ 3 s for rate-limited sites like Reddit); a "Max pages to crawl" number input (disabled until crawling is enabled, default 5000); Confirm and Cancel buttons.
    *   Add Page Modal: An optional user rating supplied in the modal is persisted immediately after the page is fetched (`emit_WebSearch_add_page`) or applied to the seed URL after a crawl completes (`emit_WebSearch_crawl_site`).
    
### Version 0.3.7.0 [for Anagnorisis > 0.3.7] (10.03.2026)
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
    *   `emit_WebSearch_add_page` - crawls a single URL in a background thread, scores it, emits `emit_WebSearch_page_added`.
    *   `emit_WebSearch_crawl_site` - BFS-crawls a domain in a background thread, batch-scores all new pages, emits updated sites list when done.
    *   `emit_WebSearch_get_sites` - returns live per-domain stats (`domain`, `pages`, `last_crawl_date`, `crawl_status`) via `GROUP BY`; in-memory `_crawl_status_map` tracks actively crawling domains.
    *   `emit_WebSearch_get_pages` - paginated, filterable by `domain` and `path` (md_file_path prefix), sortable by `rating` / `recent` / `alpha`; pages with no rating are excluded from results.
    *   `emit_WebSearch_get_folders` - builds a `FolderViewComponent`-compatible folder tree dict from rated pages' `md_file_path` values, scoped to a single domain.
    *   `emit_WebSearch_set_rating` - persists a user rating and returns the updated page dict.
    *   `emit_WebSearch_get_page_content` - reads and returns the stored `.md` file content.
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
    *   `pages/WebSearch/requirements.txt` - `markitdown`, `beautifulsoup4`.
    *   `pages/WebSearch/config.defaults.yaml` - `storage_directory`, `crawl_delay`, `max_pages_per_site` with sensible defaults.
