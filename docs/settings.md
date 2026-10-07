# Changing settings

GU Headlines keeps its settings in plain text files you can edit with any text
editor. This guide explains each one.

| File | What it holds | How to apply a change |
|---|---|---|
| `.env` | General settings: site name, how often to check, photos, port, password | Run `docker compose up -d` |
| `config/sources.yaml` | The list of news sites | Nothing to do: used on the next check (see [News sources](news-sources.md)) |
| `config/categories.yaml` | The topics and their keywords | Nothing to do: used within a minute (see [Topics](topics.md)) |
| `guheadlines/web/static/style.css` and the templates | Colors and page layout | Run `docker compose up -d --build` (see [Changing the look](#changing-the-look)) |

---

## How to change a setting in `.env`

1. Open the file (from inside the `GU-headlines` folder):

   ```sh
   nano .env
   ```

2. Find the line, change the value after the `=` sign, and save
   (in `nano`: **Ctrl+O**, **Enter**, **Ctrl+X**).
3. Apply the change:

   ```sh
   docker compose up -d
   ```

   Docker restarts only the parts that need the new setting. The website is
   back within a few seconds.

**Rules for the `.env` file**
- One setting per line, written `NAME=value`, with no spaces around the `=`.
- No quotes needed, even for values with spaces: `SITE_NAME=Guam News Now`.
- Lines starting with `#` are notes and are ignored. To switch a setting off
  and use its default, put `#` in front of it.
- For yes/no settings use `true` or `false`.
- If a line is missing, the default in the tables below is used.

---

## Website

| Setting | Default | What it does |
|---|---|---|
| `SITE_NAME` | `GU Headlines` | The name in the top bar, browser tab and footer. |
| `SITE_TAGLINE` | `Guam news, updated every hour` | The short line in the footer and in the description search engines show. If you change how often the site checks for news, update this too. |
| `PAGE_SIZE` | `30` | Stories per page before the **Older →** link. Anything from 5 to 100. |
| `WEB_PORT` | `8000` | The port the website uses: `http://your-server:8000`. Change it if something else already uses 8000, or set it to `80` to leave the port off the address (only if nothing else on the server uses port 80). |

**Example: rename the site**

```
SITE_NAME=Island Headlines
SITE_TAGLINE=Every Guam story, every hour
```

## Checking for news

| Setting | Default | What it does |
|---|---|---|
| `SCRAPE_INTERVAL_MINUTES` | `60` | How often to check every news site. Checks happen on the clock: `60` means at the top of every hour, `30` at :00 and :30, `120` every other hour. The minimum is 5. Checking more often than every 30 minutes rarely finds more news and makes more requests to the news sites. |
| `SCRAPE_ON_START` | `true` | Also check immediately when GU Headlines starts, instead of waiting for the next scheduled check. |
| `MAX_NEW_PER_SOURCE` | `40` | The most new stories saved from one site in one round. Anything left over is picked up in a catch-up round (next setting). This keeps the first load and very busy days from hammering one site. |
| `CATCH_UP_SECONDS` | `60` | When a site isn't fully loaded (it had more stories than one round takes, or it said "too many requests"), come back for that site after this many seconds, and keep coming back until it's done. Then it returns to the regular schedule. The minimum is 30. If a site asks for a longer pause, that is honored (up to 10 minutes). |
| `PER_HOST_DELAY` | `1.5` | Seconds to wait between two requests to the same news site. Higher is gentler. Individual sites can be slowed down further in `sources.yaml` (`request_delay`). |
| `SCRAPER_WORKERS` | `4` | How many news sites are checked at the same time. |
| `REQUEST_TIMEOUT` | `25` | Seconds to wait for a news site to answer before giving up (it will try again later). |

**Example: check every 30 minutes**

```
SCRAPE_INTERVAL_MINUTES=30
SITE_TAGLINE=Guam news, updated every 30 minutes
```

## Being a good visitor

| Setting | Default | What it does |
|---|---|---|
| `RESPECT_ROBOTS_TXT` | `true` | Obey each site's `robots.txt` file, where sites say which pages robots may read. Keep this on. To make an exception for one site, use `ignore_robots: true` on that site in `sources.yaml` instead (see [News sources](news-sources.md#robotstxt)). |
| `SCRAPER_USER_AGENT` | `Mozilla/5.0 (compatible; GUHeadlinesBot/1.0; +https://github.com/gottalogin1/GU-headlines)` | How GU Headlines introduces itself to news sites. It's good manners to include a way to reach you, so a site owner can ask you something instead of blocking you. |

**Example: add your contact details**

```
SCRAPER_USER_AGENT=Mozilla/5.0 (compatible; GUHeadlinesBot/1.0; +mailto:you@example.com)
```

## Photos

| Setting | Default | What it does |
|---|---|---|
| `STORE_IMAGES` | `true` | Save a copy of each story's photo on your server, so old stories keep their pictures even after the news site deletes them. With `false`, the photo is loaded from the news site each time a page is viewed. |
| `IMAGE_MAX_WIDTH` | `720` | Width in pixels of saved photos. 720 looks sharp on the site, including on phones. Larger means bigger files. This only affects photos saved from now on. |

Saved photos are stored in Docker's `media` storage, about 30 to 60 KB each.

## Database

| Setting | Default | What it does |
|---|---|---|
| `POSTGRES_PASSWORD` | *(none: you must set it)* | The database password. **Set it once, before the first start, and don't change it afterwards**: the database keeps the password it was created with. |
| `POSTGRES_DB` | `guheadlines` | Database name. Leave as is. |
| `POSTGRES_USER` | `guheadlines` | Database user name. Leave as is. |
| `DATABASE_URL` | *(not set)* | Only for running without Docker (see [How it works](how-it-works.md#running-without-docker)). Replaces the three settings above. |

**Changing the database password later (advanced).** Change it inside the
database first, then in `.env`:

```sh
docker compose exec db psql -U guheadlines -c "ALTER USER guheadlines PASSWORD 'the-new-password'"
nano .env                 # put the same new password in POSTGRES_PASSWORD
docker compose up -d
```

## Troubleshooting output

| Setting | Default | What it does |
|---|---|---|
| `LOG_LEVEL` | `INFO` | How much the programs write to the log. `DEBUG` shows every story it saves or skips and why; useful when a site misbehaves, noisy otherwise. `WARNING` shows only problems. |

---

## Changing the look

The name and tagline are in `.env` (above). Colors and layout live in the
program's own files. If you change them, write down what you changed: a future
update may replace these files (see
[Looking after it](maintenance.md#updating)).

- **Colors:** open `guheadlines/web/static/style.css`. The first block,
  starting `:root {`, lists every color with a name, for example
  `--accent: #2563eb;` (the blue used for buttons and links). The second
  block, inside `@media (prefers-color-scheme: dark)`, holds the same colors
  for dark mode. Colors are written as hex codes; any "color picker" website
  gives you one.
- **Logo:** the "GU" square is in `guheadlines/web/templates/base.html`
  (`<span class="brand-mark">GU</span>`). Change the letters there.
- **Browser tab icon:** `guheadlines/web/static/favicon.svg`.

Apply with:

```sh
docker compose up -d --build
```

Your browser may keep showing the old colors for a minute; press
**Ctrl+Shift+R** (**Cmd+Shift+R** on Mac) to reload fully.
