
<center>

<img src="logo/logo.png" width="220">

</center>

Eagle-Search is a multithreaded web crawler written in Python. It discovers URLs, extracts links from textual resources, and stores page contents in SQLite databases.

It supports incremental crawling, historical indexing, compressed database archives, and an optional Bloom filter mode.

## Features

- Multithreaded.

- `robots.txt` support and `sitemap.xml` discovery.

- `HTML`, `XML`, `RSS`, `Atom`, `JSON`, `Markdown`, plain text, and `CSV` indexing.

- `SQLite` storage with automatic database rotation and `gzip` compression.

- Historical indexing mode: store new versions when page content changes.

- Two URL scheduling modes: SQLite frontier or Bloom filter.

- Persistent Bloom filters with separate input and output files.

## Usage

```bash
python3 src-search/crawler.py --help
```

Basic crawl:

```bash
python3 src-search/crawler.py --seed seeds/seed.csv
```

Continuous crawling with 8 threads and a 30-day reindexing interval:

```bash
python3 src-search/crawler.py \
    --seed seeds/seed.csv \
    --threads 8 \
    --delay 2 \
    --reindex-days 30 \
    --forever
```

Custom storage paths:

```bash
python3 src-search/crawler.py \
    --frontier-database database/frontier.db \
    --page-database database/pages_active.db \
    --archive database/archives
```

## Crawling modes

### SQLite frontier (default)

Discovered URLs are stored in `frontier.db`, along with their last-check timestamps and HTTP statuses.

The crawler selects URLs that have never been processed or whose `--reindex-days` interval has elapsed.

This mode provides persistent URL scheduling across runs.

### Bloom filter

Enable with `--bloom-mode`.

Instead of maintaining a SQLite frontier, the crawler uses:

- A Bloom filter to detect previously processed URLs.

- A FIFO queue (`deque`) for pending URLs for each thread to handle.

- Sets to prevent duplicate pending or concurrent requests.

The Bloom filter uses much less memory than storing every processed URL, but can produce false positives. A URL may therefore be skipped even if it has never been processed.

Bloom filters have a fixed size determined by their capacity and target false-positive rate. For example, a filter configured for 10 million URLs at a 1% false-positive rate uses approximately 11.4 MiB.

**Create a new filter:**

```bash
python3 src-search/crawler.py \
    --bloom-mode \
    --bloom-output-file database/bloom/filter.bin \
    --seed seeds/seed.csv
```

**Reuse an existing filter:**

```bash
python3 src-search/crawler.py \
    --bloom-mode \
    --bloom-input-file database/bloom/filter.bin \
    --bloom-output-file database/bloom/filter.bin \
    --seed seeds/seed.csv
```

**Load one filter and save to another:**

```bash
python3 src-search/crawler.py \
    --bloom-mode \
    --bloom-input-file old.bin \
    --bloom-output-file new.bin \
    --seed seeds/seed.csv
```

The filter is saved periodically and upon normal shutdown. Its contents are independent of `--reindex-days`.

## Storage and archives

Eagle-Search uses separate SQLite databases:

```text
database/
|-- frontier.db
|-- pages_active.db
|-- archives/
|   |-- pages_0000.db.gz
|   |-- pages_0001.db.gz
|── bloom/
    |-- filter.bin
```

- `frontier.db`: URL scheduling state (unused in Bloom mode).

- `pages_active.db`: indexed textual content.

- `archives/`: compressed snapshots of the pages database.

- `bloom/`: saved Bloom filters.

When the active pages database reaches its configured size limit (`MAX_PAGE_DB_SIZE`), Eagle-Search creates a compressed backup, then initializes a new active database.

With historical indexing enabled, modified documents generate additional records. Otherwise, the active database is intended to keep only their latest version.

## Main options

| Argument | Description |
|---|---|
| `--seed` | Seed URLs file |
| `--threads` | Number of worker threads |
| `--delay` | Minimum delay between requests to the same host |
| `--timeout` | Request timeout |
| `--max-bytes` | Maximum accepted response size |
| `--max-pages` | Maximum URL attempts in finite mode |
| `--forever` | Continuous crawling |
| `--reindex-days` | Reindex interval in SQLite frontier mode |
| `--historical` | Preserve changed document versions |
| `--always-recheck-robots` | Always refresh robots rules |
| `--no-always-recheck-robots` | Allow cached robots rules |
| `--recheck-robots-delay` | Robots cache lifetime |
| `--frontier-database` | Frontier SQLite path |
| `--page-database` | Active pages SQLite path |
| `--archive` | Compressed archive directory |
| `--frontier-min-free-gb` | Minimum free space for frontier storage |
| `--page-min-free-gb` | Minimum free space for pages storage |
| `--archive-min-free-gb` | Minimum free space for archive storage |
| `--bloom-mode` | Enable Bloom filter mode |
| `--bloom-input-file` | Load an existing Bloom filter |
| `--bloom-output-file` | Save Bloom filter to this path |

## Requirements

Python 3 and SQLite
