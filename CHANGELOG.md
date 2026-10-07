# Changes

What changed in each release of GU Headlines. To update to the newest
release, see [Updating](docs/maintenance.md#updating).

## Unreleased

### Changed
- **r/guam posts appear only on the Community page.** They're no longer on
  the front page, the other topics or the archive, nor in the sidebar's list
  of sources; search shows them when you search within Community. Set with
  the new `only_in_topic` option in `sources.yaml`.
- **PNC News First is removed**, as it has closed. Sites removed from
  `sources.yaml` no longer appear on the Sources page.
- **Marianas Business Journal, Guam Business Magazine and Andersen Air Force
  Base are no longer read**, as their firewalls turn away automated visitors.
  They stay on the list in a separate "Can't be collected" area on the
  Sources page and in the sidebar, with links to visit them. Set with the new
  `cannot_scrape` option; `check --source` still tries one, to see whether it
  works again.

### Fixed
- The top menu no longer hides its last items (Archive, Sources) on laptop
  screens since the Community topic was added. Below 1,200 pixels wide the
  menu has its own row.

## 1.0.1 (2026-09-29)

### Added
- **Guam Federation of Teachers** (gftunion.com) as a news site, under Labor.
- **r/guam on Reddit**, under a new **Community** topic. Posts are read from
  the community's feed; picture posts show the picture.
- `feed_only` option in `sources.yaml`, for sites whose pages turn robots
  away but whose feed works.
- The Live source check can probe any addresses you give it, to try out a
  site before adding it.

### Fixed
- A "bot check" page (such as "One moment, please..." or "Just a moment...")
  is no longer mistaken for a site's content. The Sources page says so ("...
  showed a bot check instead of the page"), and stories behind one are
  retried later instead of being skipped for good. "HTTP 403" messages name
  the firewall when it can be told, for example "(blocked by Cloudflare)".
- A news site's feed that answers "too many requests" is read again a minute
  later, like the story pages already were, instead of waiting for the next
  hour. (This was the red "HTTP 429" on Pacific Daily News, the Daily Post and
  Marianas Variety.)
- A site asking to slow down is shown on the Sources page as a grey
  "catching up" note, not as a red error; it finishes loading on its own.

## 1.0.0 (2026-09-27)

The first release.

### What it does
- Checks Guam's news sites every hour and saves only stories it hasn't seen
  before: the headline, the story's own photo and its opening paragraphs, with
  a link back to the full article.
- If a site isn't fully read in one round (it had many new stories, or it said
  "too many requests"), it comes back for that site every minute until it's
  done, then returns to the hourly schedule.
- Sorts stories into **Local, Military, Business and Labor** using keyword
  lists you can edit in `config/categories.yaml`.
- Search across every story ever collected: exact "phrases", `or`,
  `-exclusions`, filters by site, topic and date, and matching that ignores
  word endings and accents ("Hagatna" finds "Hagåtña").
- An archive by year, month and day (Chamorro Standard Time), a Sources page
  showing how each site is doing, and `/healthz` for uptime monitors.
- A clean frosted-glass design that works on phones and in light and dark
  mode.

### News sites
The Guam Daily Post, Pacific Daily News, KUAM News, KANDIT News Group,
Pacific Island Times, Marianas Variety, Stars and Stripes, DVIDS (Guam
commands), Guam Department of Labor, PNC News First, Marianas Business
Journal, Guam Business Magazine and Andersen Air Force Base. The Office of the
Governor's press releases are included but switched off. Sites are listed in
`config/sources.yaml`.

### Running it
- One command (`docker compose up -d --build`) starts the database, the
  website and the news collector.
- Database upgrades are applied automatically; `scripts/backup.sh` makes
  dated backups.
- Beginner guides in `docs/` cover installing, settings, news sites, topics,
  backups, updates and putting the site on your own domain.

### Known limitations
- PNC News First, Marianas Business Journal, Guam Business Magazine and
  Andersen Air Force Base block visits from data centers. They usually work
  from a home or business internet connection; the Sources page shows
  whether they do.
- Marianas Variety only allows slow reading, so a big batch of its stories can
  take a while to appear. Its robots.txt request is ignored, as the site owner
  chose (`ignore_robots` in `sources.yaml`).
