# How it works

Behind the scenes, for the curious and for developers. You don't need this
page to run GU Headlines.

**Contents**

- [The three parts](#the-three-parts)
- [What happens every hour](#what-happens-every-hour)
- [How search works](#how-search-works)
- [All commands](#all-commands)
- [The database](#the-database)
- [GitHub checks](#github-checks)
- [Running without Docker](#running-without-docker)
- [Development](#development)

---

## The three parts

`docker compose up` starts three containers:

| Container | What it is | Details |
|---|---|---|
| `db` | PostgreSQL 16, the database | Data is kept in the Docker volume `pgdata`. |
| `web` | The website (Python, FastAPI) | Pages are built on the server, so no JavaScript is needed. Serves saved photos from the `media` volume. Listens on port 8000 (`WEB_PORT`). |
| `worker` | The news collector | Runs on a clock-aligned schedule (`SCRAPE_INTERVAL_MINUTES`) and saves photos to the `media` volume. |

`web` and `worker` are the same program (`guheadlines`) started with
different commands. Both read `config/` from your folder (mounted
read-only), which is why edits to the YAML files need no rebuild. Both apply
database upgrades (Alembic migrations) when they start.

## What happens every hour

For each enabled news site:

1. **Discover.** Fetch the configured feeds and listing pages. Feeds are
   requested with `If-None-Match` / `If-Modified-Since`, so an unchanged feed
   answers `304 Not Modified` and costs almost nothing. An identical response
   is also skipped.
2. **Skip what is known.** Every link is normalized (tracking parameters,
   `www`, trailing slashes and AMP variants are removed) and looked up in the
   database. Links already stored, already rejected (not an article, not about
   Guam), or waiting for a retry are not downloaded.
3. **Cheap filters first.** Feed stories in excluded sections, or not about
   Guam for regional sites, are rejected from the feed data alone, without
   downloading the page.
4. **Fetch new pages**, newest first, at most `MAX_NEW_PER_SOURCE` per round.
   From each page, in order of reliability:
   - OpenGraph and JSON-LD (schema.org `NewsArticle`) metadata,
   - common news-site body containers (WordPress, BLOX/TownNews, Drupal, Wix,
     DVIDS, af.mil),
   - story text embedded as JSON by JavaScript-built pages (e.g. KUAM's
     `__PAGE_MODEL__`, Next.js `__NEXT_DATA__`),
   - [trafilatura](https://trafilatura.readthedocs.io) as a general fallback.

   Sites marked `feed_only` (Reddit) are not opened: the story is built from
   the feed entry's headline, text, picture and date.

   The intro is the story's opening paragraphs: it adds paragraphs until it has
   about 280 characters (at most three paragraphs, cut at 700 characters),
   skipping photo captions, bylines and "subscribe" lines.
5. **Tag and store.** Topics are assigned ([Topics](topics.md)). The photo is
   downloaded, resized to `IMAGE_MAX_WIDTH`, and saved as WebP. A photo the
   site reuses on many stories (a logo) is ignored from then on.
6. **Retry or remember.** Pages that fail are retried after 1, 2, 4 and
   8 hours. If an article page is blocked but its feed was readable, the
   feed's headline and summary are used instead.
7. **Catch up.** If pages are left over (the per-round limit was reached, or
   the site answered `429 Too Many Requests`), the worker returns for just
   those sites after `CATCH_UP_SECONDS`, or after the site's `Retry-After` if
   that is longer (up to 10 minutes). It continues with the leftover pages
   without re-reading the feeds, until everything is loaded, then returns to
   the regular schedule.

Only one collection runs at a time (a PostgreSQL advisory lock), even if you
start one by hand while the worker is running.

**Being polite:** it identifies itself (`SCRAPER_USER_AGENT`), waits between
requests to the same site (`PER_HOST_DELAY`, `request_delay`, and robots.txt
`Crawl-delay`), obeys robots.txt except for sites marked `ignore_robots`, and
never tries to get around a site's blocking.

## How search works

Each story has a PostgreSQL full-text index built from:
- the headline (weighted highest),
- the intro,
- the byline, section and tags (weighted lowest).

It uses a custom text-search setup, `guam_english`: English word stemming
(so *workers* matches *worker*) plus `unaccent` (so *Hagatna* matches
*Hagåtña*). Queries use PostgreSQL's `websearch_to_tsquery`, which gives the
`"phrase"`, `or` and `-word` syntax. "Best match" ranks by `ts_rank_cd`,
divided by the story's age in years plus one, so a year-old story needs twice
the text score to outrank today's. When nothing matches exactly, a `pg_trgm`
word-similarity search on headlines finds near-misses.

## All commands

Run with `docker compose exec worker guheadlines COMMAND` (or just
`guheadlines COMMAND` without Docker).

| Command | What it does |
|---|---|
| `scrape` | Check all enabled sites now. `--source SLUG` for one site (repeatable). `--until-done` keeps catching up every `CATCH_UP_SECONDS` until nothing is left (`--max-rounds N` limits it, default 60). |
| `check --source SLUG` | Dry run: what the site's feeds and pages give, and what would be saved for the first few new stories (`--limit N`, default 3). Saves nothing. |
| `probe URL…` | Describe a page or feed: status, CMS, advertised feeds, common link shapes, what the extractor gets. `--source SLUG` probes a site's configured addresses; `--all` probes every site. Needs no database. |
| `backfill --source SLUG --since YYYY-MM-DD` | Import older stories from the site's sitemaps (`--limit N`, default 300). |
| `reclassify` | Re-apply `categories.yaml` to every stored story. |
| `sources` | List the configured sites with story counts; also validates both YAML files. |
| `migrate` | Apply database upgrades (done automatically on start). |
| `web` | Run the website (`--port`, `--workers`). |
| `worker` | Run the scheduled news collector. |

## The database

| Table | Holds |
|---|---|
| `sources` | One row per news site: name, homepage, on/off, last check, last error |
| `articles` | The stories: URL (unique), headline, intro, byline, section, tags, topics, photo, publish time, and the search index |
| `seen_urls` | Links that were checked but not saved (rejected, or failed and waiting to retry), so they aren't downloaded every hour |
| `http_cache` | Feed validators (`ETag`, `Last-Modified`, content hash) for conditional requests |
| `ignored_images` | Photos detected as logos or placeholders |
| `scrape_runs` | One row per site per check, for the Sources page (kept 180 days) |

Schema changes are Alembic migrations in `guheadlines/migrations/versions/`.

## GitHub checks

Three workflows run in GitHub Actions:

- **CI**, on every push and pull request: lint (ruff), the test suite
  against PostgreSQL, and a Docker Compose smoke test that builds the image,
  starts the stack and loads the main pages.
- **Live source check**, started from the Actions tab (*Run workflow*), or by
  a push that changes `config/` or the scraper with `[live]` in the commit
  message. It:
  - runs `probe --all`,
  - runs `check` on every site,
  - does a full `scrape --until-done` and a second, hourly-style scrape into
    a throwaway database,
  - prints a summary,
  - starts the website and checks every page,
  - saves screenshots of the site as a 3-day artifact on the run page.

  GitHub's servers are in a US data center, so sites that block data-center
  addresses fail there even if they work for you.
- **Release**, when a version tag such as `v1.0.0` is pushed (or run from the
  Actions tab with the tag name): checks that the tag matches the version in
  `pyproject.toml` and publishes a GitHub release with that version's notes
  from `CHANGELOG.md`.

To make a new release: set the new `version` in `pyproject.toml` and
`guheadlines/__init__.py`, add a section for it at the top of
`CHANGELOG.md`, merge, then tag the merged commit (`git tag v1.1.0` and
`git push origin v1.1.0`).

## Running without Docker

You need Python 3.11+ and PostgreSQL 13+.

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt && pip install -e .
export DATABASE_URL=postgresql://guheadlines:secret@localhost:5432/guheadlines
guheadlines migrate
guheadlines web --port 8000     # run this as one service (e.g. systemd)
guheadlines worker              # and this as another
```

Without Docker, photos go to `data/media` in the project folder (set
`MEDIA_DIR` to change it), and settings come from environment variables with
the same names as in `.env`.

## Development

```sh
pip install -e '.[dev]' ruff
ruff check guheadlines tests && ruff format --check guheadlines tests
pytest                                                                      # unit tests
TEST_DATABASE_URL=postgresql://postgres@localhost/guheadlines_test pytest   # + database and website tests
```

The database tests drop and recreate the `public` schema of the test
database, so point `TEST_DATABASE_URL` at a throwaway database.

```
config/                 sources.yaml, categories.yaml
guheadlines/
  scraper/              HTTP client, discovery, extraction, images, pipeline, probe
  web/                  FastAPI app, queries, templates, CSS
  migrations/           Alembic schema migrations
  cli.py                the `guheadlines` command
  schedule.py           when the worker wakes up next
tests/                  pytest suite with saved sample pages in tests/fixtures
.github/workflows/      CI, the live source check and releases
scripts/backup.sh       database backup
```
