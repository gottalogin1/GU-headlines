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
- Write notes on their own line. In a value, a space followed by `#` starts a
  note, and Docker ignores the rest of the line.
- Do not use `$` in a value. Docker reads `$name` as the name of a different
  variable and replaces it, so `ab$cd` becomes `ab`. If a value must contain
  `$`, put the value in single quotes: `POSTGRES_PASSWORD='ab$cd'`.
- For yes/no settings use `true` or `false`.
- If a line is missing, the default in the tables below is used.

## Getting the newest notes in `.env`

`.env.example` has a note for each setting: what it does, if it is required,
its default, an example, and which values to keep secret. `git pull` never
changes your own `.env`, so your file keeps the notes of the version that you
first copied. To get the newest notes and keep all your values, run these
commands in the `GU-headlines` folder (on Windows, in WSL):

**Step 1.** Get the newest version, and keep a copy of your settings:

```sh
git pull
mkdir -p backups && cp .env backups/env-before-update
```

**Step 2.** Make a new `.env` from the template, with your values in it:

```sh
python3 - <<'EOF'
import re
from pathlib import Path

old = {}
for line in Path("backups/env-before-update").read_text(encoding="utf-8").splitlines():
    m = re.match(r"\s*([A-Z][A-Z0-9_]*)\s*=(.*)$", line)
    if m:
        old[m.group(1)] = m.group(2).strip()

lines, copied = [], []
for line in Path(".env.example").read_text(encoding="utf-8").splitlines():
    m = re.match(r"(# )?([A-Z][A-Z0-9_]*)=", line)
    if m and m.group(2) in old:
        line = m.group(2) + "=" + old[m.group(2)]
        copied.append(m.group(2))
    lines.append(line)

extra = [name for name in old if name not in copied]
if extra:
    lines += ["", "# From the old .env. The template does not list these names."]
    lines += [name + "=" + old[name] for name in extra]

Path(".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
print("Copied your values for:", ", ".join(copied))
print("Not in the template (kept at the end):", ", ".join(extra) or "none")
EOF
```

**Step 3.** Make sure that each of your values is still there:

```sh
diff <(tr -d '\r' < backups/env-before-update | grep -E '^[A-Z]' | sort) <(grep -E '^[A-Z]' .env | sort)
```

A line that starts with `>` is a setting that your old file did not have.
It has the default value, so nothing changes. A line that starts with `<`
is a value from your old file that is now different. You must see no `<`
lines. If you see one, put your old file back with
`cp backups/env-before-update .env`, and copy the notes by hand.

**Step 4.** Apply it with `docker compose up -d`. The site works as before.

Git ignores the `backups` folder, so the copy of your password stays on this
computer. Keep the copy with your other backups, or delete it.

---

## Web address and https

Visitors reach the site through **Caddy**, a free web server that runs as the
fourth container (`caddy`) and passes each request on to the website. Caddy
also gets the certificate that https needs from Let's Encrypt, and renews it
by itself. [Putting it on the internet](maintenance.md#putting-it-on-the-internet)
has the steps.

| Setting | Default | What it does |
|---|---|---|
| `SITE_DOMAIN` | *(empty)* | The site's domain name, such as `guamheadlines.com` (no `https://`, no `/`). Empty: plain `http://` on port 80, at any address that reaches the server, such as its IP address. Set: `https://` for that name with a free certificate, and `http://` visits are sent on to `https://`. For two names, put a comma between them: `guamheadlines.com, www.guamheadlines.com`. Each name must already point at the server, or Caddy can't get its certificate. |
| `HTTP_PORT` | `80` | The port for `http://`. With 80 the address needs no port: `http://localhost`. On a computer where another program already uses port 80, use another number such as `8000` and open `http://localhost:8000`. Keep 80 on a server with a domain name. |
| `HTTPS_PORT` | `443` | The port for `https://`. Keep 443 on a server with a domain name. Change it only on a computer that doesn't use https, where another program already uses 443. |

`HTTP_PORT` and `HTTPS_PORT` are read only by Docker Compose. Inside Docker,
Caddy always uses ports 80 and 443, and the website always uses port 8000.
The website itself isn't reachable from outside Docker, so every visit goes
through Caddy.

---

## Website

| Setting | Default | What it does |
|---|---|---|
| `SITE_NAME` | `GU Headlines` | The name in the top bar, browser tab and footer. |
| `SITE_TAGLINE` | `Guam news, updated every hour` | The short line in the footer and in the description search engines show. If you change how often the site checks for news, update this too. |
| `SITE_URL` | *(empty)* | The site's public address, such as `https://news.example.com`, once it is [on the internet](maintenance.md#putting-it-on-the-internet). Search engines are given this address for every page (in each page's canonical link, link previews and the sitemap), so a page is never listed twice under two addresses. Empty: `https://` and the first name in `SITE_DOMAIN`, or, without `SITE_DOMAIN`, the address each visitor used. Set it only to pick a different main address, such as `https://www.guamheadlines.com`. |
| `GOOGLE_SITE_VERIFICATION` | *(empty)* | Proves to [Google Search Console](https://search.google.com/search-console) that the site is yours: choose **URL prefix**, then **HTML tag**, and paste only the `content="..."` value here. See [Search engines](maintenance.md#search-engines). |
| `BING_SITE_VERIFICATION` | *(empty)* | The same for [Bing Webmaster Tools](https://www.bing.com/webmasters) (its **HTML Meta Tag** option). |
| `PAGE_SIZE` | `30` | Stories per page before the **Older →** link. Anything from 5 to 100. |

**Example: rename the site**

```
SITE_NAME=Island Headlines
SITE_TAGLINE=Every Guam story, every hour
```

The picture shown when someone shares a link to the site (on Facebook,
Messenger, X and in text messages) is
`guheadlines/web/static/social-card.jpg`, and it says "GU Headlines". After
renaming the site, replace it with your own 1200 × 630 JPEG of the same name,
then run `docker compose up -d --build`.

## Advertising

The home page, the Jobs page, the Sources page and the Obituaries page can show
one Google AdSense ad at the top of the right-hand column. To show it on
another topic's page too, add `show_ad: true` to that topic in
`config/categories.yaml` ([Topics](topics.md#separate-topics-obituaries)).
All these pages use the same ad unit. On a phone, the column moves below the
stories, so the ad comes after them. Until you set it up, the site shows no ad
and no ad code.

| Setting | Default | What it does |
|---|---|---|
| `ADSENSE_CLIENT` | *(empty)* | Your AdSense publisher ID, such as `ca-pub-1234567890123456` (`pub-1234567890123456` also works). You can also paste the ads.txt line or the verification code from AdSense: the site takes the ID out of it. With only this setting, the site can be verified, but it shows no ad. |
| `ADSENSE_SLOT` | *(empty)* | The ad unit's number, such as `9876543210`. With `ADSENSE_CLIENT`, the ad block is shown. |

**How to set it up**

1. In [AdSense](https://adsense.google.com), add the site. When AdSense asks
   how to verify it, choose the **meta tag**. Put your publisher ID in `.env`
   as `ADSENSE_CLIENT` and run `docker compose up -d`. The site then has the
   tag, and it also serves the `ads.txt` file that AdSense checks for.
2. Wait for AdSense to approve the site.
3. In AdSense, go to **Ads** › **By ad unit** and make a **Display ads** unit
   (responsive). AdSense shows its ad code. Copy only two values from it:

   ```html
   <ins class="adsbygoogle"
        data-ad-client="ca-pub-1234567890123456"   ← ADSENSE_CLIENT
        data-ad-slot="9876543210"                  ← ADSENSE_SLOT
   ```

   Do not paste the whole code anywhere. The site writes the ad code itself,
   because the security rules block code that is pasted in (see below).
4. Add the slot to `.env` and run `docker compose up -d`:

   ```
   ADSENSE_CLIENT=ca-pub-1234567890123456
   ADSENSE_SLOT=9876543210
   ```

   A new ad unit can stay empty for up to an hour. When Google has no ad to
   show, the block folds away.

**If AdSense says that it found no ads.txt file**

Open `https://your-domain/ads.txt` in a browser (put your own domain in).

- If it shows one line, such as
  `google.com, pub-1234567890123456, DIRECT, f08c47fec0942fa0`, with your
  publisher ID, the site is correct. AdSense can take a few days to see a
  new ads.txt file. You do not have to do anything.
- If it shows the "Not found" page, the site has no publisher ID. Put it in
  `.env` as `ADSENSE_CLIENT` and run `docker compose up -d` in the
  GU Headlines folder. The automatic update does not read a changed `.env`
  until the next update, so run the command yourself. Then open the address
  again.

**What changes when ads are on**
- The pages with the ad get a less strict Content-Security-Policy, which
  Google supports for AdSense: the AdSense code is allowed by a random value
  (a nonce) that changes on every page, and it can then load the other code,
  frames and requests that ads need. All other pages keep the strict policy.
- Google's ad code loads when the visitor first scrolls, taps, presses a key
  or moves the mouse, not with the page. The code is large, so the page is
  ready sooner without it. Speed tests such as
  [PageSpeed Insights](https://pagespeed.web.dev) never scroll or tap, so they
  measure the page without the ad. On a phone the ad is below the stories, so
  visitors scroll to it anyway. The block keeps room for the ad until it
  comes in, and is taken away when an ad blocker stops it.
- `robots.txt` lets the AdSense crawler (`Mediapartners-Google`) read every
  page, so that it can choose ads that fit.
- The disclaimer on the Sources page gets a paragraph about Google's
  advertising cookies and how to opt out. The AdSense program policies require
  this notice.
- AdSense requires a consent message for visitors from the European Economic
  Area, the UK and Switzerland. Turn on **Privacy & messaging** › **European
  regulations** in AdSense. Google shows the message, and the site needs no
  change.

## Sponsor spot

Besides AdSense, you can sell one ad spot yourself, for example to a local
business, and keep all of the money. The sponsor's picture is shown at the top
of the right-hand column of every page, under the word "Sponsor", above the
AdSense ad. On a phone, the column comes after the stories. A click on the
picture opens the sponsor's website in a new tab. Until you set it up, there
is no sponsor spot.

| Setting | Default | What it does |
|---|---|---|
| `SPONSOR_IMAGE` | *(empty)* | The file name of the sponsor's picture in the `config/sponsor` folder, such as `ad.jpg`. A JPG, PNG, WebP or GIF file (a GIF shows only its first picture). Empty: no sponsor spot. |
| `SPONSOR_LINK` | *(empty)* | The web address that the picture opens, starting with `https://`. Empty: the picture has no link. |
| `SPONSOR_ALT` | *(empty)* | What the picture says, in a few words, for people who cannot see it and for search engines. Empty: "Sponsor: " and the name of the sponsor's website. |

**How to set it up**

1. Get the picture from the sponsor: a JPG or PNG file. The best size is 600
   pixels wide and 500 pixels high, the usual "300 x 250" ad at twice the
   size, for sharp phone screens. Other sizes work too: the site shows the
   picture as wide as the column (300 pixels), or smaller if the picture is
   smaller. Give the file a short name without spaces, such as `ad.jpg`.
2. Copy the file to the `config/sponsor` folder in the GU Headlines folder on
   your server. From Windows, in PowerShell, in the folder with the picture
   (with your own user name, server address and folder; `pwd` in the
   GU Headlines folder on the server shows its full name):

   ```
   scp ad.jpg root@203.0.113.10:/root/GU-headlines/config/sponsor/
   ```

   A file-copy program such as WinSCP or FileZilla (SFTP) also works, with
   the same address, user name and password as ssh. Git ignores the pictures
   in this folder, so the automatic update never changes or removes them.
3. Add the settings to `.env` and run `docker compose up -d`:

   ```
   SPONSOR_IMAGE=ad.jpg
   SPONSOR_LINK=https://www.example.com/specials
   SPONSOR_ALT=Island Coffee: two iced coffees for 5 dollars, Hagatna
   ```

4. Open your site: the picture is at the top of the right-hand column.

To change the picture, copy the new file over the old one with the same name.
It shows within a few minutes, without a restart. To stop the ad, make
`SPONSOR_IMAGE` empty and run `docker compose up -d`.

**If the picture does not show**

Run `docker compose logs web | grep SPONSOR`. The lines say what is wrong:

- "there is no file": the name in `SPONSOR_IMAGE` and the file name are not
  the same (capitals count: `Ad.JPG` is not `ad.jpg`), or the file is not in
  `config/sponsor`.
- "not a picture that the site can read": the file is not a JPG, PNG, WebP or
  GIF picture, even if its name ends that way. Ask the sponsor for a JPG or PNG.
- "Permission denied": the website cannot read the file. In the GU Headlines
  folder, run `chmod 644 config/sponsor/*`.

No line at all: check that `SPONSOR_IMAGE` has only the file name (with
`.jpg`, `.png`, `.webp` or `.gif` at the end), and that you ran
`docker compose up -d` after you changed `.env`.

**What it does**
- The site serves the picture itself, from its own address
  (`/sponsor.webp`): no outside script, cookie or request. The security rules
  (Content-Security-Policy) do not change, and neither do the PageSpeed scores.
- The site makes a small WebP copy of the picture, at most 600 pixels wide, so
  a big file from the sponsor does not slow the pages down. The page gives the
  picture's size, so nothing jumps when it comes in.
- With the [visitor stats](#visitor-stats) on, the link first opens
  `/go/sponsor` on your site, which counts the click and sends the reader on
  to the sponsor at once. The stats page then shows a **Sponsor spot** part:
  the page views in the period (the picture is on every page), the sponsor
  clicks, and the clicks per 1,000 views. These are numbers to give the
  sponsor. Search engines are told not to follow the link (`rel="sponsored"`,
  as Google asks for paid links).
- The disclaimer on the Sources page gets a paragraph that says that the
  picture is a paid ad and not an endorsement.

## Visitor stats

GU Headlines can count page views and story clicks, and show them on a private
page of your site, `/stats`, behind a password. The page shows the totals for
today, the last 7, 30 or 90 days or the last year, a chart for each day, the
websites that readers come from, the stories readers opened most, the clicks
for each news source, the views and clicks for each topic, and the most viewed
pages. At the bottom, **Source health** shows how each news site, job board
and event calendar is doing (see [Source health](maintenance.md#source-health)).
Until you set a password, nothing is counted.

| Setting | Default | What it does |
|---|---|---|
| `STATS_PASSWORD` | *(empty)* | The password of the stats page. A password here also turns the counting on. Make a long random one with `openssl rand -hex 16`, and use only letters and digits. Empty: nothing is counted, there is no `/stats` page, and story links go straight to the news sites. |

**How to open the stats page**

1. Put the password in `.env` as `STATS_PASSWORD` and run `docker compose up -d`.
2. Open your domain followed by `/stats`, for example
   `https://guamheadlines.com/stats`.
3. The browser asks for a user name and a password. Type any user name (for
   example `stats`) and your password. The browser remembers it until you
   close the browser.

Open the page only over https (with `SITE_DOMAIN` set, see
[Web address and https](#web-address-and-https)). Over plain http the password
travels without protection.

**What is counted**

- A **page view** each time a person opens a page of the site. All the pages
  of a list count as one: page 2 of Latest counts as Latest. A search counts
  as Search, and the words searched for are not kept. Pages that are not found
  and the stats page itself are not counted.
- A **story click** each time a person follows a story's link (the headline,
  the photo or "Read the full story") to the news site.
- **Where readers come from**: for each visit that starts on your site, the
  website it came from, such as `google.com` or `facebook.com` (the phone
  copies `m.facebook.com` and `lm.facebook.com` count as `facebook.com`).
  Moving from one page of your site to another is not a new visit. **No
  website** means that the person typed the address, used a bookmark, or
  opened the link in an app that does not tell where it came from (many chat
  and email apps). The browser sends this information with each visit (the
  Referer header). Most sites send only their name, and the stats page keeps
  only that name, never the full address or search words.
- Search engines, link previews (Facebook, Discord, iMessage and others),
  speed tests such as PageSpeed, other robots, and pages that the browser
  loads before the person opens them are not counted. Your own visits are
  counted, like any other reader's.

**How it works, and what it keeps**

- The counting happens on your own server, so no script runs in the
  visitor's browser and the pages are as fast as before. No cookie is set,
  no outside service is used, and no API key is needed.
- The site keeps only totals: one number for each page, one for each story,
  and one for each website that sent visits, for each Guam day. It stores no IP address, no browser name and no
  other personal detail.
- While the stats are on, each story link first opens a short address on your
  own site, `/go/<number>`. It counts the click and sends the reader on to the
  story at once. It works only for stories that are in the database, so
  nobody can use it to send people to another site. `robots.txt` and
  `rel="nofollow"` tell search engines not to follow these links. The news
  sites still see your site as the place that sent the reader.
- The website adds up the counts in memory and writes them to the database
  every 30 seconds, and when it stops. A new view or click can take up to a
  minute to show on the stats page.
- The disclaimer on the Sources page gets a short paragraph that tells
  visitors about the counts.

## Newsletter feed

The worker makes a feed of the top stories for an email newsletter, at your
domain followed by `/feed.xml`. An email service such as SendFox reads it.
See [Newsletter feed](newsletter.md) for how the stories are picked and for
connecting SendFox step by step.

| Setting | Default | What it does |
|---|---|---|
| `NEWSLETTER_DAYS` | `tue,fri` | The days of the week when the worker makes a new edition of the feed. Day names in English, or their first three letters, with commas. Empty: no feed. |
| `NEWSLETTER_TIME` | `10:00` | The time of the edition, in Guam time, on a 24-hour clock. Make it about two hours before the email goes out. |
| `NEWSLETTER_STORIES` | `12` | The number of stories in each edition (1 to 50). |
| `SENDFOX_API_TOKEN` | *(empty)* | A SendFox API token. With it, every page has a newsletter sign-up box, and the site has a newsletter page (`/newsletter`). See [The sign-up box](newsletter.md#the-sign-up-box). Secret. |
| `SENDFOX_LIST` | *(empty)* | The SendFox list that new readers join: its name or number. Empty: the account's only list. |

## PayPal

The parts of the site that sell something take the money through PayPal. The
buyer pays on PayPal's own page (with a PayPal account, or with a card as a
guest), and PayPal then sends the buyer back to your site. The site shows no
PayPal button and loads no PayPal script: a plain link opens PayPal's page, so
the pages stay as fast and as strict as before.

| Setting | Default | What it does |
|---|---|---|
| `PAYPAL_CLIENT_ID` | *(empty)* | The Client ID of your app on [developer.paypal.com](https://developer.paypal.com) (Apps & Credentials). Empty: the site takes no payments. |
| `PAYPAL_CLIENT_SECRET` | *(empty)* | The Secret of the same app. Secret. |
| `PAYPAL_MODE` | `sandbox` | `sandbox` for test payments with pretend money, `live` for real payments. Sandbox and Live apps have different Client IDs and Secrets. |

The steps to make the app, to test with a pretend buyer, and to switch to real
payments are in `.env.example`, in the PayPal part. After a change, run
`docker compose up -d`, then check the values with:

```
docker compose exec web guheadlines paypal-check
```

**How a payment works.** The server makes a PayPal "order" for the price and
sends the buyer to PayPal's page with a link. After the buyer approves, PayPal
sends the buyer back to your site, and the server takes the money (it
"captures" the order). An order that is approved but never captured costs the
buyer nothing. Each step can be done again safely: the server asks PayPal to
do the same request only once. A refund gives all of a payment back.

## Sending email

The site sends email for the bid alerts: the alerts, the welcome email, and the
link to a subscriber's own page. It gives each email to an email service
through SMTP (the standard way that programs send email). The recommended
service is [Resend](https://resend.com): its free plan sends 3,000 emails a
month, at most 100 a day. Any other SMTP service works too.

| Setting | Default | What it does |
|---|---|---|
| `SMTP_HOST` | *(empty)* | The SMTP server, such as `smtp.resend.com`. Empty: no email is sent. |
| `SMTP_PORT` | `587` | `465` encrypts from the start; other ports (`587`, `2587`) switch to encryption first (STARTTLS). |
| `SMTP_USER` | *(empty)* | The user name. For Resend: `resend`. |
| `SMTP_PASSWORD` | *(empty)* | The password. For Resend: an API key (starts with `re_`). Secret. |
| `MAIL_FROM` | *(empty)* | The sender, such as `GU Headlines <alerts@guamheadlines.com>`, at a domain that the service has verified. Shown only in the emails. |

Send a test email with `docker compose exec web guheadlines mail-test
you@example.com`. The steps for Resend are in `.env.example` and in
[bid-alerts.md](bid-alerts.md#1-set-up-email-with-resend).

## Bid alerts

| Setting | Default | What it does |
|---|---|---|
| `BID_ALERTS_PRICE` | *(empty)* | The monthly price in US dollars, such as `15`. Empty: no bid alerts. |

Bid alerts are on only when the price, PayPal, email and the site's address
(`SITE_DOMAIN` or `SITE_URL`) are all set. `docker compose exec web
guheadlines bid-alerts` says what is missing, and lists the subscribers. See
[bid-alerts.md](bid-alerts.md).

## Checking for news

| Setting | Default | What it does |
|---|---|---|
| `SCRAPE_INTERVAL_MINUTES` | `60` | How often to check every news site. Checks happen on the clock: `60` means at the top of every hour, `30` at :00 and :30, `120` every other hour. The minimum is 5. Checking more often than every 30 minutes rarely finds more news and makes more requests to the news sites. |
| `SCRAPE_ON_START` | `true` | Also check immediately when GU Headlines starts, instead of waiting for the next scheduled check. |
| `MAX_NEW_PER_SOURCE` | `40` | The most new stories saved from one site in one round. Anything left over is picked up in a catch-up round (next setting). This keeps the first load and very busy days from hammering one site. |
| `CATCH_UP_SECONDS` | `60` | When a site isn't fully loaded (it had more stories than one round takes, or it said "too many requests"), come back for that site after this many seconds, and keep coming back until it's done. Then it returns to the regular schedule. The minimum is 30. If a site asks for a longer pause, that is honored (up to 10 minutes). |
| `JOBS_INTERVAL_HOURS` | `6` | How often to read the job boards for the [Jobs page](jobs.md), in hours. They are also read when GU Headlines starts. |
| `USAJOBS_API_KEY`, `USAJOBS_EMAIL` | (empty) | A free key for the USAJOBS job board and the email address it was sent to. Without them that board is skipped. See [Jobs](jobs.md#usajobs). |
| `EVENTS_INTERVAL_HOURS` | `3` | How often to read the event calendars for the [Events page](events.md), in hours. They are also read when GU Headlines starts. |
| `BIDS_INTERVAL_HOURS` | `6` | How often to read the government bid lists for the [Bids page](bids.md), in hours. They are also read when GU Headlines starts. |
| `GOOGLE_CALENDAR_API_KEY` | (empty) | A free Google key for reading the Guam Legislature's calendar (Google Calendar). Without it that calendar is skipped; the Legislature's session agendas are still listed. See [Events](events.md#google-calendar-key). |
| `PER_HOST_DELAY` | `1.5` | Seconds to wait between two requests to the same news site. Higher is gentler. Individual sites can be slowed down further in `sources.yaml` (`request_delay`). |
| `SCRAPER_WORKERS` | `4` | How many news sites are checked at the same time. |
| `REQUEST_TIMEOUT` | `25` | Seconds to wait for a news site to answer before giving up (it will try again later). |

**Example: check every 30 minutes**

```
SCRAPE_INTERVAL_MINUTES=30
SITE_TAGLINE=Guam news, updated every 30 minutes
```

## Weather

The worker also reads the weather and the tides for Guam from three free
United States government services. They need no key.

- The **National Weather Service** (NWS) office in Tiyan
  ([weather.gov/gum](https://www.weather.gov/gum/)): the watches, warnings and
  advisories for Guam and the Guam coastal waters, the forecast for Hagåtña
  (for the week and for each hour), the latest reading at Guam International
  Airport, the Surf Zone Forecast (rip currents, surf height, water
  temperature) and the Coastal Waters Forecast (wind and seas).
- The **Joint Typhoon Warning Center** (JTWC): the tropical storms and
  typhoons that it has warnings for in the western North Pacific.
- **NOAA Tides & Currents**
  ([tidesandcurrents.noaa.gov](https://tidesandcurrents.noaa.gov/)): the times
  and heights of the high and low tides at one tide station, for the next
  month.

| Setting | Default | What it does |
|---|---|---|
| `WEATHER_INTERVAL_MINUTES` | `10` | How often to read the weather, in minutes. The minimum is 5. `0` turns the weather off: no banner, no weather box and no Weather page. |
| `TIDE_STATION` | `1630000` | The NOAA tide station whose tides are shown. `1630000` is Apra Harbor. Empty: no tides. To find another station's ID, open [NOAA's tide predictions](https://tidesandcurrents.noaa.gov/tide_predictions.html), find Guam, and copy the station's number. Tides are worked out years ahead, so they are read only every 6 hours (and at once after you change the station). |

What the site shows:

- **A banner at the top of every page** while NWS has a watch or a warning for
  Guam (a Typhoon Warning, a Tropical Storm Watch, a Flood Watch, a Tsunami
  Warning and so on). Warnings are red and watches are amber. A visitor opens
  the banner to read the full NWS text.
- **A weather box at the top of the home page**: the temperature, how hot it
  feels, the humidity and the wind at the airport, the next four forecast
  periods (two on phones), the storm list, and the advisories and statements
  (a Small Craft Advisory, a Rip Current Statement), which are not in the
  banner, and today's high and low tides.
- **The Weather page** (`/weather`, the **Weather** link in the top bar): all
  of it in more detail, with the next 24 hours, the week, the surf and the
  sea, sunrise and sunset, and the tides for a day, a week or a month (see
  [The Weather page](using-the-website.md#the-weather-page)). Sunrise,
  sunset and the moon's phase are worked out by the site itself, with no
  service.

If a service cannot be reached, the site keeps what it got last. A warning
stays in the banner until its end time. The reading, the forecasts and the
storm list are left out when they are more than 6 hours old (18 hours for the
surf and coastal waters forecasts, which NWS writes twice a day). The tides
are used as long as the saved month lasts.

To read the weather and the tides now and see what each service gave:

```
docker compose exec worker guheadlines weather
```

## Outages and gas prices

The **Outages page** (`/outages`, the **Outages** link in the top bar) lists
power and water outages and boil-water notices (see
[The Outages page](using-the-website.md#the-outages-page)). The worker reads
three public pages. None needs a key:

- **Power**: the Guam Power Authority's outage notices on GuamALERTS
  ([web.guamalerts.com/notification/gpa](https://web.guamalerts.com/notification/gpa)),
  the island's official alert service, which GPA's website links to.
- **Water**: the Guam Waterworks Authority's outage updates
  ([bloggwapio.blogspot.com](https://bloggwapio.blogspot.com/)), which GWA's
  website links to as "Current Outages". The site reads its RSS feed.
- **Boil-water notices**: from GWA's updates and from Guam EPA's news feed
  ([epa.guam.gov](https://epa.guam.gov/)).

If a page cannot be reached, the site keeps the notices it already has, and
after 6 hours says when it last reached that agency.

**Gas prices** in the sidebar come from the news. No agency or company
publishes Guam's pump prices in a form a program may read (the Guam Energy
Office stopped its price survey in 2024, and GasBuddy does not allow automated
collection), and prices are the same at every station. So the site reads the
regular, premium and diesel prices from the headlines and intros of the news
stories it already collects, such as "Gas prices up again: $5.94 a gallon of
regular", and links each price to its story. It reads a price only when it is
next to its grade's name, and skips sentences about other places (the
mainland, Saipan, Hawaii) and old prices ("up from $4.88"). If no recent story
gives a price, the sidebar shows none.

| Setting | Default | What it does |
|---|---|---|
| `OUTAGES_INTERVAL_MINUTES` | `15` | How often to read the outage notices, in minutes. The minimum is 5. `0` turns the Outages page and its link off. |
| `GAS_PRICE_DAYS` | `45` | How many days back to look for a news story that gives the gas prices. Older prices are not shown. `0` turns the gas prices off. The maximum is 365. |

To read the outage notices now and see what each page gave:

```
docker compose exec worker guheadlines outages
```

## Being a good visitor

| Setting | Default | What it does |
|---|---|---|
| `RESPECT_ROBOTS_TXT` | `true` | Obey each site's `robots.txt` file, where sites say which pages robots may read. Keep this on. To make an exception for one site, use `ignore_robots: true` on that site in `sources.yaml` instead (see [News sources](news-sources.md#robotstxt)). |
| `SCRAPER_USER_AGENT` | `Mozilla/5.0 (compatible; GUHeadlinesBot/1.0)` | How GU Headlines introduces itself to news sites. It's good manners to include a way to reach you, so a site owner can ask you something instead of blocking you. |

**Example: add your contact details**

```
SCRAPER_USER_AGENT=Mozilla/5.0 (compatible; GUHeadlinesBot/1.0; +mailto:you@example.com)
```

## Photos

| Setting | Default | What it does |
|---|---|---|
| `STORE_IMAGES` | `true` | Save a copy of each story's photo on your server, so old stories keep their pictures even after the news site deletes them. With `false`, the photo is loaded from the news site each time a page is viewed. |
| `IMAGE_MAX_WIDTH` | `720` | Width in pixels of the larger saved photos (the ones phones load). 720 looks sharp on the site, including on phones. Larger means bigger files. This only affects photos saved from now on. |
| `IMAGE_STORAGE_GB` | `5` | The most disk space the saved photos may use, in GB. Above it, the worker deletes the photos of the oldest stories until the photos use 90% of it; those stories then show the news site's own photo. Decimals are fine (`2.5`). `0`: no limit. |

Saved photos are stored in Docker's `media` storage. Each photo is saved
twice: a copy for phones (about 30 to 60 KB) and a small copy, 360 pixels
wide, for computers and tablets, where the photo is drawn 228 pixels wide
(about 10 to 20 KB). Each browser loads only the copy it needs. Photos saved
before small copies existed get theirs from the worker, a few thousand after
each news check, newest stories first. If a story has no saved copy (the
photo was never saved, was deleted to stay within `IMAGE_STORAGE_GB`, or the
`media` storage was not moved along with the database), the site shows the
news site's own photo instead.

To see how much space the photos use, and tidy them up at once:

```sh
docker compose exec worker guheadlines photos
```

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

## Alerts

| Setting | Default | What it does |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | (empty) | The secret webhook link of a Discord channel. Alerts are posted there: a news site, job board or event calendar that stopped working, a news site that went quiet, new stories with your keywords, the site not answering, automatic updates. Empty: no alerts. See [Alerts in Discord](maintenance.md#alerts-in-discord). |
| `ALERT_AFTER_HOURS` | `24` | How many hours a news site, job board or event calendar must fail before its alert is posted. The minimum is 1. |
| `ALERT_KEYWORDS` | (empty) | Words or phrases, with a comma between them, such as `typhoon, shooting, power outage`. A new story whose headline or intro mentions one is posted in Discord. Empty: no keyword alerts. See [Keyword alerts](maintenance.md#keyword-alerts). |

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
  starting `:root {`, lists every color of **dark mode** (the default) with a
  name, for example `--fill: #3d5a7d;` (the blue of the buttons and the
  current section). The second block, starting `:root[data-theme="light"] {`,
  holds the same colors for **light mode**, such as `--bg: #eeebe3;` (the
  paper-colored page background). Colors are written as hex codes; any "color picker" website
  gives you one.
- **Logo:** the "GU" square is in `guheadlines/web/templates/base.html`
  (`<span class="brand-mark">GU</span>`). Change the letters there.
- **Browser tab icon:** `guheadlines/web/static/favicon.svg`. Phones and
  some browsers use `favicon.ico` and `apple-touch-icon.png` instead, and
  link previews use `social-card.jpg`. Change those images too.

Apply with:

```sh
docker compose up -d --build
```

Your browser may keep showing the old colors for a minute; press
**Ctrl+Shift+R** (**Cmd+Shift+R** on Mac) to reload fully.
