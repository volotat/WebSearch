"""
Web crawler for IndieWeb blog exploration.

Fetches HTML pages, converts them to Markdown via markitdown,
extracts same-domain links, and performs BFS crawling with
optional model scoring.
"""

import os
import re
import time
import hashlib
import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from markitdown import MarkItDown

import modules.WebSearch.db_models as db_models


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Extensions that are never crawl-worthy (images, media, archives, etc.).
# Links pointing to these are silently skipped at extraction time.
# NOTE: document formats (.pdf, .docx, …) are intentionally NOT listed here —
# they are handled separately by _fetch_and_store via markitdown conversion.
_SKIP_EXTENSIONS = frozenset({
    # images
    '.webp', '.jpg', '.jpeg', '.png', '.gif', '.svg', '.ico', '.bmp', '.tiff', '.avif',
    # audio / video
    '.mp3', '.mp4', '.ogg', '.wav', '.flac', '.webm', '.avi', '.mov', '.mkv',
    # binary / archive / data
    '.csv', '.json', '.xml', '.zip', '.tar', '.gz', '.rar', '.7z',
    # fonts
    '.woff', '.woff2', '.ttf', '.otf', '.eot',
    # stylesheets / scripts (no user-readable text)
    '.css', '.js', '.map',
})

# markitdown-supported document MIME types mapped to the file extension that
# markitdown uses to select the right converter.
_DOCUMENT_MIME_TO_EXT = {
    'application/pdf':                                                             '.pdf',
    'application/msword':                                                          '.doc',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document':    '.docx',
    'application/vnd.ms-excel':                                                    '.xls',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet':          '.xlsx',
    'application/vnd.ms-powerpoint':                                               '.ppt',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation':  '.pptx',
}
_DOCUMENT_EXTENSIONS = frozenset(_DOCUMENT_MIME_TO_EXT.values())


def _temp_ext_for(url: str, content_type: str):
    """
    Return the file extension to give a markitdown temp file, or None if the
    resource is not a supported document type.
    URL extension takes precedence over Content-Type so markitdown always picks
    the correct converter (it dispatches based on file extension).
    """
    url_ext = os.path.splitext(urlparse(url).path.lower())[1]
    if url_ext in _DOCUMENT_EXTENSIONS:
        return url_ext
    mime = content_type.split(';')[0].strip().lower()
    return _DOCUMENT_MIME_TO_EXT.get(mime)


def _normalise_url(url: str) -> str:
    """Strip fragment and trailing slash for deduplication."""
    parsed = urlparse(url)
    path = parsed.path.rstrip('/') or '/'
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def _blake2b(content: bytes) -> str:
    """Fast 128-bit BLAKE2b digest as hex string (32 chars, faster than MD5)."""
    return hashlib.blake2b(content, digest_size=16).hexdigest()


def _extract_title(html: str) -> str:
    """Return the <title> text or an empty string."""
    soup = BeautifulSoup(html, 'html.parser')
    tag = soup.find('title')
    return tag.get_text(strip=True) if tag else ''


def _extract_same_domain_links(html: str, base_url: str, domain: str):
    """Yield absolute URLs that belong to *domain* and look like HTML pages."""
    soup = BeautifulSoup(html, 'html.parser')
    for a_tag in soup.find_all('a', href=True):
        href = a_tag['href']
        abs_url = urljoin(base_url, href)
        parsed = urlparse(abs_url)
        if parsed.netloc != domain or parsed.scheme not in ('http', 'https'):
            continue
        # Skip links whose path ends with a known non-HTML extension.
        path_lower = parsed.path.lower()
        ext = os.path.splitext(path_lower)[1]
        if ext in _SKIP_EXTENSIONS:
            continue
        yield _normalise_url(abs_url)


def _url_to_filepath(url: str, storage_dir: str) -> str:
    """
    Map a URL to a .md path inside *storage_dir*.
    e.g. https://example.com/blog/post-1  ->  <storage_dir>/example.com/blog/post-1.md
    """
    parsed = urlparse(url)
    domain = parsed.netloc
    path = parsed.path.strip('/')
    if not path:
        path = 'index'
    # Sanitise path segments
    safe_path = re.sub(r'[^\w\-./]', '_', path)
    return os.path.join(storage_dir, domain, f"{safe_path}.md")


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class SiteCrawler:
    """
    BFS crawler that converts pages to Markdown and stores them.

    Parameters
    ----------
    storage_dir : str
        Root directory where .md files are written
        (e.g. ``/mnt/project_config/modules/WebSearch``).
    crawl_delay : float
        Seconds to wait between HTTP requests (default 1.0).
    max_pages : int
        Maximum number of pages to crawl per domain (default 50).
    request_timeout : int
        HTTP request timeout in seconds (default 15).
    status_callback : callable or None
        ``fn(message: str)`` called to report progress.
    """

    HASH_ALGORITHM = 'blake2b:v1'

    def __init__(
        self,
        storage_dir: str,
        crawl_delay: float = 1.0,
        max_pages: int = 50,
        request_timeout: int = 15,
        status_callback=None,
    ):
        self.storage_dir = storage_dir
        self.crawl_delay = crawl_delay
        self.max_pages = max_pages
        self.request_timeout = request_timeout
        self._status = status_callback or (lambda m: None)
        self._markitdown = MarkItDown()

    # ---- public API --------------------------------------------------------

    def crawl_site(self, start_url: str, app=None, sublinks_only: bool = False, recrawl: bool = False):
        """
        Crawl starting from *start_url* (BFS, same-domain only).

        Parameters
        ----------
        start_url : str
            The seed URL to begin crawling from.
        app : Flask app
            Needed for DB access in a background thread.
        sublinks_only : bool
            When True only follow links whose URL path begins with the
            start URL's path (e.g. only pages inside a specific subreddit).
        recrawl : bool
            When True, pages whose raw HTTP content hash hasn't changed are
            skipped (markitdown is never called), but their links are still
            followed so new pages on index/listing pages are discovered.

        Returns
        -------
        list[dict]
            List of page info dicts for every page successfully crawled.
        """
        start_url = _normalise_url(start_url)
        domain = urlparse(start_url).netloc
        # Path prefix used when sublinks_only=True
        start_path_prefix = urlparse(start_url).path.rstrip('/')

        visited = set()
        queue = [start_url]
        results = []
        skipped = 0

        while queue and len(visited) < self.max_pages:
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)

            self._status(f"Crawling {len(visited)}/{self.max_pages}: {url}")

            try:
                page_info = self._fetch_and_store(url, domain, app, recrawl=recrawl)
            except Exception as exc:
                print(f"[WebSearch] Error crawling {url}: {exc}")
                continue

            if page_info is not None:
                if page_info.get('_unchanged'):
                    skipped += 1
                else:
                    results.append(page_info)
                # Extract links for BFS regardless of whether the page changed
                for link in page_info.get('_links', []):
                    if link in visited:
                        continue
                    if sublinks_only:
                        link_path = urlparse(link).path
                        if link_path != start_path_prefix and not link_path.startswith(start_path_prefix + '/'):
                            continue
                    queue.append(link)

            # Polite delay between requests
            if queue:
                time.sleep(self.crawl_delay)

        if recrawl:
            self._status(f"Recrawl complete – {len(results)} updated, {skipped} unchanged for {domain}")
        else:
            self._status(f"Crawl complete – {len(results)} pages saved for {domain}")
        return results

    def crawl_single_page(self, url: str, app=None):
        """Fetch and store a single page (no link following)."""
        url = _normalise_url(url)
        domain = urlparse(url).netloc

        self._status(f"Fetching {url} …")
        try:
            page_info = self._fetch_and_store(url, domain, app)
        except Exception as exc:
            self._status(f"Error: {exc}")
            return None

        return page_info

    # ---- internal ----------------------------------------------------------

    def _fetch_and_store(self, url, domain, app, recrawl=False):
        """Fetch one URL, convert to .md, persist to disk + DB.  Returns info dict or None."""
        resp = requests.get(url, timeout=self.request_timeout, headers={
            'User-Agent': 'Anagnorisis-WebSearch/1.0',
        })
        resp.raise_for_status()

        content_type = resp.headers.get('Content-Type', '')
        is_html = 'html' in content_type.lower()
        raw_bytes = resp.content

        # Compute fast hash of raw HTTP bytes (used for change detection)
        file_hash = _blake2b(raw_bytes)

        if is_html:
            # ── HTML page ──────────────────────────────────────────────
            title = _extract_title(resp.text)
            same_domain_links = list(_extract_same_domain_links(resp.text, url, domain))
            tmp_suffix = '.html'
        else:
            # ── Non-HTML resource — accept only markitdown-supported docs
            tmp_suffix = _temp_ext_for(url, content_type)
            if tmp_suffix is None:
                return None  # image, binary, stylesheet, etc. — skip silently
            title = ''          # no <title> tag in documents; URL used as fallback in UI
            same_domain_links = []  # documents don't contain navigable same-domain links

        # ── Recrawl fast-path: skip markitdown if raw content is unchanged ─
        # Links are still returned so BFS can discover new pages on index pages.
        if recrawl:
            with app.app_context():
                existing = db_models.WebPage.query.filter_by(url=url).first()
                if (
                    existing and
                    existing.hash_algorithm == self.HASH_ALGORITHM and
                    existing.hash == file_hash
                ):
                    return {'_links': same_domain_links, '_unchanged': True, 'id': existing.id}

        # Convert to Markdown via markitdown.
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=tmp_suffix, delete=False, mode='wb') as tmp:
            tmp.write(raw_bytes)
            tmp_path = tmp.name
        try:
            md_result = self._markitdown.convert(tmp_path)
            md_text = md_result.text_content if md_result else ''
        finally:
            os.unlink(tmp_path)

        # Build filesystem path
        md_path = _url_to_filepath(url, self.storage_dir)
        os.makedirs(os.path.dirname(md_path), exist_ok=True)

        # Write .md file
        with open(md_path, 'w', encoding='utf-8') as f:
            f.write(md_text)

        # Preview text (first ~300 chars)
        preview = md_text[:300].strip()
        if len(md_text) > 300:
            preview += '…'

        rel_path = os.path.relpath(md_path, self.storage_dir)
        parsed = urlparse(url)
        now = datetime.datetime.utcnow()

        # Persist to DB
        with app.app_context():
            existing = db_models.WebPage.query.filter_by(url=url).first()
            if existing:
                existing.hash = file_hash
                existing.hash_algorithm = self.HASH_ALGORITHM
                existing.md_file_path = rel_path
                existing.title = title or existing.title
                existing.preview_text = preview
                existing.last_crawl_date = now
                db_models.db.session.commit()
                page_id = existing.id
            else:
                page = db_models.WebPage(
                    hash=file_hash,
                    hash_algorithm=self.HASH_ALGORITHM,
                    url=url,
                    domain=domain,
                    url_path=parsed.path,
                    md_file_path=rel_path,
                    title=title,
                    preview_text=preview,
                    crawl_date=now,
                    last_crawl_date=now,
                )
                db_models.db.session.add(page)
                db_models.db.session.commit()
                page_id = page.id

        return {
            'id': page_id,
            'url': url,
            'title': title,
            'md_file_path': rel_path,
            'preview_text': preview,
            'hash': file_hash,
            '_links': same_domain_links,
        }
