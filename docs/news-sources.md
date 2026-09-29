# News sources

The news sites GU Headlines reads are listed in **`config/sources.yaml`**.
This guide shows how to pause, remove, add and fix them, and explains every
option.

**Contents**

- [How it reads a news site](#how-it-reads-a-news-site)
- [Editing sources.yaml safely](#editing-sourcesyaml-safely)
- [Common tasks](#common-tasks): pause, remove, rename, add, test
- [Adding a new news site, step by step](#adding-a-new-news-site-step-by-step)
- [Every option explained](#every-option-explained)
- [Fixing problems](#fixing-problems)
- [Reading the Sources page](#reading-the-sources-page)
- [Patterns: a short guide](#patterns-a-short-guide)
- [Importing older stories](#importing-older-stories)

---

## How it reads a news site

For each site, every hour, GU Headlines:

1. **Finds links to stories.** It reads the site's **feed** if it has one. A
   feed (also called RSS) is a list of the latest stories that sites publish
   for news readers; it is the cleanest source. It can also read a **listing
   page**, such as the site's "Local News" page, and pick out the links that
   look like stories.
2. **Skips the stories it already has**, so only new pages are downloaded.
3. **Opens each new story** and takes the headline, photo, date, byline and
   first paragraph.
4. **Sorts it into topics** and saves it.

So, for each site, you tell it **where to find links** (feeds and/or listing
pages) and, for listing pages, **what a story link looks like**.

## Editing sources.yaml safely

Open the file from inside the `GU-headlines` folder:

```sh
nano config/sources.yaml
```

The file is written in **YAML**, a simple format where **spacing matters**:

```yaml
sources:
  - slug: postguam                      # a dash starts a new site
    name: The Guam Daily Post           # settings of that site line up
    feeds:                              # a list...
      - https://www.postguam.com/...    # ...one item per line, with a dash
```

YAML rules to remember:
- **Indent with spaces, never Tab.** Use two spaces per level, and line up
  items at the same level exactly.
- Each site starts with `  - slug:` (two spaces, dash, space).
- A `#` starts a note; everything after it on that line is ignored.
- Put **patterns** (see [below](#patterns-a-short-guide)) inside
  **single quotes**: `'/story/\d+/'`.
- Web addresses don't need quotes.

**After saving, check the file for mistakes:**

```sh
docker compose exec worker guheadlines sources
```

If the file is fine, it lists your sites:

```
postguam     on       812 articles  The Guam Daily Post
guampdn      on       640 articles  Pacific Daily News
governor     off        0 articles  Office of the Governor
```

If something is wrong, it says so instead, for example
`config error: sources.yaml [mysite]: needs at least one feed or listing page`,
or `invalid YAML` with a line number. Fix it and run the command again.

A mistake can't break the running site: until you fix it, GU Headlines keeps
using the last correct version of the file, and writes `config error` in its
log.

**Changes take effect on the next check**, at the top of the hour. No restart
is needed. To use them right away:

```sh
docker compose exec worker guheadlines scrape --source SLUG
```

(Replace `SLUG` with the site's slug, for example `kuam`.)

---

## Common tasks

### Pause a site

Add `enabled: false` to it:

```yaml
  - slug: gbm
    name: Guam Business Magazine
    enabled: false
    feeds:
      ...
```

Its stories stay on your site and in search; it just isn't checked any more.
The Sources page shows it as **paused**. To turn it back on, delete that line
or change it to `enabled: true`.

### Turn on the Office of the Governor

It's included but paused. Find `slug: governor` and remove its
`enabled: false` line.

### Remove a site

Pausing is usually better. If you do delete a site's whole block from the
file, its old stories stay (they're part of your archive) and the site is
shown as paused.

### Rename a site

Change `name:`; it updates everywhere on the next check. **Don't change the
`slug`**: the slug is the site's permanent ID. A new slug is treated as a
brand-new site, and the old stories stay under the old one.

### Test a site without saving anything

```sh
docker compose exec worker guheadlines check --source kuam
```

This shows how many story links it finds, then what it *would* save for the
first 3 new stories: headline, date, topics, photo address and intro.
Nothing is saved. Add `--limit 10` to see more.

---

## Adding a new news site, step by step

The example adds an imaginary site, *Island Times*, at
`https://www.islandtimes.example`. Use your own site's address.

### Step 1: Look at the site with `probe`

```sh
docker compose exec worker guheadlines probe https://www.islandtimes.example/
```

`probe` visits the page and describes it. The useful lines are:

```
  cms: WordPress
  feed advertised: https://www.islandtimes.example/feed/
  84 same-site links; most common shapes:
     24  www.islandtimes.example/{year}/{n}/{slug}/
      6  www.islandtimes.example/category/{slug}/
```

- **`cms`** is the software the site runs on. WordPress sites almost always
  have a feed at `/feed/`.
- **`feed advertised`** means the site offers a feed. Use it.
- **`most common shapes`** shows what the site's links look like: `{slug}` is
  a headline in the address, like `governor-signs-bill`; `{n}` is a number;
  `{year}` is a year. The most common shape with a `{slug}` is usually the
  story links.

### Step 2: Check the feed

```sh
docker compose exec worker guheadlines probe https://www.islandtimes.example/feed/
```

A working feed prints `FEED (rss20): 10 entries` and the first few stories.

### Step 3: Add the site to sources.yaml

Add a new block at the end of the file. It must be indented like the others:
two spaces, then a dash.

```yaml
  - slug: islandtimes
    name: Island Times
    homepage: https://www.islandtimes.example/
    feeds:
      - https://www.islandtimes.example/feed/
```

That's enough for a site with a feed. The `slug` is a short ID you choose:
lowercase letters, numbers and dashes only, and unique.

**If the site has no feed**, give it a listing page and describe what a story
link looks like instead. For the shape
`www.islandtimes.example/{year}/{n}/{slug}/` from step 1:

```yaml
  - slug: islandtimes
    name: Island Times
    homepage: https://www.islandtimes.example/
    listing_pages:
      - https://www.islandtimes.example/news/
    article_pattern: '/20\d\d/\d+/'
```

`'/20\d\d/\d+/'` means "a year starting with 20, then a number", which only
story links have. See [Patterns](#patterns-a-short-guide) for more.

### Step 4: Test it

```sh
docker compose exec worker guheadlines sources                     # file OK?
docker compose exec worker guheadlines check --source islandtimes  # what would it save?
```

In the `check` output, confirm that each story has a sensible headline,
date, photo and intro. If a story shows `SKIP`, the reason is given; see
[Fixing problems](#fixing-problems).

### Step 5: Load it

Wait for the next hourly check, or load it now:

```sh
docker compose exec worker guheadlines scrape --source islandtimes --until-done
```

`--until-done` keeps going (with a pause of a minute between rounds) until
every new story from that site is loaded.

### Recipes for common kinds of sites

**WordPress sites** (for example KANDIT, Dept. of Labor): the feed is at `/feed/`.

```yaml
    feeds:
      - https://www.example.com/feed/
```

**BLOX / TownNews sites** (the Guam Daily Post, Pacific Daily News and
Marianas Variety use this; `probe` shows `cms: BLOX/TownNews`): each section
has a feed. Change `c=news/local` to the section you want, as seen in the
site's own address (for example `/business/local/` becomes
`c=business/local`). These sites limit how fast you can read, so slow down.

```yaml
    request_delay: 5
    feeds:
      - https://www.example.com/search/?f=rss&t=article&c=news/local&l=50&s=start_time&sd=desc
    article_pattern: '/article_[0-9a-f-]+\.html'
```

**Wix sites** (for example Pacific Island Times): the feed is at `/blog-feed.xml`.

**A regional site that also covers Saipan, Palau or other islands:** add
`require_guam: true` to keep only stories that mention Guam.

**A Reddit community** (subreddit): Reddit's pages turn robots away, but each
community's feed works. Use `feed_only: true` so posts are built from the
feed without opening them, and `ignore_robots: true` because Reddit's
robots.txt turns all robots away. This is how r/guam is set up; for another
community, change `guam` in both addresses:

```yaml
  - slug: reddit-guam
    name: r/guam (Reddit)
    homepage: https://www.reddit.com/r/guam/
    feeds:
      - https://www.reddit.com/r/guam/new/.rss?limit=50
    feed_only: true
    ignore_robots: true
    categories: [community]
```

---

## Every option explained

Options for one site. Only `slug`, `name`, and a `feeds` or `listing_pages`
entry are required.

| Option | Example | What it does |
|---|---|---|
| `slug` | `postguam` | The site's permanent ID: lowercase letters, numbers, `-` or `_`. Used in commands and in your site's addresses (`/source/postguam`). Don't change it later. |
| `name` | `The Guam Daily Post` | The name shown on the website. |
| `homepage` | `https://www.postguam.com/` | The site's home page. The site's name on each story links here, and the sidebar and Sources page show it as the site's address. |
| `enabled` | `false` | Pauses the site. Leave it out (or use `true`) to keep the site on. |
| `feeds` | *(a list of addresses)* | RSS or Atom feeds to read. The best way to find stories. |
| `listing_pages` | *(a list of addresses)* | Pages to scan for story links, such as the home page or a "Local News" page. Needs `article_pattern`. |
| `article_pattern` | `'/story/\d+/'` | For listing pages: only links matching this pattern are treated as stories. Can be a list of patterns; a link matching any of them counts. |
| `autodiscover_feeds` | `true` | Also use any feed that the listing pages advertise. |
| `exclude_url_patterns` | `- '/opinion/'` | Skip story links whose address matches one of these patterns. Added to the shared list under `defaults:` at the top of the file. |
| `exclude_sections` | `[Opinion, Sports]` | Skip stories that the site files under these sections or feed categories. Added to the shared list under `defaults:`. |
| `categories` | `[military]` | Always put this site's stories in these topics, in addition to the automatic sorting. Use topic *slugs* from `categories.yaml`: `local`, `military`, `business`, `labor` or `community`. |
| `require_guam` | `true` | Keep only stories that mention Guam. What counts as "mentions Guam" is the `guam_keywords` list near the top of the file. |
| `request_delay` | `5` | Seconds between requests to this site. Use it for sites that answer "too many requests" (HTTP 429). If the site's robots.txt asks for a longer delay, that is used instead. |
| `max_age_days` | `30` | Links found on *listing pages* that are older than this are skipped. Home pages often link to old "evergreen" pages (station promos, guides); this keeps them out. Feed stories are never skipped for age. |
| `body_selector` | `'.story-text'` | Where the story text is on the page. Only needed if the intro comes out wrong. To find it, open a story in Chrome or Firefox, right-click its first paragraph and choose **Inspect**. Look a few lines up for the box that holds all the paragraphs, such as `<div class="story-text">`, and write its class with a dot in front: `'.story-text'`. |
| `ignore_robots` | `true` | Read this site even where its robots.txt asks robots not to (see [robots.txt](#robotstxt)). Also covers the photos its stories point to. |
| `feed_only` | `true` | Build stories from the feed alone: the headline, text, photo and date the feed gives, without opening each story's page. For sites whose pages turn robots away but whose feed works, such as Reddit. Needs `feeds`. |
| `sitemaps` | *(a list of addresses)* | Sitemap files for [importing older stories](#importing-older-stories). Usually found automatically. |

### Settings shared by all sites

At the top of `sources.yaml`:

- **`defaults: exclude_sections`**: sections skipped on every site
  (Opinion, Obituaries, Public Notices, Sports, and more). Remove a line to
  start collecting that kind of story, or add one.
- **`defaults: exclude_url_patterns`**: story addresses skipped on every
  site, such as `/sports/` and `/opinion/`.
- **`defaults: max_age_days`** *(optional)*: changes the default of 30 days
  for all sites.
- **`guam_keywords`**: the words that mean "this story is about Guam", for
  sites with `require_guam: true`. Add a village or place name here if Guam
  stories are being skipped. Accents and capital letters don't matter.

### robots.txt

Many sites have a file called `robots.txt` that tells automated programs which
pages they may read. GU Headlines obeys it. Two sites are set to
`ignore_robots: true` on your instructions:

- **Marianas Variety**, whose robots.txt asks all robots except a few search
  engines to stay out.
- **r/guam**, because Reddit's robots.txt turns all robots away. Only the
  community's public feed is read, once an hour, as feed reader apps do; the
  posts themselves are never opened (`feed_only`).

If you use `ignore_robots` on a site, know that the site owner has asked not
to be read this way and may block your server. To turn the exception off,
delete the `ignore_robots: true` line.

---

## Fixing problems

The `check` command explains why each story was skipped:

| `check` says | Meaning | What to do |
|---|---|---|
| `discovered 0 article links` | No feed worked and no links matched the pattern. | Run `probe` on the feed or listing page. Check the address still works, and that `article_pattern` fits the link shapes `probe` shows. |
| `SKIP (rejected): not an article page` | A matched link wasn't a story (e.g. a section page). | Make `article_pattern` stricter, or add the address to `exclude_url_patterns`. |
| `SKIP (rejected): not about Guam` | `require_guam` is on and the story doesn't mention Guam. | Expected for regional sites. If it's wrong, add the missing place name to `guam_keywords`. |
| `SKIP (rejected): excluded section: Opinion` | The site filed it under a skipped section. | Expected. Remove the section from `exclude_sections` if you want them. |
| `SKIP (rejected): older than 30 days (evergreen link)` | An old page linked from a listing page. | Expected. Raise `max_age_days` if real stories are being skipped. |
| `SKIP (rejected): gone (404)` | The link is broken on the news site. | Nothing to do. |
| `SKIP (failed): ...` | The page couldn't be loaded this time. | It is retried automatically: after 1, 2, 4, then 8 hours. |
| The intro is a caption, a menu or empty | The story text wasn't found. | Set `body_selector` to the part of the page that holds the story text. |
| No photo | The site has no share image and no photo in the story. | Nothing to do; the story is shown without a photo. |

## Reading the Sources page

The **Sources** page (top bar) shows the last problem for each site in red.
Common messages:

| Message contains | Meaning | What to do |
|---|---|---|
| `HTTP 403` | The site refused the request. Usually its firewall (often Cloudflare) blocks the kind of internet address your server has. | Nothing, if it still works sometimes. Cloud servers are blocked more often than home connections. Adding contact details to `SCRAPER_USER_AGENT` ([Settings](settings.md#being-a-good-visitor)) can help. Otherwise pause the site. |
| `HTTP 404` | That feed or page no longer exists. The site has moved things around. | Find the new address with `probe` on the site's home page, and update `sources.yaml`. |
| `HTTP 429` / `rate limited by the site; N pages left for later` | The site asked GU Headlines to slow down. | Nothing: it comes back every minute until done. If it happens every hour, add `request_delay: 5` (or higher). |
| `blocked by robots.txt` | The site's robots.txt forbids reading that address. | Pause the site, or see [robots.txt](#robotstxt). |
| `ConnectError`, `ConnectTimeout`, `ReadTimeout` | The site didn't answer: it was down, or your internet was. | Nothing if it's occasional. It tries again next hour. |
| `config error` (in the log) | A mistake in `sources.yaml` or `categories.yaml`. | Run `docker compose exec worker guheadlines sources` to see the problem. |

A site that shows **0** in the *24 h* column for several days, with no error,
may have changed its layout. Run `check` on it.

---

## Patterns: a short guide

`article_pattern` and `exclude_url_patterns` use **patterns** (called
*regular expressions*) to match web addresses. You only need a few symbols.
Capital letters don't matter. A pattern matches if it fits **anywhere** in the
address.

| Symbol | Means | Example | Matches |
|---|---|---|---|
| plain text | exactly that text | `'/story/'` | `…kuam.com/story/353384413/…` |
| `\d` | any single digit | `'/20\d\d/'` | `/2026/` |
| `+` | one or more of the thing before it | `'/story/\d+/'` | `/story/353384413/` |
| `?` | the thing before it is optional | `'/sports?/'` | `/sport/` and `/sports/` |
| `(a\|b)` | either `a` or `b` | `'/(world\|nation)/'` | `/world/` or `/nation/` |
| `[0-9a-f-]` | any one of these characters | `'/article_[0-9a-f-]+\.html'` | `/article_0f2c…-4666.html` |
| `\.` | a real dot (a plain `.` means "any character") | `'\.pdf$'` | addresses ending in `.pdf` |
| `^` / `$` | start / end of the address | `'^https://kuam\.com/'` | addresses that begin that way |

Tips:
- Start from a real story address and keep the part that all story links
  share, for example `/story/` followed by digits.
- Always wrap patterns in single quotes: `'…'`.
- Check your pattern with `check --source …`: `discovered N article links`
  should be a sensible number.

---

## Importing older stories

GU Headlines starts collecting from the day you install it. To fill the
archive with earlier stories from a site, use `backfill`. It reads the site's
**sitemap** (a list of all its pages that most news sites publish):

```sh
docker compose exec worker guheadlines backfill --source pnc --since 2025-01-01 --limit 500
```

- `--since` is the earliest date to import (year-month-day).
- `--limit` is the most stories to import in this run (default 300). Run it
  again to continue.

It goes gently: it waits between requests and pauses when a site asks it to.
Large imports can take hours; leave it running. It can't import from sites
that block your server (see [Reading the Sources page](#reading-the-sources-page)).
