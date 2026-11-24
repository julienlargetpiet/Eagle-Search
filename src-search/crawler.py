#!/usr/bin/env python3
"""Small, resumable, polite web crawler backed by SQLite."""

import os
import gzip
import shutil
import tempfile
import argparse
import hashlib
import ipaddress
import re
import socket
import sqlite3
import time
import csv
import threading
import queue
import json
import xml.etree.ElementTree as ET

from bloom_filter import BloomFilter, load_bloom, save_bloom

from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from itertools import count

from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.robotparser import RobotFileParser

from pathlib import PurePosixPath, Path

from safe_transport import create_safe_opener

from collections import OrderedDict, deque

MAX_ROBOTS_CACHE = 1_000
MAX_SITEMAP_ORIGIN_CACHE = 1_000

USER_AGENT = "PersonalWebIndexer/0.1"  
ABSOLUTE_URL = re.compile(r'https?://[^\s<>"\'`]+', re.IGNORECASE)

NON_HTML_EXTENSIONS = {
    ".js", ".css", ".png", ".jpg", ".jpeg",
    ".gif", ".svg", ".ico", ".webp",
    ".pdf", ".zip", ".gz", ".mp4",
    ".woff", ".woff2", ".ttf"
}


INDEXABLE_TYPES = {
    "text/html",
    "text/plain",
    "text/markdown",
    "application/xhtml+xml",
    "application/xml",
    "text/xml",
    "application/json",
    "application/ld+json",
    "application/rss+xml",
    "application/atom+xml",
    "text/csv",
    "application/csv"
}
 
CSV_TYPES = {"text/csv", "application/csv"}

SITEMAP_NAMESPACE = "http://www.sitemaps.org/schemas/sitemap/0.9"

MARKDOWN_LINK = re.compile(r'!?\[[^\]]*\]\(\s*(<[^>]+>|[^\s)]+)')


GIB = 1024 ** 3

MAX_PAGE_DB_SIZE = 1 * GIB

MIN_FRONTIER_FREE_GB = 1
MIN_PAGE_FREE_GB = MAX_PAGE_DB_SIZE / GIB
MIN_ARCHIVE_FREE_GB = 2 * MAX_PAGE_DB_SIZE / GIB

MAX_PENDING_URLS = 100_000

BLOOM_SAVE_INTERVAL = 300


def check_disk_space(path, min_free_gb):

    free = shutil.disk_usage(path).free

    if free < min_free_gb * GIB:

        print(
            f"[DISK FULL] Only { free / GIB:.2f } GiB"
            f"available on the filesystem containing {path}"
        )

        return False

    return True

def extract_csv_links(content):

    urls = set()

    try:
        dialect = csv.Sniffer().sniff(
            content[:8192],
            delimiters=",;\t|"
        )
    except csv.Error:
        dialect = csv.excel

    reader = csv.reader(
        content.splitlines(),
        dialect=dialect
    )

    for row in reader:

        for cell in row:

            for match in ABSOLUTE_URL.findall(cell):

                url = normalize(match.rstrip(".,;!)]}"))

                if url is not None:
                    urls.add(url)

    return urls


def extract_text_links(content):

    urls = set()

    for match in ABSOLUTE_URL.findall(content):

        url = normalize(match.rstrip(".,;!)]}"))

        if url is not None:
            urls.add(url)

    return urls

def extract_markdown_links(content, base_url):

    urls = extract_text_links(content)

    for match in MARKDOWN_LINK.finditer(content):

        raw = match.group(1).strip("<>")

        url = normalize(raw, base_url)

        if url is not None:
            urls.add(url)

    return urls


def xml_handle_links(content):

    root = ET.fromstring(content)

    namespace = f"{{{SITEMAP_NAMESPACE}}}"

    if root.tag in (
        namespace + "urlset",
        namespace + "sitemapindex"
    ):
        return extract_sitemap_links(root)
    
    
    return extract_xml_links(root)

def extract_sitemap_links(root):

    namespace = f"{{{SITEMAP_NAMESPACE}}}"

    # we precise the namespace twice because ElementTree 
    # does not assume the child namespace from its parent

    if root.tag == namespace + "urlset":

        path = "./sm:url/sm:loc"

    elif root.tag == namespace + "sitemapindex":

        path = "./sm:sitemap/sm:loc"

    else:

        return set()

    urls = set()

    for element in root.findall(
        path,
        namespaces={"sm": SITEMAP_NAMESPACE}
    ):

        if element.text:

            url = normalize(element.text.strip())

            if url is not None:
                urls.add(url)

    return urls


def default_sitemap(url):

    parsed = urlsplit(url)

    origin = f"{parsed.scheme}://{parsed.netloc}"

    return origin + "/sitemap.xml"

def likely_indexable_content(url):

    path = urlsplit(url).path.lower()
    extension = PurePosixPath(path).suffix

    return extension not in NON_HTML_EXTENSIONS

def indexable_type(content_type):

    return (
        content_type in INDEXABLE_TYPES
        or content_type.endswith("+json")
        or content_type.endswith("+xml")
    )

def read_seeds(paths):

    for path in paths:

        with open(path, "r", encoding="utf-8-sig", newline="") as file:

            reader = csv.DictReader(file)

            if not reader.fieldnames or "url" not in reader.fieldnames:
                raise ValueError(f"{path}: missing 'url' column")

            for row in reader:

                url = row["url"]

                if url:
                    yield url.strip()


def now():
    return int(time.time())

def extract_json_links(content):

    urls = set()

    data = json.loads(content)

    def walk(value):

        if isinstance(value, dict):

            for item in value.values():
                walk(item)

        elif isinstance(value, list):

            for item in value:
                walk(item)

        elif isinstance(value, str):

            for match in ABSOLUTE_URL.findall(value):

                url = normalize(match.rstrip(".,;!)]}"))

                if url:
                    urls.add(url)

    walk(data)

    return urls

def extract_xml_links(root):

    urls = set()

    for element in root.iter():

        if element.text:

            for match in ABSOLUTE_URL.findall(element.text):

                url = normalize(match.rstrip(".,;!)]}"))

                if url:
                    urls.add(url)

        # URLs inside XML attributes.
        for value in element.attrib.values():

            for match in ABSOLUTE_URL.findall(value):

                url = normalize(match.rstrip(".,;!)]}"))

                if url:
                    urls.add(url)

    return urls

# URL parts:
# https://john:secret123@example.com:8080/articles/?q=1#findings
# scheme-username-password-host-port-path-query-fragment
# host+port=netloc or authority

def normalize(raw, base=None):
    try:

        candidate = urljoin(base, raw.strip()) if base else raw.strip()

        parts = urlsplit(candidate)

        if (parts.scheme.lower() not in ("http", "https") 
            or not parts.hostname
        ):
            return None

        if (parts.username is not None 
            or parts.password is not None
        ):
            return None

        host = parts.hostname.encode("idna").decode("ascii").lower()

        port = parts.port

        scheme = parts.scheme.lower()

        if port in (80 if scheme == "http" else 443, None):

            authority = f"[{host}]" if ":" in host else host

        else:

            authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"

        path = parts.path or "/"

        return urlunsplit((scheme, 
                           authority, 
                           path, 
                           parts.query, 
                           ""))

    except (ValueError, UnicodeError):
        return None

def fetch(url, timeout, max_bytes, opener):

    req = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": (
                "text/html, application/xhtml+xml, "
                "application/xml, application/json, "
                "text/markdown, text/plain, text/csv, application/csv"
            )
        }
    )

    try:
        with opener.open(req, timeout=timeout) as response:

            status = response.status
            content_type = response.headers.get_content_type()
            final_url = normalize(response.url)

            if not indexable_type(content_type):
                return status, None, final_url, content_type

            raw = response.read(max_bytes + 1)

            if len(raw) > max_bytes:
                raise ValueError(
                    f"Content exceeds --max-bytes ({max_bytes})"
                )

            encoding = (
                response.headers.get_content_charset()
                or "utf-8"
            )

            try:
                content = raw.decode(
                    encoding,
                    errors="replace"
                )
            except LookupError:
                content = raw.decode(
                    "utf-8",
                    errors="replace"
                )

            return status, content, final_url, content_type

    except HTTPError as exc:
        return exc.code, None, url, None



class Links(HTMLParser):

    def __init__(self, base):

        super().__init__(convert_charrefs=True)
        self.base = base
        self.urls = set()

    def handle_starttag(self, tag, attrs):

        attributes = dict(attrs)

        # The <base> tag specifies the base URL used to resolve relative hyperlinks within an HTML document.

        if tag == "base" and attributes.get("href"):

            candidate = normalize(attributes["href"], self.base)

            if candidate:
                self.base = candidate

        elif tag in ("a", "area", "link") and attributes.get("href"):

            url = normalize(attributes["href"], self.base)

            if url:
                self.urls.add(url)


def extract_links(html, url):

    parser = Links(url)
    parser.feed(html)

    for match in ABSOLUTE_URL.findall(html):

        candidate = normalize(match.rstrip(".,;!)]}"))

        if candidate:
            parser.urls.add(candidate)

    return parser.urls

def database_size(db):

    page_count = db.execute(
        "PRAGMA pages_db.page_count"
    ).fetchone()[0]

    page_size = db.execute(
        "PRAGMA pages_db.page_size"
    ).fetchone()[0]

    return page_count * page_size

def init_pages_db(db):

    db.execute("""
        CREATE TABLE IF NOT EXISTS pages_db.pages (
            id INTEGER PRIMARY KEY,
            url TEXT NOT NULL,
            content TEXT NOT NULL,
            content_type TEXT NOT NULL,
            indexed_at INTEGER NOT NULL DEFAULT (unixepoch())
        )
    """)

    db.execute("""
        CREATE INDEX IF NOT EXISTS pages_db.idx_pages_url_time
        ON pages(url, indexed_at DESC)
    """)

def init_db(db, pages_database):

    pending_path = Path(str(pages_database) + ".rotation-pending")
    
    if pending_path.exists():
        raise RuntimeError(
            f"Interrupted rotation detected: {pending_path}. "
            "Recover the previous database before restarting."
        )

    db.execute(
        "ATTACH DATABASE ? AS pages_db",
        (pages_database,)
    )

    # Enable WAL separately for both databases.
    db.execute("PRAGMA main.journal_mode=WAL;")
    db.execute("PRAGMA pages_db.journal_mode=WAL;")

    db.executescript("""

        CREATE TABLE IF NOT EXISTS pages_db.pages (
            id INTEGER PRIMARY KEY,
            url TEXT NOT NULL,
            content TEXT NOT NULL,
            content_type TEXT NOT NULL,
            indexed_at INTEGER NOT NULL DEFAULT (unixepoch())
        );

        CREATE INDEX IF NOT EXISTS pages_db.idx_pages_url_time
        ON pages(url, indexed_at DESC);

        CREATE TABLE IF NOT EXISTS main.frontier (
            url TEXT PRIMARY KEY,
            last_checked_at INTEGER,
            http_status INTEGER
        );

        CREATE INDEX IF NOT EXISTS main.idx_frontier_last_checked
        ON frontier(last_checked_at);

    """)


def rotate_if_needed(db, 
                     page_database, 
                     archive
):

    size = database_size(db)

    if size < MAX_PAGE_DB_SIZE:
        return False

    page_path = Path(page_database)

    archive_dir = Path(archive)
    archive_dir.mkdir(parents=True, exist_ok=True)

    pending_path = Path(str(page_path) + ".rotation-pending")

    # Never silently discard a database left by an
    # interrupted previous rotation.
    if pending_path.exists():
        raise RuntimeError(
            f"Unresolved rotation detected: {pending_path}"
        )

    db.commit()

    # Find the first available archive number.
    number = 0

    while True:

        archive_path = archive_dir / f"pages_{number:04d}.db.gz"

        if not archive_path.exists():
            break

        number += 1

    print(
        f"[ROTATION] Page database reached "
        f"{size / 1024**3:.2f} GiB",
        flush=True
    )

    # ---------------------------------------------
    # Step 1: Create and compress a SQLite snapshot
    # ---------------------------------------------

    with tempfile.TemporaryDirectory(
        dir=archive_dir
    ) as temporary_dir:

        snapshot_path = Path(temporary_dir) / "snapshot.db"
        compressed_path = Path(temporary_dir) / "snapshot.db.gz"

        snapshot_db = sqlite3.connect(snapshot_path)

        try:
            db.backup(snapshot_db, name="pages_db")
        finally:
            snapshot_db.close()

        with open(snapshot_path, "rb") as source:
            with gzip.open(
                compressed_path,
                "wb",
                compresslevel=6
            ) as target:

                shutil.copyfileobj(
                    source,
                    target,
                    length=1024 * 1024
                )

        # Verify that the compressed file can be read.
        # Compare its decompressed bytes with the snapshot.
        with open(snapshot_path, "rb") as original:
            with gzip.open(compressed_path, "rb") as archived:

                while True:

                    expected = original.read(1024 * 1024)
                    actual = archived.read(1024 * 1024)

                    if expected != actual:
                        raise RuntimeError(
                            "Archive verification failed"
                        )

                    if not expected:
                        break

        # Publish atomically without overwriting an
        # archive that might already exist.
        os.link(compressed_path, archive_path)

    print(f"[ROTATION] Archived: {archive_path}", flush=True)

    # ---------------------------------------------
    # Step 2: Replace the active SQLite database
    # ---------------------------------------------

    moved = False
    attached_new = False

    try:

        # Important: DETACH closes the attachment and
        # allows SQLite to finish its WAL handling.
        db.execute("DETACH DATABASE pages_db")

        # Preserve the old active file until the new
        # database has been initialized successfully.
        os.replace(page_path, pending_path)
        moved = True

        # SQLite automatically creates a new database file when we 
        # attach a nonexistent filename, provided its parent directory exists.

        db.execute(
            "ATTACH DATABASE ? AS pages_db",
            (str(page_path),)
        )
        attached_new = True

        db.execute("PRAGMA pages_db.journal_mode=WAL")

        init_pages_db(db)
        db.commit()

        # New active database is ready.
        pending_path.unlink()

    except Exception:

        # Best-effort recovery of the previous active DB.
        if moved:

            if attached_new:
                db.rollback()
                db.execute("DETACH DATABASE pages_db")

            if page_path.exists():
                page_path.unlink()

            os.replace(pending_path, page_path)

            db.execute(
                "ATTACH DATABASE ? AS pages_db",
                (str(page_path),)
            )

        raise

    print(
        f"[ROTATION] New active database: {page_path}",
        flush=True
    )

    return True



def add_urls(db, urls):
    db.executemany(
        "INSERT OR IGNORE INTO frontier(url) VALUES (?)",
        (
            (normalized,)
            for raw in urls
            if (normalized := normalize(raw)) is not None
            and likely_indexable_content(normalized)
        )
    )


def robots_allowed(url,
                   timeout,
                   cache,
                   recheck_robots,
                   recheck_robots_delay,
                   opener
):

    parsed = urlsplit(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    cached = cache.get(origin)

    must_recheck = (
        recheck_robots
        or cached is None
        or time.monotonic() - cached[1] >= recheck_robots_delay
    )

    if cached is not None and not must_recheck:
        cache.move_to_end(origin)

    if must_recheck:

        robots_url = origin + "/robots.txt"

        req = Request(
            robots_url,
            headers={"User-Agent": USER_AGENT}
        )

        req._is_robot_request = True

        try:
            try:
                with opener.open(req, timeout=timeout) as response:

                    lines = response.read(512 * 1024 + 1)

                    if len(lines) > 512 * 1024:
                        return False

                    text = lines.decode("utf-8", errors="replace")

            except HTTPError as exc:

                if exc.code in (404, 410):
                    text = ""
                else:
                    return False

            parser = RobotFileParser()
            parser.parse(text.splitlines())

            cache[origin] = (parser, time.monotonic())

            cache.move_to_end(origin)

            if len(cache) > MAX_ROBOTS_CACHE:
                cache.popitem(last=False)

        except (URLError, OSError, ValueError):
            return False

    return cache[origin][0].can_fetch(USER_AGENT, url)


def enqueue_bloom_url(url, 
                      bloom, 
                      pending_urls, 
                      pending_set, 
                      in_flight
):

    url = normalize(url)

    if url is None:
        return

    if url in pending_set:
        return

    if url in in_flight:
        return

    if url in bloom:
        return

    if len(pending_urls) >= MAX_PENDING_URLS:
        return

    pending_urls.append(url)
    pending_set.add(url)

def crawl(frontier_database,
          page_database,
          seed,
          max_pages,
          timeout,
          max_bytes,
          reindex_days,
          recheck_robots,
          recheck_robots_delay,
          opener,
          robot_cache_for_thread,
          forever,
          threads,
          historical,
          archive,
          archive_min_free_gb,
          frontier_min_free_gb,
          page_min_free_gb,
          bloom_mode,
          bloom_input_file,
          bloom_output_file
):

    last_bloom_save = time.monotonic()


    task_queue = queue.Queue(maxsize=threads)
    result_queue = queue.Queue()

    in_flight = set()
    submitted = 0

    sitemap_checked_origins = OrderedDict()

    pending_urls = deque()
    pending_set = set()

    bloom = None

    if bloom_mode:
    
        if bloom_input_file:
    
            bloom = load_bloom(bloom_input_file)
    
            print(
                f"[BLOOM] Loaded from {bloom_input_file}",
                flush=True
            )
    
        else:
    
            bloom = BloomFilter(
                capacity=10_000_000,
                error_rate=0.01
            )
    
            print("[BLOOM] Created new filter", flush=True)
    

    # --------------------------------------------------
    # Worker: network operations and HTML parsing only
    # --------------------------------------------------

    def worker():

        while True:

            url = task_queue.get()

            try:

                if url is None:
                    return

                status = None
                content = None
                content_type = None
                final_url = None
                links = set()
                skipped = False
                error = None

                try:

                    allowed = robots_allowed(
                        url,
                        timeout,
                        robot_cache_for_thread(),
                        recheck_robots,
                        recheck_robots_delay,
                        opener
                    )

                    if not allowed:

                        skipped = True

                    else:

                        status, content, final_url, content_type = fetch(
                            url,
                            timeout,
                            max_bytes,
                            opener
                        )

                        if (status == 200 and content is not None):

                            if content_type in ("text/html", "application/xhtml+xml"):
                            
                                links = extract_links(content, final_url or url)
                            
                            elif content_type in ("application/xml", "text/xml") or content_type.endswith("+xml"):
                            
                                links = xml_handle_links(content)
                            
                            elif content_type == "application/json" or content_type.endswith("+json"):
                            
                                links = extract_json_links(content)

                            elif content_type == "text/markdown":
                            
                                links = extract_markdown_links(content, final_url or url)
                            
                            elif content_type == "text/plain":
                            
                                links = extract_text_links(content)

                            elif content_type in CSV_TYPES:
                            
                                links = extract_csv_links(content)


                except Exception as exc:

                    error = exc

                result_queue.put((
                    url,
                    status,
                    content,
                    content_type,
                    final_url,
                    links,
                    skipped,
                    error
                ))

            finally:

                task_queue.task_done()

    # --------------------------------------------------
    # Main thread: SQLite scheduling and writing
    # --------------------------------------------------

    with sqlite3.connect(
        ":memory:" if bloom_mode else frontier_database
    ) as db:
    
        if bloom_mode:
    
            db.execute(
                "ATTACH DATABASE ? AS pages_db",
                (page_database,)
            )
    
            db.execute("PRAGMA pages_db.journal_mode=WAL")
    
            init_pages_db(db)
    
        else:
    
            init_db(db, page_database)

        def schedule_urls(urls):
        
            if bloom_mode:
        
                for url in urls:
        
                    enqueue_bloom_url(
                        url,
                        bloom,
                        pending_urls,
                        pending_set,
                        in_flight
                    )
        
            else:
        
                add_urls(db, urls)

        schedule_urls(read_seeds(seed))
        db.commit()

        workers = []

        for _ in range(threads):

            thread = threading.Thread(
                target=worker,
                daemon=True
            )

            thread.start()
            workers.append(thread)

        try:

            while True:

                # --------------------------------------
                # Dispatch eligible URLs
                # --------------------------------------

                if not bloom_mode:
                
                    if not check_disk_space(
                        Path(frontier_database).parent,
                        frontier_min_free_gb
                    ):
                        break

                if not check_disk_space(
                    Path(page_database).parent,
                    page_min_free_gb
                ):
                    break
                
                if not check_disk_space(
                    archive,
                    archive_min_free_gb
                ):
                    break


                while (
                    len(in_flight) < threads
                    and (forever or submitted < max_pages)
                ):
                
                    if bloom_mode:
                
                        if not pending_urls:
                            break
                
                        url = pending_urls.popleft()
                        pending_set.remove(url)
                
                    else:
                
                        cutoff = now() - reindex_days * 86400
                
                        rows = db.execute("""
                            SELECT url
                            FROM frontier
                            WHERE last_checked_at IS NULL
                               OR last_checked_at <= ?
                            ORDER BY
                                (last_checked_at IS NOT NULL),
                                last_checked_at ASC,
                                url
                            LIMIT ?
                        """, (cutoff, threads + 1)).fetchall()
                
                        url = next(
                            (
                                row[0]
                                for row in rows
                                if row[0] not in in_flight
                            ),
                            None
                        )
                
                        if url is None:
                            break
                
                    print(f"[DISPATCH] {url}", flush=True)
                
                    in_flight.add(url)
                
                    task_queue.put(url)
                
                    submitted += 1



                # --------------------------------------
                # Stop once finite work is exhausted
                # --------------------------------------

                if not in_flight:

                    if not forever and submitted >= max_pages:

                        print(
                            f"[STOP] Reached --max-pages: {submitted}/{max_pages}",
                            flush=True
                        )

                        break

                    if bloom_mode:
                    
                        print("[STOP] Bloom queue is empty.", flush=True)
                        break

                    if not forever:
                        print("No URLs currently due for indexing.")
                        break

                    # No URLs eligible right now.
                    # Wait for the oldest one to become due.

                    next_check = db.execute("""
                        SELECT MIN(last_checked_at)
                        FROM frontier
                        WHERE last_checked_at IS NOT NULL
                    """).fetchone()[0]

                    if next_check is None:
                        print("No URLs in the frontier.")
                        break

                    next_due = next_check + reindex_days * 86400

                    remaining = max(
                        0,
                        next_due - time.time()
                    )

                    sleep_seconds = max(
                        1,
                        remaining
                    )

                    print(
                        f"No URLs due. "
                        f"Next eligible in {remaining:.0f}s."
                    )

                    time.sleep(sleep_seconds)

                    continue

                # --------------------------------------
                # Receive a completed worker result
                # --------------------------------------

                print(
                    f"[WAITING] {len(in_flight)} URLs in flight",
                    flush=True
                )

                (
                    url,
                    status,
                    content,
                    content_type,
                    final_url,
                    links,
                    skipped,
                    error
                ) = result_queue.get()

                try:

                    page_inserted = False

                    if error is not None:

                        print(f"[ERROR] {url}: {error}")

                    elif skipped:

                        print(
                            f"[SKIP robots.txt / non-public] {url}"
                        )

                    elif status == 200 and content is not None:

                        any_changed = False

                        for page_url in {url, final_url}:

                            if page_url is None:
                                continue

                            old = db.execute("""
                                SELECT content, content_type
                                FROM pages
                                WHERE url = ?
                                ORDER BY indexed_at DESC, id DESC
                                LIMIT 1
                            """, (page_url,)).fetchone()

                            changed = (
                                old is None
                                or old[0] != content
                                or old[1] != content_type
                            )

                            if changed:

                                db.execute("""
                                    INSERT INTO pages(
                                        url,
                                        content,
                                        content_type,
                                        indexed_at
                                    )
                                    VALUES (?, ?, ?, ?)
                                """, (
                                    page_url,
                                    content,
                                    content_type,
                                    now()
                                ))

                                page_inserted = True
                                any_changed = True

                        schedule_urls(links)
                        
                        if final_url:
                            schedule_urls([final_url])


                        print(
                            f"[200 {'NEW' if any_changed else 'SAME'}] "
                            f"{url} | type={content_type} | found {len(links)} links"
                        )

                    elif status == 200:

                        print(f"[{status} / unsupported content] {url}")

                    else:
                        
                        print(f"[HTTP {status}] {url}")

                finally:
               
                    try:
                
                        # Discover the default sitemap once per origin.
                
                        origin_parts = urlsplit(url)
                        origin = f"{origin_parts.scheme}://{origin_parts.netloc}"
               
                        if origin in sitemap_checked_origins:
                        
                            sitemap_checked_origins.move_to_end(origin)
                        
                        else:
                        
                            sitemap_checked_origins[origin] = None
                        
                            if len(sitemap_checked_origins) > MAX_SITEMAP_ORIGIN_CACHE:
                                sitemap_checked_origins.popitem(last=False)
                        
                            schedule_urls([default_sitemap(url)])

                        # Non-historical mode: remove unavailable resources
                        # and their previously stored snapshots.
               
                        if not historical and status in (404, 410):
                        
                            db.execute("""
                                DELETE FROM pages
                                WHERE url = ?
                            """, (url,))
                        
                            if not bloom_mode:
                        
                                db.execute("""
                                    DELETE FROM frontier
                                    WHERE url = ?
                                """, (url,))
                       
                        elif not bloom_mode:
                        
                            db.execute("""
                                UPDATE frontier
                                SET last_checked_at = ?,
                                    http_status = ?
                                WHERE url = ?
                            """, (
                                now(),
                                status,
                                url
                            ))

                        db.commit()

                        if page_inserted:
                            rotate_if_needed(db, 
                                             page_database, 
                                             archive
                            )
               
                        if bloom_mode:
                            bloom.add(url)

                            if time.monotonic() - last_bloom_save >= BLOOM_SAVE_INTERVAL:

                                save_bloom(bloom, bloom_output_file)
                                last_bloom_save = time.monotonic()

                    finally:
                
                        in_flight.remove(url)
                        result_queue.task_done()

        finally:

            if bloom_mode:
                save_bloom(bloom, bloom_output_file)

                print(
                    f"[BLOOM] Saved: {bloom_output_file}",
                    flush=True
                )


            # Tell workers to exit after their current task.

            for _ in workers:
                task_queue.put(None)

            for thread in workers:
                thread.join()

def main():

    parser = argparse.ArgumentParser(description="Incremental historical web crawler")

    parser.add_argument(
        "--frontier-database", 
        default="database/frontier.db"
    )

    parser.add_argument(
        "--page-database", 
        default="database/pages_active.db"
    )

    parser.add_argument(
        "--archive", 
        default="database/archives"
    )

    parser.add_argument(
        "--seed",
        action="append",
        default=[],
        help="CSV file containing seed URLs (repeatable)"
    )

    parser.add_argument(
        "--reindex-days", 
        type=int, 
        default=30
    )

    parser.add_argument(
        "--always-recheck-robots",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Recheck robots.txt instead of using the cached version"
    )

    parser.add_argument(
        "--recheck-robots-delay",
        type=float,
        default=86400,
        help="Seconds before rechecking a cached robots.txt"
    )

    parser.add_argument(
        "--max-pages", 
        type=int, 
        default=100, 
        help="Max crawl attempts this run"
    )

    parser.add_argument(
        "--delay", 
        type=float, 
        default=1.0, 
        help="Minimum seconds between requests to one host"
    )

    parser.add_argument(
        "--timeout", 
        type=float, 
        default=15
    )

    parser.add_argument(
        "--max-bytes", 
        type=int, 
        default=2_000_000
    )

    parser.add_argument(
        "--frontier-min-free-gb", 
        type=float, 
        default=10,
        help="Stop crawling if available disk space fails below this threshold (GiB) for where is located frontier database"
    )

    parser.add_argument(
        "--page-min-free-gb", 
        type=float, 
        default=10,
        help="Stop crawling if available disk space fails below this threshold (GiB) for where is located page database"
    )

    parser.add_argument(
        "--archive-min-free-gb", 
        type=float, 
        default=10,
        help="Stop crawling if available disk space fails below this threshold (GiB) for where is located the archives"
    )

    parser.add_argument(
        "--forever",
        action=argparse.BooleanOptionalAction,
        default=False,
    )

    parser.add_argument(
        "--bloom-mode",
        action=argparse.BooleanOptionalAction,
        default=False,
    )

    parser.add_argument(
        "--historical",
        action=argparse.BooleanOptionalAction,
        default=True,
    )

    parser.add_argument(
        "--threads",
        type=int,
        default=8,
        help="Number of concurrent crawler workers"
    )

    parser.add_argument(
        "--bloom-input-file",
        type=str,
        default=None
    )

    parser.add_argument(
        "--bloom-output-file",
        type=str,
        default=None
    )


    args = parser.parse_args()

    if (args.reindex_days < 0
        or args.max_pages < 1
        or args.delay < 0
        or args.timeout <= 0
        or args.max_bytes < 1
        or args.recheck_robots_delay < 0
        or args.threads < 1
    ):
        parser.error("Invalid crawler configuration")
   
    if (
        args.frontier_min_free_gb < MIN_FRONTIER_FREE_GB
        or args.page_min_free_gb < MIN_PAGE_FREE_GB
        or args.archive_min_free_gb < MIN_ARCHIVE_FREE_GB
    ):
        parser.error("Insufficient minimum disk-space thresholds")

    if args.forever and args.reindex_days == 0:
        parser.error("--forever requires --reindex-days > 0")

    if args.bloom_mode and args.frontier_database:
        print("[NOTE] --bloom-mode supposes won't use the frontier database")


    if args.bloom_mode and not args.bloom_output_file:
        parser.error(
            "--bloom-mode requires --bloom-output-file"
        )

    # Each worker gets its own robots.txt cache.

    thread_state = threading.local()

    def robot_cache_for_thread():

        if not hasattr(thread_state, "robots_cache"):
            thread_state.robots_cache = OrderedDict()

        return thread_state.robots_cache

    opener = None

    def check_redirect_robots(url):

        return robots_allowed(
            url,
            args.timeout,
            robot_cache_for_thread(),
            args.always_recheck_robots,
            args.recheck_robots_delay,
            opener
        )

    opener = create_safe_opener(
        args.delay,
        check_redirect_robots
    )

    crawl(
        args.frontier_database,
        args.page_database,
        args.seed,
        args.max_pages,
        args.timeout,
        args.max_bytes,
        args.reindex_days,
        args.always_recheck_robots,
        args.recheck_robots_delay,
        opener,
        robot_cache_for_thread,
        args.forever,
        args.threads,
        args.historical,
        args.archive,
        args.archive_min_free_gb,
        args.frontier_min_free_gb,
        args.page_min_free_gb,
        args.bloom_mode,
        args.bloom_input_file,
        args.bloom_output_file
    )

if __name__ == "__main__":
    main()



