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

The home page, the Jobs page, the Sources page, the Obituaries page and the
[visitor guides](guides.md#ads-in-the-guides) can show one Google AdSense ad at
the top of the right-hand column. To show it on
another topic's page too, add `show_ad: true` to that topic in
`config/categories.yaml` ([Topics](topics.md#separate-topics-obituaries)).
All these pages use the same ad unit. On a phone, the column moves below the
stories, so the ad comes after them. Until you set it up, the site shows no ad
and no ad code.

| Setting | Default | What it does |
|---|---|---|
| `ADSENSE_CLIENT` | *(empty)* | Your AdSense publisher ID, such as `ca-pub-1234567890123456` (`pub-1234567890123456` also works). You can also paste the ads.txt line or the verification code from AdSense: the site takes the ID out of it. With only this setting, the site can be verified, but it shows no ad. |
| `ADSENSE_SLOT` | *(empty)* | The ad unit's number, such as `9876543210`. With `ADSENSE_CLIENT`, the ad block is shown. |
| `ADSENSE_STORIES_SLOT` | *(empty)* | The number of a second ad unit, such as `1122334455`, shown between the stories (see [The ad between the stories](#the-ad-between-the-stories)), and inside each visitor guide with three or more parts, after its first part. It needs `ADSENSE_CLIENT` and `ADSENSE_SLOT` too. |

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

**The ad between the stories**

The home page and the Obituaries page can show a second ad, between the
stories: after the fifth story, when more stories follow. On a phone, the
right-hand column comes after all the stories, so this is the ad that phone
readers see first. It uses its own ad unit, so the AdSense reports show how
much each spot earns (**Reports** › **By ad unit**).

1. In AdSense, go to **Ads** › **By ad unit** › **Display ads**. Name the new
   unit, for example *Between stories*, keep **Responsive**, and click
   **Create**.
2. Copy only the number after `data-ad-slot=` from the ad code.
3. Add it to `.env` and run `docker compose up -d`:

   ```
   ADSENSE_STORIES_SLOT=1122334455
   ```

The page queues this ad unit, and the AdSense code that the right-hand column
loads on the first scroll, tap, key press or mouse move fills both units. So
the PageSpeed scores do not change. The block keeps room for the ad, and folds
away when Google has no ad to show or an ad blocker stops the code.

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

## Coffee donations

Readers who like the site can send you a little money, "a coffee", through
PayPal. With `PAYPAL_DONATE_LINK` set, every page shows a **Support this
site** box in the right-hand column, under the newsletter box, with a **Buy me
a coffee** button. The button opens your PayPal page in a new tab, where the
reader picks the amount and pays with PayPal or a card. Until you set it up,
there is no box.

| Setting | Default | What it does |
|---|---|---|
| `PAYPAL_DONATE_LINK` | *(empty)* | Your PayPal.Me link (such as `https://paypal.me/GuamHeadlines`) or PayPal Donate button link (`https://www.paypal.com/donate/?hosted_button_id=...`). Only links on paypal.me or paypal.com are used, and `https://` can be left out. A link with an email address in it is not used. Empty: no box. |

**How to set it up**

Use a PayPal.Me link if you are not sure. It works with a personal account.

1. Open [paypal.com/paypalme](https://www.paypal.com/paypalme), log in, and
   choose a name for your link, such as *GuamHeadlines*. Readers see this name
   and the name and picture of your PayPal account, but not your email
   address.
2. Copy the link that PayPal shows, such as `https://paypal.me/GuamHeadlines`.
3. Add it to `.env` and run `docker compose up -d`:

   ```
   PAYPAL_DONATE_LINK=https://paypal.me/GuamHeadlines
   ```

4. Open your site and click **Buy me a coffee**: your PayPal page opens.

A Donate button link also works: make a button at
[paypal.com/donate/buttons](https://www.paypal.com/donate/buttons) and copy
only its link, not its button code. With a personal account, PayPal can put
your email address in that link (`business=you@example.com`). The site does
not use such a link, because it never shows an email address: use PayPal.Me
then.

**What it does**
- The button is a plain link: no PayPal script, cookie or request on the site.
  The security rules and the PageSpeed scores do not change.
- The disclaimer on the Sources page gets a paragraph that says that the money
  is a personal gift to the person who runs the site, not a donation to a
  charity, so it is not tax-deductible.
- PayPal keeps a small fee from each payment. The money is income. Ask a tax
  preparer if you must report it.

## Affiliate links

An affiliate link is a link to a booking website with your partner ID in it.
When a reader books through it, the booking website pays you a commission, and
the reader pays the same price. With one or more of the settings below, the
Weather page, the Events page and each event's own page show a **Plan a
trip** box in the right-hand column, with one line for each link that you set.
Until you set one, there is no box.

| Setting | Default | Shown as |
|---|---|---|
| `AFFILIATE_HOTELS_LINK` | *(empty)* | Hotels |
| `AFFILIATE_FLIGHTS_LINK` | *(empty)* | Flights |
| `AFFILIATE_TOURS_LINK` | *(empty)* | Tours and activities |
| `AFFILIATE_CARS_LINK` | *(empty)* | Car rentals |

Each value is a full link that starts with `https://`. A value that is not a
link is not used.

**How to set it up**

1. Join an affiliate program that pays through PayPal (Stripe does not work
   for Guam addresses). For example,
   [Travelpayouts](https://www.travelpayouts.com) has hotels, flights, tours
   and car rentals in one account, and pays to PayPal once a month from 50 US
   dollars. [Viator](https://partnerresources.viator.com) has tours and pays to
   PayPal every week. Read their current terms first: terms change. Sign up
   with the same email address as your PayPal account.
2. In the program's dashboard, make a link (a "deep link" or "tracking link")
   to the page that readers must see, such as the hotels in Guam. Copy the
   full link. Do not copy a banner code or a script: the security rules block
   code that is pasted in.
3. Add the links to `.env` and run `docker compose up -d`:

   ```
   AFFILIATE_HOTELS_LINK=https://www.example.com/hotels/guam?aid=123456
   AFFILIATE_TOURS_LINK=https://www.example.com/guam/tours?pid=123456
   ```

4. Open `/weather` on your site and click each line in the **Plan a trip**
   box. The program's dashboard shows the clicks and bookings.

**What it does**
- Under the links, a short note tells readers that they are affiliate links
  and that the site can earn a commission. The US Federal Trade Commission
  requires this note. The disclaimer on the Sources page gets a paragraph
  about them too.
- The links have `rel="sponsored"`, which tells search engines that they are
  paid links, as Google asks.
- They are plain links: no script from the booking website. The security rules
  and the PageSpeed scores do not change.

## Visitor stats

GU Headlines can count page views and story clicks, and show them on a private
page of your site, `/stats`, behind a password. The page shows the totals for
today, the last 7, 30 or 90 days or the last year, a chart for each day, the
websites that readers come from, the stories readers opened most, the clicks
for each news source, the views and clicks for each topic, and the most viewed
pages. At the bottom, **Source health** shows how each news site, job board
and event calendar is doing (see [Source health](maintenance.md#source-health)),
and **Backups** lists the nightly backups, to download (see
[Backups](maintenance.md#backups)).
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

After 10 wrong passwords within 15 minutes from one address, that address is
blocked for an hour (even with the right password then), and Discord gets an
alert. If that happens to you, wait an hour. See
[Keeping the site safe](security.md).

The same password opens the visitor guides editor, `/admin/guides` (see
[Visitor guides](guides.md)), and the review pages of paid listings and booked
ads.

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

The parts of the site that sell something (paid listings, ads, bid alerts and
the [guide packs](guides.md#selling-guides) of the visitor guides) take the
money through PayPal. The buyer pays on PayPal's own page (with a PayPal account, or with a card as a
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

## Paid listings

People and businesses can pay through PayPal to put a listing on the site:

- **Classified ads** on the Classifieds page (`/classifieds`): for sale, cars
  and trucks, housing, services and wanted.
- **Featured job posts**, pinned at the top of the Jobs page.
- **Featured events**, at the top of the Events page, in gold on the month
  calendar, on the home page, in the calendar feed (`/events.ics`) and with an
  event page of their own, until the event is over.
- **Business directory** (`/directory`): a basic entry is free; the featured
  upgrade adds a photo and a website link and puts the entry at the top.
- **Press releases** (`/press`): news that a business or group writes about
  itself, with a page of its own (`/press/<number>`), marked "Sponsored". It
  stays on the site, and goes out once in the [newsletter](newsletter.md): in
  the next edition after you approve it, after the third story, titled
  "Press release:".
- **Deals and offers** (`/deals`): a coupon or special, with a coupon code if
  it has one, also shown in turns in a "Deals and offers" box at the top of
  the home page. The business picks the last day (up to 8 weeks ahead) and
  pays `DEAL_WEEK_PRICE` for each week, or part of a week, from the day it
  sends the form. A renewal adds one week.

Nobody needs an account. The form leads to a private page for the listing (its
address has a secret part), with a "Pay with PayPal" link. Each listing then
waits for you on `/admin/listings` (the same password as `/stats`). Approve
puts it on the site; reject gives the money back through PayPal and shows your
reason on the poster's page; take down removes a live listing. With
`DISCORD_WEBHOOK_URL` set, Discord tells you when a listing waits. Unpaid
listings are deleted after a week. Paid links carry `rel="sponsored"` for
search engines, and the Sources page tells visitors that these listings are
paid.

The listings need `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` and
`STATS_PASSWORD`. Without them, the pages and the "Post" buttons do not show.
No new database table: press releases and deals are kept with the other
paid listings.

| Setting | Default | What it does |
|---|---|---|
| `CLASSIFIED_PRICE` | `5` | US dollars for a classified ad. `0`: no Classifieds page. |
| `CLASSIFIED_DAYS` | `30` | Days a classified ad shows after you approve it (1 to 365). |
| `FEATURED_JOB_PRICE` | `25` | US dollars for a featured job post. `0`: none offered. |
| `FEATURED_JOB_DAYS` | `7` | Days a featured job post stays pinned (1 to 365). |
| `FEATURED_EVENT_PRICE` | `15` | US dollars for a featured event, shown until it is over. `0`: none offered. |
| `BUSINESS_UPGRADE_PRICE` | `50` | US dollars for the featured upgrade of a directory entry. `0`: no directory. |
| `BUSINESS_UPGRADE_DAYS` | `365` | Days the upgrade lasts (1 to 3650); then the entry stays as a basic one. |
| `PRESS_RELEASE_PRICE` | `25` | US dollars for a press release, which stays on the site. `0`: no Press releases page. |
| `DEAL_WEEK_PRICE` | `10` | US dollars for each week a deal shows. `0`: no Deals page. |

Write prices without the `$` sign (Docker Compose changes values that contain
it). A new price applies to listings sent after `docker compose up -d`.

Listings end by themselves after their number of days. A few days before,
the poster gets an email, and can renew from the listing's own page: see
[Renewal reminders](#renewal-reminders).

## Advertise page

The Advertise page (`/advertise`) lets a local business book and pay for an
ad by itself, without writing to you first. It shows your real numbers from
the [visitor stats](#visitor-stats), what is for sale, and a booking form for
each thing that has a price:

- **The [sponsor spot](#sponsor-spot), by the week** (Monday to Sunday, Guam
  time). In a booked week, the business's picture takes the place of
  `SPONSOR_IMAGE`. In the other weeks, `SPONSOR_IMAGE` shows as before; with
  no `SPONSOR_IMAGE`, the spot shows a small "Your business here" box that
  links to the Advertise page.
- **A section sponsor, by the week**: a line such as "Weather brought to you
  by Island Bakery", with the business's short line and a link, at the top of
  every page of one section. The sections are the news topics in the top bar
  (from `config/categories.yaml`), Jobs, Events, Bids, and Weather and Outages
  while those pages are on. One business a section a week; the business picks
  the section on the Advertise page, which shows each section's page views in
  the last 30 days. Words only, so the pages stay as fast as before.
- **A sponsor line in the [newsletter](newsletter.md), by the edition**: one or
  two sentences and a link, as the first item of the edition, titled
  "Sponsor:" and the business's name.

| Setting | Default | What it does |
|---|---|---|
| `SPONSOR_WEEK_PRICE` | *(empty)* | The price of the sponsor spot for one week, in US dollars, such as `25` or `25.50` (no `$` sign). Empty: not sold online. |
| `SECTION_SPONSOR_PRICE` | *(empty)* | The price of one section for one week, in US dollars. Every section has the same price. Empty: sections are not sold. |
| `NEWSLETTER_SPONSOR_PRICE` | *(empty)* | The price of the sponsor line in one newsletter edition, in US dollars. Empty: not sold. Needs the newsletter feed (`NEWSLETTER_DAYS`). |

The page is on only when a price is set, [PayPal](#paypal) is set up
(`PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET`), and `STATS_PASSWORD` is set:
it opens your review page. Then every page has an **Advertise** link at the
bottom, and the page is in the sitemap. The step-by-step setup, with a test
booking, is in `.env.example`, in the "Advertise page" part.

**How a booking goes**

1. The business picks its weeks (the next 12 weeks, from next Monday) or its
   editions (the next 8 that are more than a day away), and fills in its name,
   its email address, its website, and its picture (sponsor spot) or its words
   (newsletter). A week or an edition can be booked by one business only. Its
   dates are held for it for an hour.
2. The site makes the booking's own page, with a secret address, and the
   business pays from there on PayPal's page (a PayPal account or a card).
   The money goes into your PayPal account at once.
3. You get a [Discord alert](#alerts) (with `DISCORD_WEBHOOK_URL` set). The ad
   does **not** show yet.
4. Open `/advertise/review` (any user name and your `STATS_PASSWORD`; the same
   login as the stats page). It lists the bookings, the ones waiting for you
   first, with the business's email address, its link, and its picture or
   words. Press **Approve**, and the ad shows in its weeks. Or write why in
   the box and press **Turn down and refund**: PayPal gives all of the money
   back, the business sees your reason on its page, and its dates are free
   again. If PayPal cannot make the refund, the page says so: refund it in
   PayPal (Activity, the payment, Issue a refund), then press **Turn down,
   refunded in PayPal**.
5. The business's page shows where its booking stands, and, once the ad has
   started, its page views and clicks.

Approve a paid ad before its first day, or it starts late. A booking that is
not paid within the hour is cancelled, and its dates are free again; if the
business pays on PayPal after that and its dates are still free, it keeps
them.

**What it does**
- No logins: the business's way back is the secret address of its booking's
  page. That page and the review page are never counted in the stats, never
  kept by browsers or search engines, and are left out of `robots.txt`.
- Plain forms, sent to your own site. The payment happens on PayPal's own page,
  opened with a link, so the pages load nothing from PayPal and the security
  rules and PageSpeed scores stay the same.
- The booked picture is checked (a JPG, PNG, WebP or GIF of up to 5 MB, at
  least 300 pixels wide, not a long strip) and stored as a small WebP copy, at
  most 600 pixels wide, in the database. It is served from `/sponsor.webp`,
  like `SPONSOR_IMAGE`.
- Clicks on a booked ad go through `/go/ad/<number>` and are counted for that
  booking (and, for the sponsor spot, in the stats page's Sponsor spot part).
  The links of the stories in the newsletter end in `?from=newsletter`, so the
  Advertise page can show how many story clicks the newsletter brings.
- An ad that names something in the `muted:` list of `config/sources.yaml` (as
  stories that are never shown do) cannot be booked.
- The Sources page's disclaimer gets a paragraph about the details a business
  gives when it books.
- A few days before an approved ad ends, the business gets an email, and its
  booking's page has a **Run this ad again** form: see
  [Renewal reminders](#renewal-reminders).

**The media kit**

The Advertise page links to a media kit: one page, as a PDF, at
`/advertise/media-kit.pdf`. It has your numbers from the last 30 days (page
views, a day and a week on average, newsletter and sponsor clicks, a chart of
each day, the most-read sections, and the websites that readers come from)
and the prices of everything that is for sale on the site (the Advertise page
and the [paid listings](#paid-listings)). Send it to businesses when you ask
them to advertise. It is made again from the newest numbers at most every 10
minutes, and it is there only while the Advertise page is on. Like the
Advertise page, it gives no email address or phone number: it tells
businesses to book and pay on the site. It is left out of search engines
(`robots.txt` and `X-Robots-Tag`). The review page links to it too.

## Your own email server

| Setting | Default | What it does |
|---|---|---|
| `COMPOSE_PROFILES` | *(empty)* | `mail` starts your own mail server and webmail (two more containers). Only Docker Compose reads it. |
| `MAIL_HOSTNAME` | *(empty)* | The mail server's name, such as `mail.guamheadlines.com`. Your addresses are name@guamheadlines.com, and the webmail is at `https://mail.guamheadlines.com`. |

With both set, you get email addresses at your domain, webmail, and the site
sends its own emails (bid alerts) from your server. It also needs DNS records
and reverse DNS from your server company. The whole setup, step by step, is in
[email-server.md](email-server.md); `./scripts/mail.sh` does most of the work.

## Sending email

The site sends email for the bid alerts (the alerts, the welcome email, and the
link to a subscriber's own page) and the
[renewal reminders](#renewal-reminders). It hands each email to a mail server
through SMTP (the standard way that programs send email). The mail server can run on
your own server or belong to an email company; its setup steps give the values.
With [your own email server](email-server.md), `./scripts/mail.sh setup` makes
the site's mailbox and prints all five values.

| Setting | Default | What it does |
|---|---|---|
| `SMTP_HOST` | *(empty)* | The mail server's name, such as `mail.guamheadlines.com`. Empty: no email is sent. |
| `SMTP_PORT` | `587` | `465` encrypts from the start; other ports (such as `587`) switch to encryption first (STARTTLS). The connection is always encrypted. |
| `SMTP_USER` | *(empty)* | The user name that the mail server asks for. Empty: no sign-in. |
| `SMTP_PASSWORD` | *(empty)* | The password that goes with `SMTP_USER`. Secret. |
| `MAIL_FROM` | *(empty)* | The sender, such as `GU Headlines <alerts@guamheadlines.com>`, at your domain. Shown only in the emails. |

Send a test email with `docker compose exec web guheadlines mail-test
you@example.com`. The steps are in `.env.example` and in
[bid-alerts.md](bid-alerts.md#1-set-up-sending-email).

## Bid alerts

| Setting | Default | What it does |
|---|---|---|
| `BID_ALERTS_PRICE` | *(empty)* | The monthly price in US dollars, such as `15`. Empty: no bid alerts. |
| `BID_AWARDS_FREE_DAYS` | `180` | How many days of the [award history](bids.md#award-history) every visitor sees (7 to 3650). Bid alert subscribers see all of it; with bid alerts off, everyone does. |

Bid alerts are on only when the price, PayPal, email and the site's address
(`SITE_DOMAIN` or `SITE_URL`) are all set. `docker compose exec web
guheadlines bid-alerts` says what is missing, and lists the subscribers. See
[bid-alerts.md](bid-alerts.md).

## Renewal reminders

| Setting | Default | What it does |
|---|---|---|
| `RENEWAL_REMINDER_DAYS` | `3` | How many days before a paid listing or a booked ad ends its buyer gets one email (0 to 30). `0`: no email. |

Paid listings and booked ads end by themselves: a classified ad and a featured
job post after their number of days, a business's featured upgrade after
`BUSINESS_UPGRADE_DAYS` (the entry then stays as a basic one), a deal after
its last day (a renewal adds one week), and a booked ad after its last week or
edition. A featured event shows until it is over, and a press release stays,
so they get no reminder.

`RENEWAL_REMINDER_DAYS` before the end, the worker emails the buyer a link to
their own page (it checks after each news check, every hour by default):

- **A listing** (classified ad, featured job post, business upgrade): the
  email goes to the address that the poster paid with on PayPal. The
  listing's page has **Renew for $… with PayPal**: PayPal takes the price
  that `.env` sets at that moment, and the listing gets as many days more,
  added to its end date (or to today, when it has ended already). It needs no
  new review. With `DISCORD_WEBHOOK_URL` set, Discord tells you about each
  renewal, and `/admin/listings` counts renewals in its 30-day total. If you
  take a listing down while its poster is on PayPal's page, the payment goes
  back by itself.
- **A booked ad**: the email goes to the address in the booking. The
  booking's page has **Run this ad again**, with the next free weeks or
  editions ticked. It makes a new booking with the same picture or words,
  paid and approved like any other. No email goes out when the business has
  already booked a later date.

Each end date gets one email; when the email fails, the next hour tries
again. Reminders need [sending email](#sending-email) and `SITE_DOMAIN` or
`SITE_URL` (for the link). Without them, listings and ads still end on time,
and buyers can still renew from their own page.

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
| `DISCORD_WEBHOOK_URL` | (empty) | The secret webhook link of a Discord channel. Alerts are posted there: a news site, job board or event calendar that stopped working, a news site that went quiet, new stories with your keywords, the site not answering, automatic updates, a nightly backup that failed, a backup downloaded from the stats page. Empty: no alerts. See [Alerts in Discord](maintenance.md#alerts-in-discord). |
| `ALERT_AFTER_HOURS` | `24` | How many hours a news site, job board or event calendar must fail before its alert is posted. The minimum is 1. |
| `ALERT_KEYWORDS` | (empty) | Words or phrases, with a comma between them, such as `typhoon, shooting, power outage`. A new story whose headline or intro mentions one is posted in Discord. Empty: no keyword alerts. See [Keyword alerts](maintenance.md#keyword-alerts). |
| `REPORT_TIME` | `8:00` | When the reports are posted in Discord, Guam time. See [Reports](maintenance.md#reports). |
| `DAILY_REPORT` | `true` | The money summary every morning: yesterday's payments, and what waits for your approval. Only while PayPal is set up. |
| `WEEKLY_REPORT_DAY` | `monday` | The day of the weekly report (readers, sign-ups and money of the 7 days before). `off`: no weekly report. |
| `DISK_ALERT_PERCENT` | `90` | Discord alert when the server's disk is this full, in percent. `0` turns it off. Read by `scripts/server-check.sh`; see [Server checks](maintenance.md#server-checks). |
| `MEMORY_ALERT_PERCENT` | `90` | Discord alert when this much of the memory, in percent, has been in use for about 15 minutes. `0` turns it off. |
| `CERT_ALERT_DAYS` | `14` | Discord alert when the site's (or the mail server's) https certificate ends within this many days. `0` turns it off. |

## Backups

The whole site is backed up every night by itself; see
[Backups](maintenance.md#backups).

| Setting | Default | What it does |
|---|---|---|
| `BACKUP_TIME` | `03:00` | When the nightly backup runs, in Guam time, on a 24-hour clock. Empty: no nightly backups (`./scripts/site-backup.sh now` still works). |
| `BACKUP_KEEP_DAYS` | `14` | How many days each backup is kept, from 1 to 365. The newest backup is never deleted. Make it smaller if the disk gets full. |

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
