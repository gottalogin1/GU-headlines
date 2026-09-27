# GU Headlines

Headlines pertaining to the island in one easy source.

A self-hosted Guam news aggregator. Every hour it checks Guam's newsrooms and
stores each **new** story with its headline, main image and opening
paragraph(s). Stories are tagged **Local**, **Military**, **Business** and
**Labor**, and everything is kept in PostgreSQL with full-text search, so an
article from three years ago is as easy to find as today's.

![Front page (sample data)](docs/screenshot.jpg)

## Features

- **Hourly, incremental scraping.** Feeds are fetched with conditional
  requests (an unchanged feed returns `304` and costs nothing). Every
  discovered link is checked against the database first, so only article
  pages that have never been stored are downloaded.
- **Headline, main image and intro** for every story. Main images are resized
  to WebP and kept locally, so old stories keep their pictures after the
  publisher reorganizes its site.
- **Topics:** keyword rules in [`config/categories.yaml`](config/categories.yaml)
  (Local, Military, Business, Labor), plus the site's own sections and tags.
  A story can have several topics.
- **Search built for old articles:** PostgreSQL full-text search with stemming
  (`workers` finds `worker`), accent folding (`Hagatna` finds `Hagåtña`),
  `"exact phrases"`, `or`, `-exclusions`, filters for source, topic and
  date range, and a fuzzy headline fallback for typos.
- **Archive** by year → month → day, in Chamorro Standard Time.
- **Built to last:** PostgreSQL with versioned migrations (Alembic), pinned
  dependencies, Docker Compose, a backup script, a `/status` page per source
  and a `/healthz` endpoint for monitoring.
- **Polite:** identifies itself, honours `robots.txt`, waits between requests to the
  same site, and backs off from pages that fail.

## Sources

Configured in [`config/sources.yaml`](config/sources.yaml):

| Source | How it is read | Default topic |
|---|---|---|
| The Guam Daily Post (postguam.com) | RSS + section pages | by keywords |
| Pacific Daily News (guampdn.com) | section pages + advertised feeds | by keywords |
| KUAM News (kuam.com) | news pages + advertised feeds | by keywords |
| PNC News First (pncguam.com) | RSS + front page | by keywords |
| KANDIT News Group (kanditnews.com) | RSS + front page | by keywords |
| Marianas Business Journal (mbjguam.com) | RSS + section pages | Business |
| Pacific Island Times | RSS + front page, Guam stories only | by keywords |
| Marianas Variety | RSS, Guam stories only | by keywords |
| Stars and Stripes (Asia-Pacific) | RSS, Guam stories only | Military |
| DVIDS (Joint Region Marianas, Naval Base Guam, Camp Blaz) | unit pages + feeds | Military |
| Andersen Air Force Base | news page | Military |
| Guam Department of Labor | RSS | Labor |
| Guam Business Magazine | RSS + front page | Business |
| Office of the Governor (off by default) | RSS + press releases | by keywords |

News sites change their layouts. After you deploy, run `guheadlines check`
(below) on each source; if one finds nothing, adjust its feed URL or
`article_pattern` in `sources.yaml`. Edits take effect on the next hourly run
without a restart.

## Quick start (Docker)

Requirements: a Linux server with Docker and the Compose plugin.

```sh
git clone https://github.com/gottalogin1/GU-headlines.git
cd GU-headlines
cp .env.example .env
# edit .env: set POSTGRES_PASSWORD (and optionally WEB_PORT, SCRAPER_USER_AGENT)
docker compose up -d --build
```

Open `http://<your-server>:8000`. The worker starts scraping right away and
then runs at the top of every hour. Watch it with:

```sh
docker compose logs -f worker
```

Three containers run:

| Service | What it does |
|---|---|
| `db` | PostgreSQL 16; data in the `pgdata` volume |
| `web` | the website (FastAPI + server-rendered HTML) on port 8000 |
| `worker` | the hourly scraper; images in the `media` volume |

Schema migrations run automatically when `web` or `worker` starts.

### Check a source (dry run)

Shows what a source's feeds and pages give today and what would be stored,
without saving anything:

```sh
docker compose exec worker guheadlines check --source postguam --limit 3
```

Example output:

```
== The Guam Daily Post (postguam)
   discovered 48 article links {'feed': 40, 'listing': 8}
   12 not yet in the database

-- https://www.postguam.com/news/local/.../article_....html  (via feed)
   title:      ...
   published:  2026-09-27 01:10 UTC
   categories: military
   image:      https://bloximages....jpg
   intro:      HAGÅTÑA — ...
```

### Other commands

```sh
docker compose exec worker guheadlines scrape                   # run all sources now
docker compose exec worker guheadlines scrape --source kuam     # one source now
docker compose exec worker guheadlines sources                  # list sources and counts
docker compose exec worker guheadlines reclassify               # re-apply categories.yaml
docker compose exec worker guheadlines backfill --source pnc --since 2025-01-01 --limit 500
```

`backfill` imports older stories from a site's XML sitemaps (found through
`robots.txt`, or set `sitemaps:` on the source), so the archive can start
before the day you installed it.

## How the hourly update works

For each enabled source, each run:

1. **Discover.** Fetch the configured RSS/Atom feeds and section pages. Feeds
   send `If-None-Match` / `If-Modified-Since`; a `304 Not Modified` or an
   identical body is skipped without parsing.
2. **Skip what is known.** Every link is normalized (tracking parameters,
   `www`, trailing slashes and AMP variants removed) and looked up in
   `articles`. Pages already stored, already rejected (not an article, not
   about Guam), or waiting for a retry are not fetched.
3. **Fetch only new pages** (at most `MAX_NEW_PER_SOURCE` per run, newest
   first). From each page it extracts the headline, canonical URL, main
   image, publish time, byline, section/tags and the first paragraph(s),
   using OpenGraph/JSON-LD metadata, common CMS layouts (WordPress, BLOX,
   Drupal, Wix, DVIDS, af.mil) and [trafilatura](https://trafilatura.readthedocs.io)
   as a fallback.
4. **Tag and store.** Topics are assigned, the image is saved as WebP, and the
   story is inserted. Failed pages are retried with exponential backoff (1h,
   2h, 4h, 8h); if a site blocks article pages but publishes a feed, the
   feed's headline and summary are used.

Only one scrape runs at a time (PostgreSQL advisory lock), even if you start
one by hand while the worker is running.

## Search

The search page (`/search`) searches headlines, intros, bylines and the site's
own tags of every stored story:

| You type | It finds |
|---|---|
| `buildup contract` | stories with both words (any form: *contracts*, *contracting*) |
| `"minimum wage"` | the exact phrase |
| `typhoon or storm` | either word |
| `buildup -Okinawa` | *buildup* but not *Okinawa* |
| `Hagatna` | *Hagåtña* too (accents are ignored) |
| *(no words)* + dates | everything from that period |

Results can be filtered by source, topic and date range, and sorted by best
match (slightly favouring recent stories), newest or oldest. If nothing
matches exactly, headlines with similar spelling are shown.

## Configuration

Settings are environment variables in `.env` (see [`.env.example`](.env.example)):

| Variable | Default | Meaning |
|---|---|---|
| `POSTGRES_PASSWORD` | *(required)* | database password |
| `WEB_PORT` | `8000` | port the website is published on |
| `SCRAPE_INTERVAL_MINUTES` | `60` | how often to scrape (aligned to the clock) |
| `SCRAPE_ON_START` | `true` | also scrape when the worker starts |
| `MAX_NEW_PER_SOURCE` | `40` | new pages fetched per source per run |
| `PER_HOST_DELAY` | `1.5` | seconds between requests to one site |
| `SCRAPER_WORKERS` | `4` | sources scraped in parallel |
| `RESPECT_ROBOTS_TXT` | `true` | honour robots.txt |
| `SCRAPER_USER_AGENT` | GUHeadlinesBot | put a contact URL/email here |
| `STORE_IMAGES` | `true` | keep a local WebP copy of each main image |
| `IMAGE_MAX_WIDTH` | `720` | width of stored images |
| `PAGE_SIZE` | `30` | stories per page |
| `SITE_NAME`, `SITE_TAGLINE` | GU Headlines | shown in the header |

### Adding or fixing a source

Add an entry to `config/sources.yaml`:

```yaml
  - slug: mynews                  # lowercase, used in URLs
    name: My News Site
    homepage: https://www.mynews.com/
    feeds:
      - https://www.mynews.com/feed/
    listing_pages:                # optional: section pages to scan for links
      - https://www.mynews.com/local/
    article_pattern: '/\d{4}/\d{2}/'   # which links on those pages are stories
    categories: [business]        # optional: always apply these topics
    require_guam: false           # true for regional outlets
    body_selector: '.story-text'  # optional: if the intro comes out wrong
```

Then `guheadlines check --source mynews`. For WordPress sites, `/feed/` almost
always works. For BLOX/TownNews sites (like postguam.com), use
`/search/?f=rss&t=article&c=<section>&l=50&s=start_time&sd=desc`.

### Topics

Each topic in `config/categories.yaml` has a keyword list. A keyword in the
headline scores 3, in the site's section/tags scores 2, in the intro scores 1;
a topic is applied at 3 points. Stories that match nothing are filed under
Local. Run `guheadlines reclassify` after editing to re-tag stored stories.

## Operations

### Backups

`scripts/backup.sh` writes a compressed `pg_dump` to `./backups` and deletes
backups older than 30 days. Schedule it with cron on the host:

```cron
15 3 * * * cd /opt/GU-headlines && ./scripts/backup.sh >> backups/backup.log 2>&1
```

Restore into a fresh install:

```sh
docker compose up -d db
gunzip -c backups/guheadlines-20260927-0315.sql.gz | docker compose exec -T db psql -U guheadlines -d guheadlines
docker compose up -d
```

Stored images live in the `media` volume; back it up too if you want to keep
them (`docker run --rm -v gu-headlines_media:/m -v "$PWD/backups":/b alpine tar czf /b/media.tgz -C /m .`).

### Upgrading

```sh
git pull
docker compose up -d --build
```

Database migrations are applied automatically on start.

### Public access (HTTPS)

Put a reverse proxy in front of port 8000. With [Caddy](https://caddyserver.com):

```
news.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

`/media/` (stored images) and `/static/` are served by the app; a proxy can
cache them for a long time.

### Monitoring

- `/status`: last run, errors, and new stories (24 h / 7 days) per source.
- `/healthz`: JSON with database status and the time of the last scrape;
  point an uptime checker at it and alert if `last_scrape` is older than two
  hours.

### Running without Docker

Python 3.11+ and PostgreSQL 13+:

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt && pip install -e .
export DATABASE_URL=postgresql://guheadlines:secret@localhost:5432/guheadlines
guheadlines migrate
guheadlines web --port 8000     # e.g. as one systemd service
guheadlines worker              # and this as another
```

## Development

```sh
pip install -e '.[dev]'
pytest                                                             # unit tests
TEST_DATABASE_URL=postgresql://postgres@localhost/guheadlines_test pytest   # + database/web tests
```

The database tests drop and recreate the `public` schema of the test database,
so point `TEST_DATABASE_URL` at a throwaway database.

Layout:

```
config/                 sources.yaml, categories.yaml
guheadlines/
  scraper/              http client, discovery, extraction, images, pipeline
  web/                  FastAPI app, templates, CSS
  migrations/           Alembic schema migrations
  cli.py                `guheadlines` command
scripts/backup.sh
```

## A note on content

GU Headlines stores only what a reader sees on a news front page: the
headline, a short intro and a thumbnail, with every story linking to the
original article. Full article text is not stored. Keep your instance
private or check the publishers' terms before making it public.
