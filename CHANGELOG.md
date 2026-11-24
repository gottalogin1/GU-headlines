# Changes

What changed in each release of GU Headlines. To update to the newest
release, see [Updating](docs/maintenance.md#updating).

## Unreleased

### Added
- **Automatic updates for a server.** `./scripts/auto-update.sh --install`
  makes the server check every 5 minutes for new commits on the `main`
  branch. When there are some, it makes a database backup, then rebuilds
  and restarts the site by itself. If the new version doesn't start within
  5 minutes, it goes back to the version before. It also checks that the
  site answers. See
  [Automatic updates](docs/maintenance.md#automatic-updates).
- **Alerts in Discord.** With `DISCORD_WEBHOOK_URL` in `.env`, GU Headlines
  posts a message in a Discord channel when a news site or job board has
  not worked for a day (`ALERT_AFTER_HOURS`), when the site stops
  answering, when the news hasn't been checked for 3 hours, and after each
  automatic update. A new **Site check** on GitHub checks the site from
  outside every 30 minutes, so an outage of the whole server is reported
  too. `guheadlines alert-test` posts a test message. See
  [Alerts in Discord](docs/maintenance.md#alerts-in-discord). When you
  update, the database gets one small new table, by itself. To get the
  alerts, follow the steps there; without them, nothing changes.
- **An Events page** (new **Events** link in the top bar) listing what's
  coming up on Guam over the next 90 days, grouped by day, in Guam time:
  - Guam Legislature sessions, public hearings and committee meetings from
    the Legislature's Google Calendar, with each session's agenda (PDF) from
    its Agendas page. Certificate presentations are left out.
  - GovGuam board, commission and agency meetings from notices.guam.gov.
  - Guam Community College events.

  Search by word or narrow to one calendar. The Guam Visitors Bureau, the
  Guampedia fiesta calendar and the Chamber of Commerce are listed with the
  reason they can't be collected. The page can show an ad, like the Jobs
  page. See [Events](docs/events.md).
- `guheadlines events` reads the calendars now; `--check` shows what each
  gives. The worker reads them every `EVENTS_INTERVAL_HOURS` (default 3).

**To update:** the database upgrade runs by itself. For the Legislature's
calendar, add a free Google key as `GOOGLE_CALENDAR_API_KEY` in `.env` (the
steps are in `.env.example`, and in [Events](docs/events.md#google-calendar-key)).
Without it, the Events page still lists the Legislature's sessions from its
Agendas page and the other calendars.

### Changed
- **Andersen Air Force Base is collected again.** The base's own site turns
  automated visitors away, so it was listed under **Can't be collected**.
  Its 36th Wing posts the same news releases, with photos, to its page on
  DVIDS (the Defense Department's service for sharing military news), and
  GU Headlines now reads that page. Stories that never mention Guam or
  Andersen are skipped. Nothing to do when you update.
- **The Guam Visitors Bureau is listed under Can't be collected.** Its
  firewall has turned automated visitors away since about October 2026, so
  no new release was collected after October 3. The Sources page and the
  sidebar now list it with a link, like the other blocked sites. If
  `guheadlines check --source gvb` finds stories again from your server,
  delete its `cannot_scrape` line in `config/sources.yaml`.
- **The sites that still can't be collected were checked again** in
  November 2026: Marianas Business Journal, Guam Business Magazine, the Guam
  Visitors Bureau and RNZ Pacific still turn automated visitors away, and
  none has another official copy of its stories that GU Headlines may use.
  RNZ's terms of use also don't let other websites use its feeds, and the
  Sources page now says so. The notes in `config/sources.yaml` list what was
  tried.

### Removed
- **r/guam and the Community page.** Reddit ended its public feeds on
  November 13, 2026, and stopped giving out API keys on October 31, 2026, so
  r/guam can't be collected any more. The r/guam source, the Community topic
  and page, and the Reddit settings (`REDDIT_CLIENT_ID`,
  `REDDIT_CLIENT_SECRET`, `REDDIT_USERNAME`) are gone. When you update, the
  r/guam posts already stored are **deleted**, with their pictures, the
  first time GU Headlines starts. That can't be undone, so take a backup first
  (`./scripts/backup.sh`). Old links to the Community page now show "page not
  found". If your `.env` has the three `REDDIT_` lines, you can delete them;
  they are no longer used. The same goes for the `REDDIT_` repository secrets
  on GitHub, if you added them.

### Fixed
- **New stories that show only a date stay at the top.** Some news sites
  give the day of a story but not the time. Such a story was put at 12:00 AM,
  below the stories from earlier that day, and its card said "15 hrs ago" for
  a story that was new. Now, when the story is collected on the day it shows,
  it gets the time it was collected. Stories are still sorted newest first by
  the time the news site gives, and by the time they were collected when the
  site gives none. Stories already on the site keep their times.

## 1.2.0 (2026-11-24)

### Added
- **Port 80 and https, with Caddy.** A fourth part, `caddy`, is now the
  front door of the site. The site opens at `http://your-server` (port 80)
  instead of `http://your-server:8000`. Put your domain name in the new
  `SITE_DOMAIN` setting, and the site is served at `https://` your domain,
  with a free certificate that Caddy gets from Let's Encrypt and renews by
  itself; `http://` visits are sent on to `https://`, and browsers are told
  to keep using https. Search engines are given that https address, so
  `SITE_URL` is now needed only to pick a different one. The website is no
  longer reachable on port 8000 from outside Docker, so every visit goes
  through Caddy. Steps: [Putting it on the internet](docs/maintenance.md#putting-it-on-the-internet).
  `WEB_PORT` is no longer used; `HTTP_PORT` (default 80) and `HTTPS_PORT`
  (default 443) replace it. If something else on your server uses port 80 or
  443 (Apache, nginx, or a Caddy you installed from the old guide), switch it
  off first. To get the new notes in `.env`, see
  [Changing settings](docs/settings.md#getting-the-newest-notes-in-env).
- **Four new topics: Sports, Food, Tourism and Saipan & CNMI**, each with
  its own page in the top menu. Tourism stories used to be filed under
  Business. New news sites feed them: Guam Sports Network, the Guam Football
  Association, the Guam Visitors Bureau's news releases and Isla Public Media
  (KPRG). Pacific Daily News now also gives its sports and lifestyle stories,
  the Guam Daily Post its lifestyle stories and Marianas Variety its sports
  stories, and the Office of the Governor's press releases are switched on.
  Stories about Saipan and Tinian are kept now, under Saipan & CNMI instead
  of Local. Two new options in `config/sources.yaml` make this work:
  `allow_sections` collects a section that is skipped by default (such as
  Sports) on one site, and `undated` accepts story pages that show no date
  (see [News sites](docs/news-sources.md)).
- **An Obituaries page** (in the top menu) for obituaries, death notices,
  rosaries and funerals. They no longer appear under Latest, on the other
  topics' pages or in the archive, but search still finds them. Obituaries
  were skipped before; now they are collected from the Guam Daily Post's and
  Marianas Variety's obituary sections, KANDIT's daily list of rosaries and
  funerals, and any site that files a story as an obituary. Once AdSense is
  set up, the Obituaries page shows the ad block too. Two new topic options
  in `config/categories.yaml` do this: `separate: true` and `show_ad: true`
  (see [Topics](docs/topics.md#separate-topics-obituaries)).
- **Guam News Network** (guamnewsnetwork.com) is a new news site. Its feed
  gives the latest 30 stories with photos. Opinion pieces are skipped, and
  its National, World and sports stories (all about Guam) are kept.
- **Search engines can find and list the site.** Every page has its own
  title and summary, and one address that search engines are told to use.
  Links shared on social media show a picture card. `/sitemap.xml` lists the
  home page, topics, news sites, Jobs and every archive day that has
  stories, and `robots.txt` points to it. Pages that don't belong in search
  results (searches, the Sources page, empty archive days, missing pages)
  ask not to be listed, and stored copies of publishers' photos ask image
  search to send people to the publishers instead. New settings: `SITE_URL`
  (the site's public address) and `GOOGLE_SITE_VERIFICATION` and
  `BING_SITE_VERIFICATION` (the codes Google Search Console and Bing
  Webmaster Tools give you). The site also has an icon for browser tabs and
  phone home screens. See [Search engines](docs/maintenance.md#search-engines).
- The **Sources** page says why the site exists, and has a **Disclaimer**:
  the site writes none of the stories, their content belongs to the
  publishers, the only aim is to send readers to them, and there is no
  affiliation or warranty. Links at the bottom of every page lead to both.
- **An ad block** for Google AdSense at the top of the right-hand column on
  the home, Jobs and Sources pages. It stays off until `ADSENSE_CLIENT` and
  `ADSENSE_SLOT` are set. The site then also serves `ads.txt` and the AdSense
  verification tag. See [Advertising](docs/settings.md#advertising).

### Changed
- **Pages load faster.** Pages are sent compressed, browsers keep the site's
  styles and scripts for a year (and fetch them again when they change) and
  stored photos for 30 days, and the top story's photo loads first. Story
  cards no longer blur the page behind them: the look is the same, and
  scrolling is smoother.
- **Every link in the top bar stays visible on narrow screens.** The topic
  links wrap onto more lines instead of scrolling sideways, so Archive, Jobs
  and Sources are never cut off. On phones the search box sits under the
  logo.
- **r/guam is read through Reddit's API.** Since mid-November 2026 every
  Reddit feed answers "Not Found" to readers who aren't logged in, so the
  Sources page showed an HTTP 404 error every hour. The API needs a free
  Reddit app key in the new `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` and
  `REDDIT_USERNAME` settings ([how to get one](docs/news-sources.md#reddit)).
  Until it is set, r/guam is listed under **Can't be collected** as waiting
  for a key, and the posts collected before stay on the Community page.
  Removed and adult posts are left out.
- **RNZ Pacific** was added as a news site in this release, but is listed
  under **Can't be collected**. Its firewall answers HTTP 403 to GU
  Headlines on every request, so it never gave a story.
- **The settings template (`.env.example`) explains each setting** for a
  person who is new to this work. Each note tells what the setting does, if
  it is required, its default, an example, where to get a key, and if it is
  secret. To copy the new notes into your own `.env` and keep your values,
  see [Changing settings](docs/settings.md#getting-the-newest-notes-in-env).
- The scraper's default name for itself (`SCRAPER_USER_AGENT`) no longer
  includes a link to the project's code: it is now
  `Mozilla/5.0 (compatible; GUHeadlinesBot/1.0)`.

### Security
- Every page is sent with security headers: a Content-Security-Policy that
  lets only the site's own scripts run (plus Google's, on pages with the ad
  block), and headers that stop other sites from framing the site or
  guessing file types. The site no longer says which server software runs
  it.
- Links and photos taken from news sites are used only when they are
  ordinary `http://` or `https://` addresses.
- The scraper connects only to public internet addresses, so a photo
  address or a redirect can't make it reach your database or your local
  network.
- Pillow, the library that opens photos, is updated from 12.3.0 to 13.0.0,
  which fixes its known security flaws, and it opens photos only as JPEG,
  PNG, GIF, WebP or AVIF.
- The website and collector containers run without extra Linux permissions
  and can't gain new ones.

### Fixed
- **News stories no longer land in Community**, the topic for r/guam posts.
  A story was put there when its site filed it under a "Community" section
  or the word "community" was in its address. To take older stories out of
  Community, run `docker compose exec worker guheadlines reclassify`.
- **DVIDS** stories are collected again. Its search pages started answering
  automated visitors with a JavaScript challenge page, so DVIDS found no
  stories; the Joint Region Marianas, Naval Base Guam and Camp Blaz unit pages
  are read instead. Stories that only showed up in the Andersen Air Force Base
  search are no longer picked up from DVIDS.
- A JavaScript challenge page answered with HTTP 202 (as AWS WAF sends) now
  shows as a bot-check error on the Sources page instead of a run that quietly
  finds nothing.
- **Guam Visitors Bureau releases no longer name "GuamWEBZ" as the
  writer.** That is the bureau's web designer. Website builders' credits are
  left out of bylines (a real name next to one is kept), and stories already
  stored are cleaned up on their own.
- **`scripts/backup.sh` works from WSL on Windows.** It stopped with
  `$'\r': command not found` (Windows line endings), or when `SITE_NAME` had
  a space in it. A copy downloaded before this release needs a one-time fix:
  see [Backups](docs/maintenance.md#backups) (under "Windows").
- A few odd addresses no longer give a server error (500): a hidden NUL
  character in the address, archive days that don't exist (such as February
  30), very large page numbers and the year 1. The archive's previous and
  next links jump to the nearest day or month that has stories, instead of
  stepping through empty ones.

### Also
- The Live source check gives its scrape 70 minutes, stops the catch-up
  scrape at 33 minutes so its summary is still written, and runs on Ubuntu
  24.04.

### Updating from 1.1.0
1. Take a backup: `./scripts/backup.sh`.
2. Make sure nothing else on your server uses port 80 or 443 (such as
   Apache, nginx, or a Caddy you installed from the old guide). If something
   does, switch it off first.
3. `git pull`, then `docker compose up -d --build`. The site now opens at
   `http://your-server`, with no `:8000`. There are no database changes in
   this release.
4. Sort the stories you already have into the new topics, and take news out
   of Community:
   `docker compose exec worker guheadlines reclassify`.
5. Copy the new notes into your `.env`
   ([how](docs/settings.md#getting-the-newest-notes-in-env)), then fill in
   the settings you want: `SITE_DOMAIN` once you have a domain name (for
   https), the three Reddit settings for r/guam, and `ADSENSE_CLIENT` and
   `ADSENSE_SLOT` for ads. If your `.env` has a `WEB_PORT` line, delete it;
   it is no longer used.

## 1.1.0 (2026-10-13)

### Added
- **A Jobs page** (new **Jobs** link in the top bar) listing openings on Guam
  from GuamJobsOnline, the Guam Department of Education, federal jobs
  (federalgovernmentjobs.us and, with a free key, USAJOBS), the University of
  Guam, Guam Community College and its partners, and GuamTemps. Each posting
  shows its title, employer, pay, location, open and closing dates, the start
  of the description, the announcement PDF when there is one, and when it was
  last scraped. Anything a posting doesn't give is left blank. Filter by word
  or board. See [Jobs](docs/jobs.md).
- GovGuam Jobs and the American Job Center are listed with the reason they
  can't be collected.
- `guheadlines jobs` reads the boards now; `--check` shows what each gives.
  The worker reads them every `JOBS_INTERVAL_HOURS` (default 6).
- A board that is blocked, needs JavaScript, shows no postings or errors is
  marked with the reason on the Jobs page, and keeps its earlier postings.

### Updating from 1.0.3
`git pull`, then `docker compose up -d --build`. The database is upgraded
automatically, and the job boards are read when the worker starts. To include
USAJOBS, add a key to `.env` (see [Jobs](docs/jobs.md#usajobs)).

## 1.0.3 (2026-10-09)

### Added
- **A light/dark switch** at the right of the top bar. On a first visit the
  site follows the device's setting, and stays dark when the browser gives no
  answer. A choice made with the switch is remembered in that browser;
  switching back to the device's mode follows the device again.

### Changed
- **Light mode is a plain white page**, with white cards set off by thin
  borders, instead of the tinted background.
- To fit the switch, the top menu is a little tighter, and the search box
  takes whatever room is left.
- The README screenshot shows the new light mode.

### Updating from 1.0.2
`git pull`, then `docker compose up -d --build`. Nothing else to do. If a
browser still shows the old look, reload the page once (Ctrl+Shift+R, or
Cmd+Shift+R on a Mac).

## 1.0.2 (2026-10-07)

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

### Also
- The README has an "Updating to a new version" section and a new
  screenshot of the current design.

### Updating from 1.0.1
`git pull`, then `docker compose up -d --build`. Nothing else to do: r/guam
posts you already have move to the Community page on the first check after
the update.

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
