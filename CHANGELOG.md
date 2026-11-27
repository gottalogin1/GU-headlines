# Changes

What changed in each release of GU Headlines. To update to the newest
release, see [Updating](docs/maintenance.md#updating).

## Unreleased

### Added
- **Found more easily in Google, Bing and AI assistants.** Every page's
  title, summary and heading now use the words people search for ("Guam
  news today", "Guam weather today: forecast, typhoons, tides and surf",
  "Guam jobs", "Guam events calendar", "Guam bids and RFPs", "Guam power
  outages and water outages today", "Guam obituaries"), and later pages of a
  list say which page they are. The home page names the newsrooms in its
  summary and ends with a short "About" section; the footer of every page
  links the Guam pages. Pages describe themselves to search engines in more
  detail: the site's name, logo and other pages (new setting
  `SOCIAL_PROFILES`), that every page is about Guam, the stories a page
  lists and when it last changed. `/llms.txt` is a plain-text summary of
  the site for AI assistants, and `robots.txt` names the AI crawlers that
  are welcome. The sitemap gives the Jobs, Events and Bids pages the time
  their newest item came in. How-to: `docs/maintenance.md#search-engines`.
- **Bing hears about new pages within minutes (IndexNow).** After each news
  check, the worker tells Bing (and Yandex, Naver and Seznam) which pages
  are new or changed: the home page, the topics and news sites with new
  stories, the archive day, new event pages, newly approved press releases,
  and Jobs, Bids and the guides when they change. Bing's results also feed
  DuckDuckGo, Yahoo, Copilot and the web searches of AI assistants. Nothing
  to sign up for; on by default once the site has a public address
  (`INDEXNOW=false` turns it off).
- **Guide packs: sell visitor guides through PayPal.** Set a guide to "Guide
  pack readers" and everyone else reads its opening (the text before its
  first heading), then sees the packs that hold it. Readers pay once through
  PayPal on the pack's page (`/guides/packs/<address>`) and read with a
  private link, kept in their browser and emailed to the address they paid
  with: no accounts. Make packs and set their prices on `/admin/guides`; the
  same page lists the sales, with **Turn off** for a shared link and
  **Refund**. Three starter packs come with ten new guides, as drafts for you
  to check: a PCS move pack for military families (move checklist, housing,
  pets, cars, schools, typhoon season), a First week on Guam pack (a
  seven-day plan, three or four days, Guam on a budget, before you fly) and
  an Every guide pack. Sales count in the daily money report. New database
  tables (run the update as usual), no new settings: packs need PayPal and
  `STATS_PASSWORD`, and email for the link by email. How-to:
  [docs/guides.md](docs/guides.md#selling-guides).
- **Paid press releases and deals.** Two more kinds of paid listings, sold
  through PayPal and checked by you on `/admin/listings` like the others.
  **Press releases** (`/press`): a business or group posts its own news,
  with a photo, a media contact and a website. Each one gets a page of its
  own, is marked "Sponsored", stays on the site, and goes out once in the
  newsletter feed, after the third story of the next edition, titled "Press
  release:". **Deals and offers** (`/deals`): a coupon or special with an
  optional coupon code, filtered by category, and shown in turns in a box at
  the top of the home page. The business picks the deal's last day (up to 8
  weeks ahead) and pays for each week; before it ends, it gets the renewal
  email and can add a week. New settings `PRESS_RELEASE_PRICE` (default 25
  dollars) and `DEAL_WEEK_PRICE` (default 10 dollars a week); `0` turns
  either off. Links in the sidebar's "Buy, sell and find local" box and at
  the bottom of every page. No database change.
- **Section sponsors.** Sell one section of the site by the week: a line
  such as "Weather brought to you by Island Bakery", with a link, at the top
  of every page of that section (a news topic, Jobs, Events, Bids, Weather or
  Outages). One business a section a week. Businesses book and pay on the
  Advertise page as for the sponsor spot, and you approve each one on
  `/advertise/review`; renewal reminders, "Run this ad again", Discord
  alerts and the money summary work the same way. New setting
  `SECTION_SPONSOR_PRICE` (empty: not sold). No database change.
- **Media kit.** The Advertise page has a "Download the media kit" link: a
  one-page PDF with your real numbers from the last 30 days (page views, the
  most-read sections, where readers come from) and your prices, to send to
  local businesses. It is made again every 10 minutes. It needs a new
  package (fpdf2), so rebuild as usual when you update.
- **Visitor guides.** A new section, `/guides`, with practical guides for
  visiting Guam that you write and keep up to date yourself, in your browser,
  on `/admin/guides` (the `STATS_PASSWORD` password). Write in Markdown, add a
  cover photo and photos in the text, sort the guides into sections, keep them
  as drafts until they are ready, and preview them as readers will see them.
  Twenty starter guides come with the site: tick the ones you want and they
  are added as drafts.
  Guides show the AdSense ad in the right-hand column, a second one in the
  text (`ADSENSE_STORIES_SLOT`), the sponsor spot and the Plan a trip box, and
  each guide can carry its own sponsor ("Sponsored by", with an end date and
  click counts). The guides are kept in the database (backed up nightly), so
  their text stays yours; each has a free/paid setting, ready for selling
  guides later. The top bar shows **Guides** once one is published. New
  database table (run the update as usual), no new settings. How-to:
  `docs/guides.md`.
- **Twelve more starter guides,** most of them about things to do: Things to
  do on Guam (a list linking to every guide), Tumon and Two Lovers Point,
  Snorkeling and diving, Hiking and waterfalls, Boats, dolphins and island
  trips, World War II sites, Latte stones and ancient CHamoru sites, A day in
  northern Guam, Markets and shopping, Guam with kids, What to do on a rainy
  day, and Fiestas and festivals through the year. On `/admin/guides`, tick
  the ones you want and press **Add the ticked guides as drafts**; a starter
  guide you deleted is listed again but no longer added unless you tick it.
- **Dive and snorkel, and Guam's climate, on the Weather page.** "Dive and
  snorkel" shows the sea today: the rip current risk, the reefs with the
  smallest surf, the water temperature, today's low tides and tide range,
  the moon (spring or neap tides) and a few safety tips, with a link to Guam
  EPA's beach report. "Guam's climate through the year" is a table of each
  month's average high, low and rain (NOAA's 1991 to 2020 averages), with
  this month in bold. "Be ready" gains a link to the airport's flight
  schedule (its site turns away automated readers, so the site links to it
  rather than showing the flights). No new settings and no migration.
- **Reports and server checks in Discord.** Every morning at 8:00 (Guam
  time), a money summary: yesterday's paid listings, booked ads, bid alert
  payments and donations, money given back, and the listings and ads that
  wait for your approval, with the links. Every Monday, a weekly report:
  page views, story clicks, the busiest day, the most clicked story, where
  readers came from, ad clicks, newsletter sign-ups, new bid alert
  subscribers and the money of the week, each compared with the week before.
  `guheadlines report daily` (or `weekly`) posts one now. Donations and
  refunds need PayPal's Transaction search turned on for your app (the steps
  are in `.env.example`). The server now also checks itself with each
  automatic-update round (`scripts/server-check.sh`): an alert when the disk
  is 90% full, when memory stays 90% used, when an https certificate ends
  within 14 days, and (with your own mail server on) when its IP address is
  on a spam block list; and once a week it deletes Docker's leftovers. New
  settings: `REPORT_TIME`, `DAILY_REPORT`, `WEEKLY_REPORT_DAY`,
  `DISK_ALERT_PERCENT`, `MEMORY_ALERT_PERCENT`, `CERT_ALERT_DAYS`.
- **Renewal reminders for paid listings and booked ads.** Classified ads,
  featured job posts, business upgrades and booked ads already left the site
  on their last day; now the buyer gets one email a few days before
  (`RENEWAL_REMINDER_DAYS`, default 3, `0` turns it off). A listing's own page
  has a **Renew** button: PayPal takes the price again and the listing gets as
  many days more, with no new review (Discord tells you; `/admin/listings`
  counts renewals). A booked ad's page has **Run this ad again**, which books
  the same picture or words for the next free weeks, paid and approved as
  usual. The email goes to the address the poster paid with on PayPal, or the
  address in the booking. Needs email (`SMTP_HOST`, `MAIL_FROM`) and
  `SITE_DOMAIN` or `SITE_URL`. Database change: migration `0015_renewals`
  (runs by itself when the site starts).
- **The whole site is backed up every night.** A new `backup` container
  saves the database, the photos, `.env`, the `config` folder, Caddy's
  certificates and (with your own email server) the mailboxes, mail accounts,
  DKIM key and webmail settings, every night at 3:00 AM Guam time
  (`BACKUP_TIME`), and keeps 14 nights (`BACKUP_KEEP_DAYS`) in
  `backups/site`. A photo kept by several nights is stored once, so the
  nights cost little more space than one copy. Nothing to set up: it starts
  with the site, and makes its first backup a few minutes after it starts.
  The stats page lists the backups with a **Download** button (one `.tar`
  file with everything; over https and with a `STATS_PASSWORD` of at least 12
  characters only, and each download is posted in Discord).
  `./scripts/site-backup.sh` backs up now (`now`), lists the backups
  (`list`), makes one file to copy elsewhere (`export`), and puts a backup
  back with one command (`restore`), all of it or only some parts, on the
  same server or on a new one (where it also makes `.env` from the backup).
  A backup is skipped when it would leave less than 1 GB free, and
  `auto-update.sh` posts in Discord when a backup fails or none has run for a
  day. See [Backups](docs/maintenance.md#backups).
- **Your own email server, with webmail.** Turn it on with
  `COMPOSE_PROFILES=mail` and `MAIL_HOSTNAME=mail.guamheadlines.com` in
  `.env`, and you get email addresses at your domain, webmail at
  `https://mail.guamheadlines.com` (Roundcube; phone mail apps work too), and
  the site sends its own emails (the bid alerts) from your server instead of
  through an email company. It runs as two more containers: `mailserver`
  (docker-mailserver, with the Rspamd spam filter, DKIM signing and Fail2Ban
  against password guessing) and `webmail`. Caddy gets the certificate for the
  mail server's name, and the mail server uses it too. `./scripts/mail.sh`
  checks the server (`check`), makes your mailbox and the site's mailbox and
  starts it all (`setup`), lists the DNS records to add with your own values
  and which ones are in place (`dns`), and adds mailboxes and aliases, changes
  passwords, sends a test email and unblocks addresses. Automatic updates
  download new versions of the mail server once a week, and the nightly backup
  saves the mailboxes. It also needs DNS records and reverse DNS from
  your server company; [Your own email server](docs/email-server.md) walks
  through every step. Nothing changes until you turn it on.
- **Paid listings through PayPal: classifieds, a business directory, and
  featured jobs and events.** People can post a classified ad (for sale,
  cars and trucks, housing, services, wanted) on a new **Classifieds** page;
  employers can pin a job at the top of the Jobs page; organizers can feature
  an event at the top of the Events page, in gold on the month calendar and
  on the home page until it is over; businesses get a free entry in a new
  **Business directory**, and a paid upgrade with a photo, a website link and
  a place at the top. Nobody needs an account: the form leads to a private
  page for the listing with a **Pay with PayPal** link (a PayPal account or a
  card), and each listing waits for you on `/admin/listings` (your
  `STATS_PASSWORD`) before it shows. Reject gives the money back through
  PayPal. Discord tells you when a listing waits (with
  `DISCORD_WEBHOOK_URL`). The pages load no PayPal script, so PageSpeed and
  the security rules stay as they were. Links to the new pages are in a
  sidebar box and at the bottom of every page (the top bar is full). Prices
  and days are settings: `CLASSIFIED_PRICE` (default 5 dollars),
  `CLASSIFIED_DAYS` (30), `FEATURED_JOB_PRICE` (25), `FEATURED_JOB_DAYS` (7),
  `FEATURED_EVENT_PRICE` (15), `BUSINESS_UPGRADE_PRICE` (50) and
  `BUSINESS_UPGRADE_DAYS` (365). It all stays off until you set
  `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` and `STATS_PASSWORD`; the steps
  are in `.env.example` (PayPal and Paid listings) and
  [Paid listings](docs/settings.md#paid-listings). Test with
  `PAYPAL_MODE=sandbox` first, and check the values with
  `guheadlines paypal-check`. When you update, the database gets a new table
  by itself.
- **An Advertise page, where businesses book and pay for ads themselves.** A
  new page (`/advertise`, linked at the bottom of every page) shows your real
  numbers from the visitor stats (page views in the last 30 days, with a
  chart, and the clicks on the sponsor spot and on the newsletter's stories),
  and a booking form for each thing that has a price: the sponsor spot by the
  week (`SPONSOR_WEEK_PRICE`) and a sponsor line at the top of a newsletter
  edition (`NEWSLETTER_SPONSOR_PRICE`). A business picks its weeks or
  editions, gives its picture or words, and pays through PayPal. You get a
  Discord alert, and the ad shows only after you approve it on the new
  `/advertise/review` page (the stats password opens it); turning it down
  refunds it through PayPal. Each booking has its own page with a secret
  address, where the business sees where it stands and, later, its ad's page
  views and clicks. No logins, and no PayPal script on the site, so the
  PageSpeed scores stay the same. It needs PayPal (`PAYPAL_CLIENT_ID` and
  `PAYPAL_CLIENT_SECRET`), `STATS_PASSWORD` and a price; without them the site
  is as before. When you update, the database gets two new tables, by itself.
  See [Advertise page](docs/settings.md#advertise-page).
- **A "Buy me a coffee" box, a "Plan a trip" box and a second ad.** With
  `PAYPAL_DONATE_LINK` set (a PayPal.Me or PayPal Donate button link), every
  page shows a **Support this site** box with a **Buy me a coffee** button
  that opens your PayPal page. With one or more `AFFILIATE_*_LINK` settings
  (hotels, flights, tours and car rentals), the Weather page and the Events
  pages show a **Plan a trip** box of affiliate links, with a note that tells
  readers the site can earn a commission. With `ADSENSE_STORIES_SLOT` set,
  the home page and the Obituaries page show a second AdSense ad after the
  fifth story. All three are off until you set them, load no outside script,
  and add a paragraph to the disclaimer on the Sources page where needed. See
  [Coffee donations](docs/settings.md#coffee-donations),
  [Affiliate links](docs/settings.md#affiliate-links) and
  [The ad between the stories](docs/settings.md#advertising).
- **Paid bid alerts.** Businesses can pay a monthly price through PayPal to
  get an email when a new bid on the Bids page matches their trades (such as
  construction, IT or medical) or their own words. The new page
  `/bids/alerts` takes the sign-up; the buyer pays on PayPal's own page, so
  the site loads no PayPal script and its security rules do not change.
  There is no login: every email links to the subscriber's own page, where
  they change their choices or cancel, and a lost link can be sent again.
  The worker sends one email per subscriber after each bids check, asks
  PayPal about the subscriptions twice a day, pauses the alerts after failed
  payments, and keeps them going after a cancel until the paid month ends.
  The emails go out through a mail server over SMTP (on your own server,
  or an email company's). New settings:
  `BID_ALERTS_PRICE`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`
  and `MAIL_FROM`, with step-by-step notes in `.env.example`. New commands:
  `guheadlines bid-alerts` and `guheadlines mail-test`. See
  [Bid alerts](docs/bid-alerts.md). When you update, the database gets two
  new tables by itself; bid alerts stay off until you set them up.
- **A Bids page.** A new **Bids** link lists the Government of Guam's open
  bids and requests for proposals, soonest due first: what each is for, its
  number, the agency, when offers are due (Guam time, with the days left),
  the day it was posted and links to the notice and its PDF. It reads
  notices.guam.gov (its IFB, RFP and emergency procurement topics, which
  GSA, the Airport, the Port, GCC and other agencies use) and the bid lists
  of the Power Authority and GHURA (on the OpenGov Guam portal), the
  hospital, Waterworks and the Port. Bids leave the page once their due date
  has passed or their source lists them as closed, cancelled or awarded; a
  bid posted in two places is shown once, crediting both. Sole-source and
  hearing notices are left out. Search by word, or narrow to one agency or
  source. The sources are in the new `config/bids.yaml`, read every
  `BIDS_INTERVAL_HOURS` (default 6), and `guheadlines bids --check` tries
  them. See [Bids and RFPs](docs/bids.md). When you update, the database
  gets two new tables, by itself, and the page works without a change to
  `.env`. To keep the top bar on one line with Bids and Outages, the
  **Sources** link moved to the bottom of every page, beside About this site.
- **An Outages page.** The new **Outages** link in the top bar opens a page
  of the power outages that the Guam Power Authority announces (from
  GuamALERTS), the water outages that the Guam Waterworks Authority
  announces, and the boil-water notices of the last 30 days (from GWA and
  Guam EPA). Power and water are sorted into Today, Coming up and Earlier.
  Each notice shows the villages, the day and hours and the areas, and links
  to the agency's notice; each part has the number to call to report an
  outage. Outages are only on this page, not in the warning banner or on the
  home page. The worker reads the notices every 15 minutes
  (`OUTAGES_INTERVAL_MINUTES`; `0` turns the page off). Run
  `guheadlines outages` to read them now. No database change. See
  [Outages and gas prices](docs/settings.md#outages-and-gas-prices).
- **Gas prices in the sidebar.** Regular, premium and diesel, for a gallon,
  read from the headlines and intros of the local news stories the site
  already collects ("Gas prices up again: $5.94 a gallon of regular"), each
  with the day of its story and a link to it. Only stories from the last 45
  days count (`GAS_PRICE_DAYS`; `0` turns the prices off); with none, the
  sidebar shows no prices.
- **A newsletter sign-up box.** With `SENDFOX_API_TOKEN` set, every page has
  a small Newsletter box in the right-hand column, and the site gets a
  newsletter page (`/newsletter`, linked from the footer). Readers type their
  email address, and the server adds it to your SendFox list
  (`SENDFOX_LIST`, or the account's only list). No SendFox script loads in
  the browser, so the security rules and the PageSpeed scores do not change.
  A hidden field and a limit of 5 sign-ups an hour from one address keep
  robots out. `guheadlines sendfox-check` shows the lists and the one that
  sign-ups join. See [The sign-up box](docs/newsletter.md#the-sign-up-box).
- **A sponsor spot that you sell yourself.** Put a sponsor's picture in
  `config/sponsor/` and set `SPONSOR_IMAGE`, `SPONSOR_LINK` and
  `SPONSOR_ALT`: the picture shows at the top of the right-hand column of
  every page, marked "Sponsor", above the AdSense ad. The site serves a small
  WebP copy of it itself, with no outside script or cookie. With the visitor
  stats on, the stats page shows the sponsor clicks. See
  [Sponsor spot](docs/settings.md#sponsor-spot).
- **Source health on the stats page, and an alert when a news site goes
  quiet.** The private stats page (`/stats`) now ends with **Source health**:
  for each news site, whether it is working, failing (with the error) or
  quiet, its newest story, its new stories in the last 7 days and when it was
  last checked; and the same for the job boards and event calendars. A note
  at the top of the page names the sources that need a look. A news site is
  **quiet** when its checks work but it has had no new story for twice its
  longest wait between stories in the two months before (at least 2 days):
  its feed may have changed so that the stories are no longer found. Discord
  gets one alert when a site goes quiet and one when it has new stories
  again. Event calendars that stop working now get alerts too, like news
  sites and job boards. See [Source health](docs/maintenance.md#source-health).
- **Keyword alerts in Discord.** Put words in the new `ALERT_KEYWORDS`
  setting, such as `typhoon, shooting, power outage`, and a message comes in
  your Discord channel when a new story's headline or intro mentions one,
  with the headline, the news site and the link. Each story comes once;
  several at once come in one message. Case and accents don't matter, only
  whole words count (`shooting` also finds `shootings`), and `evacuat*` finds
  every word that starts with `evacuat`. See
  [Keyword alerts](docs/maintenance.md#keyword-alerts). When you update, the
  database gets one small new table, by itself.
- **Events in Google and in phone calendars.** Each upcoming event now has a
  page of its own on the site (click its name on the Events page), with the
  whole description, its links, and two buttons that add the event to Google
  Calendar or to another calendar app (Apple Calendar, Outlook...). Each page
  describes its event in the form Google reads for its event results (Event
  structured data), and the sitemap lists every upcoming event's page. Under
  the month calendar, **Get these events in your calendar** subscribes a
  visitor's Google Calendar, Apple Calendar or Outlook to the events: the
  main events of every calendar, or every event of the calendar picked on the
  page. The app keeps them up to date by itself. The calendar address is
  `/events.ics`. No setting to change, and no script is added. See
  [Each event's page](docs/events.md#each-events-page) and
  [Adding the events to a calendar app](docs/events.md#adding-the-events-to-a-calendar-app).
- **Faster photos on computers and tablets.** Each story's photo is now saved
  twice on your server: the copy for phones, and a small copy 360 pixels wide
  for larger screens, where the photo is drawn 228 pixels wide. Browsers load
  only the copy they need, so on a computer a page of stories loads roughly
  a quarter to a third of the photo bytes it did. Photos saved before this get their small copy
  from the worker, a few thousand after each news check, newest first; until
  then they show as before. When a story has no saved copy (for example the
  `media` storage was not moved with the database), the site shows the news
  site's own photo instead of a broken picture.
- **A limit on the disk space for photos.** The new setting
  `IMAGE_STORAGE_GB` (5 unless you change it; 0 for no limit) is the most
  space the saved photos may use. Above it, the worker deletes the photos of
  the oldest stories until they use 90% of it; those stories then show the
  news site's own photo. The worker also deletes saved photos that no story
  uses any more. `docker compose exec worker guheadlines photos` shows the
  space used. No change to `.env` is needed, and the database is not
  changed. See [Photos](docs/settings.md#photos).
- **A month calendar on the Events page.** Above the list of upcoming events,
  a calendar shows the month's main events, at most 3 a day (with "+2 more"
  and so on), all-day events first. Events are colored by their calendar:
  green for the University of Guam, red for the Guam Legislature, blue for
  Guam Community College, gray for the rest, with a legend. Click a day to
  see everything on it in the list below; ‹ and › step through the months
  events are collected for. On a phone each event is a small colored square.
  Which events count as main events is set per calendar with the new
  `main_events` option in `config/events.yaml` (out of the box, all but the
  GovGuam board meetings), and the color with `color`. No script is added:
  the calendar is plain HTML. See
  [The month calendar](docs/events.md#the-month-calendar).
- **University of Guam events** on the Events page, from its events
  calendar's RSS feed (the new `moderncampus` input in
  `config/events.yaml`): events, academic dates and holidays.

- **A newsletter feed for SendFox.** The site now has a feed of its top
  stories at `/feed.xml`, for an email service such as SendFox. Every
  Tuesday and Friday at 10:00 AM (Guam time), the worker makes a new
  edition: the stories it found since the edition before, the ones readers
  clicked most first, at most 3 from one news site. The feed keeps the
  edition until the next one, so the email service sees new stories only
  twice a week. Each story has its headline, photo, news site, the first
  lines of its intro and a link (through the click counter while the visitor
  stats are on). No page links to the feed, and search engines are told not
  to list it. The new settings `NEWSLETTER_DAYS`, `NEWSLETTER_TIME` and
  `NEWSLETTER_STORIES` change the schedule and the size. See
  [Newsletter feed](docs/newsletter.md), which also has the SendFox steps.
  When you update, the database gets one small new table, by itself, and the
  feed works without a change to `.env`.
- **Private visitor stats.** With `STATS_PASSWORD` in `.env`, the site counts
  page views and story clicks, and shows them on a private page, `/stats`,
  behind that password: totals, a chart for each day, the websites readers
  come from (such as Google or Facebook), the top stories, the clicks for
  each news source, the views and clicks for each topic, and the most viewed
  pages. The counting happens on the server: no script in the
  visitor's browser (the PageSpeed scores stay the same), no cookies, no
  outside service, no API key and no stored IP addresses. Robots and link
  previews are not counted. While the stats are on, story links pass through
  a short counting address on the site (`/go/<number>`). See
  [Visitor stats](docs/settings.md#visitor-stats). When you update, the
  database gets three small new tables, by itself. Without the setting, nothing
  changes.
- **A Weather page and the tides.** A new **Weather** link in the top bar
  opens a page with the airport reading, sunrise, sunset and the moon, the
  watches, warnings and advisories, the next 24 hours, the week's forecast,
  the surf and rip current risk on each side of the island, the wind and
  seas for boating, the typhoons, and links for being ready. Its **Tides**
  part shows the next high or low tide and has three views to switch
  between: Day (with a chart), Week and Month. The weather box on the home
  page now lists today's high and low tides too, and links to the page. The
  tides come from NOAA Tides & Currents (Apra Harbor), and the new parts of
  the weather from the National Weather Service: both free, with no key.
  Each part of the page links to where its data comes from. The new setting
  `TIDE_STATION` picks the tide station, and an empty `TIDE_STATION=` turns
  the tides off. When you update, you can add its line to your `.env` (see
  [Getting the newest notes in .env](docs/settings.md#getting-the-newest-notes-in-env)),
  or leave it out to use Apra Harbor. Nothing else to do: the database needs
  no change.
- **Automatic updates for a server.** `./scripts/auto-update.sh --install`
  makes the server check every 5 minutes for new commits on the `main`
  branch. When there are some, it makes a database backup, then rebuilds
  and restarts the site by itself. If the new version doesn't start within
  5 minutes, it goes back to the version before. It also checks that the
  site answers. Starting it with `sh` or `bash` works too. See
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
- **Weather and typhoon warnings.** While the National Weather Service has a
  watch or a warning for Guam (a Typhoon Warning, a Tropical Storm Watch, a
  Flood Watch and so on), a red or amber banner shows at the top of every
  page. Visitors open it to read the full warning. The home page also has a
  weather box: the latest reading at Guam International Airport, the
  forecast, the tropical storms and typhoons in the western North Pacific
  from the Joint Typhoon Warning Center, and the advisories. The worker reads
  the weather every 10 minutes. Both services are free and need no key. The
  new setting `WEATHER_INTERVAL_MINUTES` changes how often, and `0` turns the
  weather off. When you update, add its line to your `.env` (see
  [Getting the newest notes in .env](docs/settings.md#getting-the-newest-notes-in-env)),
  or leave it out to use the default of 10. The new command
  `guheadlines weather` reads the weather now and shows what each service
  gave.
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
- **The site is now called Guam Headlines** (it was GU Headlines), which
  matches its domain and the words people search for. If your `.env` has
  `SITE_NAME=GU Headlines`, change it to `SITE_NAME=Guam Headlines`. The
  picture shown when someone shares a link has the new name.
- **Security hardening.** After 10 wrong passwords within 15 minutes, an
  address is blocked from the private pages (`/stats`, `/admin/...`,
  `/advertise/review`, backup downloads) for an hour, and Discord gets an
  alert; the stats page warns about a short `STATS_PASSWORD`. Requests over
  20 MB are refused by Caddy and the site. Once a week, automatic updates now
  also download new versions of the database, Caddy and the Python image
  (security fixes), not only of the mail server. The server checks make
  `.env` and the `backups` folder readable by their owner only, and post in
  Discord when Linux needs a restart to finish its security updates. The
  quick database backups are readable by their owner only. New guide,
  `docs/security.md`, and a script that makes the server itself safer in one
  go: `./scripts/harden-server.sh` turns on automatic Linux security
  updates, Fail2Ban and the firewall, makes ssh take keys only (only when
  you signed in with a key, so you can't lock yourself out), replaces a
  `STATS_PASSWORD` shorter than 12 characters with a long random one, and
  lists what is still yours to do (the ssh key on your computer, two-step
  sign-in on your accounts). `--check` only shows where each step stands. No
  new settings.
- `scripts/backup.sh` (the quick database backup before each automatic
  update) keeps its files 7 days instead of 30, as the nightly backups keep
  14 days of everything. It no longer saves the mailboxes: the nightly
  backup does.
- **Sources page: how this site compares with search engines.** "Why this
  site exists" now adds that search engines such as Google read the full
  text of every article they index, while this site keeps only the
  headline, a short excerpt and the photo. Nothing to do when you update.
- **No Chamber of Commerce events, and no calendars that can't be
  collected.** The Events page no longer lists the Chamber of Commerce, the
  Guam Visitors Bureau or the Guampedia fiesta calendar, and events that
  name the Chamber of Commerce are left out of every calendar (the new
  file-wide `exclude` list in `config/events.yaml`). The "Can't be collected"
  list is gone from the Events page.
- **No news stories that name the Chamber of Commerce.** Stories whose
  headline or intro mentions the Chamber of Commerce are no longer shown
  anywhere on the site: the front page, the topics, the news sites' pages,
  the archive, search, the sidebar counts, the sitemap and the newsletter
  feed. They are still collected, so nothing is lost. The words are listed
  under the new `muted:` setting near the top of `config/sources.yaml`; add a
  word or phrase there to hide other stories the same way, or delete a line
  to show them again. See
  [Hide stories that mention a name](docs/news-sources.md#hide-stories-that-mention-a-name).
  Nothing to do when you update.
- **Faster pages with the ad, and 100 in all four PageSpeed scores.**
  Google's ad code now loads when the visitor first scrolls, taps, presses a
  key or moves the mouse, not with the page. The page is ready sooner, and
  [PageSpeed Insights](https://pagespeed.web.dev) measures the page without
  the ad. The ad block keeps room for the ad, so the right-hand column does
  not jump when the ad comes in. When an ad blocker stops the ad, the empty
  block is taken away. Nothing to do when you update.
- **Easier to read and to tap.** Links inside sentences are underlined. The
  greyed-out "Newer" and "Older" buttons and paused sources on the Sources
  page are no longer faded too light to read. The links in the weather box
  and the source lists in the right-hand column have a larger tap area. On
  the Jobs and Search pages, the line that counts the results is now a
  heading, so screen readers find their way through the list. Nothing to do
  when you update.
- **Search engines can list the Sources page**, which has "Why this site
  exists" and the disclaimer. It is in `sitemap.xml`, and `robots.txt` no
  longer blocks it. Nothing to do when you update.
- **The weather box on the home page is about half as tall.** The airport
  reading sits beside the forecast, each forecast period takes one line, and
  typhoons and advisories share one line under it. Click an advisory to read
  it and see when it ends. On a tablet, or a laptop with the sidebar, the
  box shows the next three forecast periods; on a phone, the next two.
  Nothing to do when you update.
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
- **One PayPal payment can no longer pay for two things.** When a buyer
  came back from PayPal, a new listing accepted the ID of any order of the
  site with the same price, such as a renewal or an ad booking that the
  buyer was paying for anyway. Now the site checks with PayPal that the order
  was made for this very listing, renewal or ad before it takes the money.
- **The AdSense publisher ID is found in any form that AdSense shows.** The
  site serves `ads.txt` only when `ADSENSE_CLIENT` holds a publisher ID. A
  value in a different form, such as the full line from the ads.txt page of
  AdSense, was ignored, so AdSense said that it found no ads.txt file. Now
  the site takes the ID out of the ads.txt line, the verification code or the
  AdSense code. [Advertising](docs/settings.md#advertising) tells what to do
  when AdSense says that it found no ads.txt file. Nothing to do when you
  update.
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
- **A matte look, like the screen of a color e-reader.** The frosted glass,
  the glossy edges, the shadows and the colored light behind the page are
  gone. Light mode is a warm paper color instead of plain white, and dark
  mode is a soft charcoal instead of dark blue. Cards are flat sheets with a
  thin border, the topic colors are muted, and headlines use a book-style
  serif font. The tab icon, the phone icon and the link-preview picture
  have the new look too. If a browser still shows the old look, reload the
  page once (Ctrl+Shift+R, or Cmd+Shift+R on a Mac).
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
