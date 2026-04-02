"""
Web crawler for IndieWeb blog exploration.

Fetches HTML pages, converts them to Markdown via markitdown,
extracts same-domain links, and performs BFS crawling with
optional model scoring.

Each .md file includes a YAML front-matter header with metadata
(url, domain, title, preview, crawl dates).  The file hash is
computed from the final .md bytes (front-matter + body) so that
any content or metadata change produces a new hash.
"""

import os
import io
import re
import time
import hashlib
import datetime
import tempfile
from urllib.parse import urljoin, urlparse

import yaml
import requests
from bs4 import BeautifulSoup

from markitdown import MarkItDown, StreamInfo

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


# ---------------------------------------------------------------------------
# Front-matter helpers
# ---------------------------------------------------------------------------

def build_frontmatter(meta: dict) -> str:
    """Produce ``---\\n...\\n---\\n`` YAML front-matter from *meta* dict."""
    lines = ['---']
    for key, value in meta.items():
        if value is None:
            continue
        if isinstance(value, str):
            lines.append(f'{key}: "{value}"')
        else:
            lines.append(f'{key}: {value}')
    lines.append('---')
    return '\n'.join(lines) + '\n'


def parse_frontmatter(md_text: str) -> tuple[dict, str]:
    """
    Parse YAML front-matter from a markdown string.

    Returns (meta_dict, body_text).  If no valid front-matter is found,
    returns ({}, full_text).
    """
    if not md_text.startswith('---'):
        return {}, md_text
    end = md_text.find('\n---', 3)
    if end == -1:
        return {}, md_text
    yaml_block = md_text[4:end]
    body = md_text[end + 4:]
    if body.startswith('\n'):
        body = body[1:]
    try:
        meta = yaml.safe_load(yaml_block) or {}
    except yaml.YAMLError:
        return {}, md_text
    return meta, body


# ---------------------------------------------------------------------------
# .crawl.yaml helpers
# ---------------------------------------------------------------------------

def write_crawl_yaml(folder: str, seed_url: str, sublinks_only: bool = False,
                     crawl_delay: float = 1.0, max_pages: int = 50):
    """Write (or update) a .crawl.yaml in *folder*."""
    path = os.path.join(folder, '.crawl.yaml')
    data = {}
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or {}
        except Exception:
            data = {}
    data.update({
        'seed_url': seed_url,
        'sublinks_only': sublinks_only,
        'crawl_delay': crawl_delay,
        'max_pages': max_pages,
        'last_crawl': datetime.datetime.utcnow().isoformat(),
    })
    os.makedirs(folder, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        yaml.safe_dump(data, f, default_flow_style=False)


def read_crawl_yaml(folder: str) -> dict | None:
    """
    Read .crawl.yaml from *folder*.
    Returns the parsed dict or None.
    """
    path = os.path.join(folder, '.crawl.yaml')
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f) or {}
        except Exception:
            return None
    return None


# ---------------------------------------------------------------------------
# Boilerplate stripping
# ---------------------------------------------------------------------------

_BOILERPLATE_TAGS = frozenset({
    'nav', 'header', 'footer', 'aside',
    'form', 'dialog', 'menu', 'menuitem',
})

# ARIA roles whose elements are never main content.
_BOILERPLATE_ROLES = frozenset({
    'navigation', 'banner', 'complementary', 'contentinfo',
    'search', 'menubar', 'toolbar', 'status',
})

# Class/id substrings that reliably indicate boilerplate.
_BOILERPLATE_PATTERNS = (
    'nav', 'navbar', 'sidebar', 'side-bar', 'menu', 'header', 'footer',
    'breadcrumb', 'cookie', 'banner', 'advertisement', 'advert', ' ads-',
    'social-share', 'share-buttons', 'related-posts', 'related-articles',
    'comment', 'widget', 'popup', 'modal', 'overlay', 'toast',
)


def _strip_boilerplate(html: str) -> str:
    """
    Remove structural boilerplate from an HTML page using BeautifulSoup
    (already a project dependency) before passing to markitdown.

    Strategy (in order of reliability):
      1. Remove semantic HTML5 layout tags (<nav>, <header>, <footer>, <aside>).
      2. Remove elements whose ARIA role marks them as non-content.
      3. Remove elements whose class or id contains a boilerplate keyword.

    Operates on the raw HTML string and returns cleaned HTML.
    Falls back to the original HTML if anything goes wrong.
    """
    try:
        soup = BeautifulSoup(html, 'html.parser')

        # 1. Semantic tags
        for tag in soup.find_all(_BOILERPLATE_TAGS):
            tag.decompose()

        # 2. ARIA roles
        for tag in soup.find_all(role=True):
            if tag.get('role', '').lower() in _BOILERPLATE_ROLES:
                tag.decompose()

        # 3. Class / id patterns
        for tag in soup.find_all(True):
            classes = ' '.join(tag.get('class') or [])
            elem_id = tag.get('id') or ''
            combined = (classes + ' ' + elem_id).lower()
            if any(pat in combined for pat in _BOILERPLATE_PATTERNS):
                tag.decompose()

        return str(soup)
    except Exception:
        return html  # never break the crawl


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

HASH_ALGORITHM = 'blake2b:v1'


class SiteCrawler:
    """
    BFS crawler that converts pages to Markdown and stores them.

    Parameters
    ----------
    storage_dir : str
        Root directory where .md files are written.
    crawl_delay : float
        Seconds to wait between HTTP requests (default 1.0).
    max_pages : int
        Maximum number of pages to crawl per domain (default 50).
    request_timeout : int
        HTTP request timeout in seconds (default 15).
    status_callback : callable or None
        ``fn(message: str)`` called to report progress.
    """

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
        Returns list of page info dicts for every page successfully crawled.
        """
        start_url = _normalise_url(start_url)
        domain = urlparse(start_url).netloc
        start_path_prefix = urlparse(start_url).path.rstrip('/')

        visited = set()
        queue = [start_url]
        results = []
        skipped = 0
        crawl_yaml_written = set()  # track folders where .crawl.yaml was already written
        scheme = urlparse(start_url).scheme or 'https'

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

                    # Write .crawl.yaml in the folder where this page was saved
                    # and in every parent folder up to (and including) the domain root.
                    # Each folder gets a seed_url matching its URL path so that
                    # recrawling a subfolder starts from the correct URL.
                    if page_info.get('file_path'):
                        md_abs = os.path.join(self.storage_dir, page_info['file_path'])
                        folder = os.path.dirname(md_abs)
                        domain_folder = os.path.join(self.storage_dir, domain)
                        while folder and len(folder) >= len(domain_folder):
                            if folder not in crawl_yaml_written:
                                # Derive the seed URL for this folder from its path
                                rel = os.path.relpath(folder, domain_folder)
                                if rel == '.':
                                    folder_seed_url = f'{scheme}://{domain}/'
                                else:
                                    folder_seed_url = f'{scheme}://{domain}/{rel}/'
                                write_crawl_yaml(folder, folder_seed_url, sublinks_only,
                                                 self.crawl_delay, self.max_pages)
                                crawl_yaml_written.add(folder)
                            if folder == domain_folder:
                                break
                            folder = os.path.dirname(folder)

                for link in page_info.get('_links', []):
                    if link in visited:
                        continue
                    if sublinks_only:
                        link_path = urlparse(link).path
                        if link_path != start_path_prefix and not link_path.startswith(start_path_prefix + '/'):
                            continue
                    queue.append(link)

            if queue:
                time.sleep(self.crawl_delay)

        # Ensure domain root always has a .crawl.yaml even if all pages were unchanged
        domain_folder = os.path.join(self.storage_dir, domain)
        if domain_folder not in crawl_yaml_written:
            write_crawl_yaml(domain_folder, start_url, sublinks_only,
                             self.crawl_delay, self.max_pages)

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

        # Quick hash of raw HTTP bytes — used for recrawl change detection only
        raw_hash = _blake2b(raw_bytes)

        if is_html:
            title = _extract_title(resp.text)
            same_domain_links = list(_extract_same_domain_links(resp.text, url, domain))
        else:
            tmp_suffix = _temp_ext_for(url, content_type)
            if tmp_suffix is None:
                return None
            title = ''
            same_domain_links = []

        # ── Recrawl fast-path ─────────────────────────────────────────
        # Compare raw HTTP hash against DB raw_hash for fast change detection
        # without reading .md files from disk.
        if recrawl:
            with app.app_context():
                existing = db_models.WebPage.query.filter_by(url=url).first()
                if existing and existing.raw_hash == raw_hash:
                    return {'_links': same_domain_links, '_unchanged': True, 'id': existing.id}

        # ── Convert to Markdown ───────────────────────────────────────
        if is_html:
            cleaned_html = _strip_boilerplate(resp.text)
            md_result = self._markitdown.convert_stream(
                io.BytesIO(cleaned_html.encode('utf-8')),
                stream_info=StreamInfo(mimetype='text/html', charset='utf-8', url=url),
            )
            md_text = md_result.text_content if md_result else ''
        else:
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

        # Preview text (first ~300 chars of body)
        preview = md_text[:300].strip()
        if len(md_text) > 300:
            preview += '…'

        now = datetime.datetime.utcnow()

        # Read existing front-matter to preserve crawl_date on recrawl
        old_meta = {}
        if os.path.exists(md_path):
            try:
                with open(md_path, 'r', encoding='utf-8') as f:
                    old_meta, _ = parse_frontmatter(f.read())
            except Exception:
                pass

        crawl_date = old_meta.get('crawl_date', now.isoformat())

        # Build front-matter
        meta = {
            'url': url,
            'domain': domain,
            'title': title or old_meta.get('title', ''),
            'preview': preview,
            'raw_hash': raw_hash,
            'crawl_date': str(crawl_date),
            'last_crawl_date': now.isoformat(),
        }
        frontmatter = build_frontmatter(meta)
        full_md = frontmatter + md_text

        # Write .md file
        with open(md_path, 'w', encoding='utf-8') as f:
            f.write(full_md)

        # Compute hash from the written .md file bytes
        file_hash = _blake2b(full_md.encode('utf-8'))

        rel_path = os.path.relpath(md_path, self.storage_dir)

        # ── Persist to DB ─────────────────────────────────────────────
        with app.app_context():
            existing = db_models.WebPage.query.filter_by(url=url).first()
            if existing:
                old_hash = existing.hash
                existing.hash = file_hash
                existing.hash_algorithm = HASH_ALGORITHM
                existing.file_path = rel_path
                existing.raw_hash = raw_hash
                # user_rating, model_rating, last_viewed are preserved
                # Invalidate model_rating if content actually changed
                if old_hash != file_hash:
                    existing.model_rating = None
                    existing.model_hash = None
                db_models.db.session.commit()
                page_id = existing.id
            else:
                page = db_models.WebPage(
                    hash=file_hash,
                    hash_algorithm=HASH_ALGORITHM,
                    file_path=rel_path,
                    url=url,
                    raw_hash=raw_hash,
                )
                db_models.db.session.add(page)
                db_models.db.session.commit()
                page_id = page.id

        return {
            'id': page_id,
            'url': url,
            'title': title,
            'file_path': rel_path,
            'hash': file_hash,
            '_links': same_domain_links,
        }
