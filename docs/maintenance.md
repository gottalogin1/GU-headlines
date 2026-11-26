# Looking after it

GU Headlines mostly runs by itself. This guide covers the occasional jobs:
checking it's healthy, backups, updates, alerts, and putting it on the
internet.

All commands are run from inside the `GU-headlines` folder.

**Contents**

- [Is it healthy?](#is-it-healthy)
- [Backups](#backups)
- [Restoring from a backup](#restoring-from-a-backup)
- [Updating](#updating)
  - [Automatic updates](#automatic-updates)
- [Alerts in Discord](#alerts-in-discord)
- [Disk space](#disk-space)
- [Putting it on the internet](#putting-it-on-the-internet)
- [Moving to a new computer](#moving-to-a-new-computer)
- [Starting over or uninstalling](#starting-over-or-uninstalling)

---

## Is it healthy?

**Quick look:** the **Updated** box in the website's sidebar says when the
news was last checked. Normally that's under an hour ago (a little more just
before the top of the hour).

**The Sources page** shows each news site's last check and any problem. A
single red message now and then is normal: news sites have hiccups. A site
that shows **0** new stories for days deserves a look; see
[News sources](news-sources.md#reading-the-sources-page).

**The programs themselves:**

```sh
docker compose ps
```

All five (`db`, `web`, `worker`, `caddy`, `backup`) should say **Up**
(seven, with `mailserver` and `webmail`, when
[your own email server](email-server.md) is on). To see what the
news collector has been doing:

```sh
docker compose logs --tail 50 worker
```

**Alerts.** GU Headlines can post a message in a Discord channel when the
site stops answering or a news site stops working, so you don't have to
look. See [Alerts in Discord](#alerts-in-discord).

The address `/healthz` (for example `https://guamheadlines.com/healthz`, or
`http://your-server/healthz` without a domain) answers like this, which
monitoring services can check:

```json
{"status": "ok", "last_scrape": "2026-09-27T19:00:41+00:00"}
```

---

## Backups

**The whole site is backed up every night, by itself.** At 3:00 AM Guam time
(`BACKUP_TIME` in `.env`), the `backup` part of the site saves:

- the database: every story, job, event, bid, listing, booking and
  subscriber, and the visitor counts,
- the saved photos,
- your settings (`.env`), with your passwords and keys,
- the `config` folder (news sites, topics, your sponsor picture),
- Caddy's https certificates,
- with [your own email server](email-server.md) on: the mailboxes, the mail
  accounts and the DKIM key, and the webmail's settings and contacts.

Each backup is a folder in `backups/site`, named by its date and time, for
example `backups/site/2026-11-26-0300`, and is kept for 14 days
(`BACKUP_KEEP_DAYS`). A photo that is in the backups of several nights is
stored only once, so 14 nights take much less space than 14 copies of the
site. The site keeps working while it backs up. When the site starts for the
first time (or after it was off for a day), the first backup comes a few
minutes later.

There is nothing to set up. The settings are in `.env.example`, under
"Backups".

### Seeing the backups

On the private stats page (`/stats`, see
[Visitor stats](settings.md#visitor-stats)), the **Backups** section at the bottom lists
them, with their size, the space all of them use, and the free space on the
disk. It says so when the last backup failed. With
[alerts in Discord](#alerts-in-discord) and
[automatic updates](#automatic-updates) on, a failed backup (or no backup for
more than a day) is posted in Discord too.

On the server:

```sh
./scripts/site-backup.sh list
```

### Backing up now

Before a big change, back up by hand:

```sh
./scripts/site-backup.sh now
```

It takes a few seconds to a few minutes and makes one more folder in
`backups/site`.

### Keeping a copy somewhere else

The backups are on the server's own disk. A broken disk, or a server that the
hosting company deletes, takes them with it. So now and then (once a month,
say), download one to your own computer:

- **In the browser:** open `/stats`, scroll to **Backups**, and press
  **Download** next to a backup. You get one file, such as
  `site-backup-2026-11-26-0300.tar`, with everything in it. This works only
  over https (with `SITE_DOMAIN` set) and with a `STATS_PASSWORD` of at least
  12 letters and digits, because a backup holds all your passwords. Each
  download is posted in your Discord channel, so you would notice a download
  that wasn't you.
- **On the server:** this makes a fresh backup as one file in the `backups`
  folder, and says how to copy it to your computer (it is deleted from the
  server after 3 days):

  ```sh
  ./scripts/site-backup.sh export
  ```

  To send the newest backup straight to your own computer, without a copy on
  the server, run this **on your computer** (with your user, server and
  folder):

  ```sh
  ssh you@your-server 'cd GU-headlines && ./scripts/site-backup.sh export -' > site-backup.tar
  ```

Keep the file private: it holds your `.env` (every password and key) and your
readers' email addresses. A `.tar` file opens in Windows (right-click,
Extract All), on a Mac (double-click) and in 7-Zip, if you want to look
inside. `database.sql.gz` in it is the database, and `README.txt` says what
the rest is.

### The quick database backup

Before each [automatic update](#automatic-updates), the server also saves the
database alone, as `backups/guheadlines-<date>-<time>.sql.gz`, kept for 7
days. You can make one by hand with `./scripts/backup.sh`. It's a quick way
back from an update that went wrong.

**Windows:** the scripts need a Linux-style shell. Run them from
[WSL](https://learn.microsoft.com/windows/wsl/install) or Git Bash. If one
fails with errors like `$'\r': command not found`, your copy was downloaded
before the project fixed its line endings. Fix it once, from the same WSL or
Git Bash window:

```sh
sed -i 's/\r$//' scripts/*.sh .env
```

---

## Restoring from a backup

Restoring **replaces** what the site has now with the backup. Anything newer
than the backup (stories found since, new listings, mail that came since) is
lost, so pick the newest backup that is still good.

**From a backup on the server** (a name from `./scripts/site-backup.sh list`):

```sh
./scripts/site-backup.sh restore 2026-11-26-0300
```

**From a downloaded file:** copy it into the `GU-headlines` folder on the
server (for example with `scp site-backup-2026-11-26-0300.tar
you@your-server:GU-headlines/`), then:

```sh
./scripts/site-backup.sh restore site-backup-2026-11-26-0300.tar
```

It says what it will put back and asks you to type `yes`. Then it stops the
site, puts the backup back, and starts the site again; the site is offline
for a few minutes. If the database can't be put back (a damaged file), it
stays as it was, and nothing else is changed.

**Only some parts.** Name them after the backup:

```sh
./scripts/site-backup.sh restore 2026-11-26-0300 database
./scripts/site-backup.sh restore 2026-11-26-0300 photos mail
```

The parts are `database`, `photos`, `settings` (your `.env`), `sponsor` (the
sponsor picture), `mail` (mailboxes, accounts and the DKIM key), `webmail`
and `certificates`.

**Your settings (`.env`) are kept** unless you name `settings`. Then the
backup's `.env` replaces yours, and yours is kept as
`backups/env-before-restore-<date>`. On a new server without a `.env` yet,
the settings always come from the backup.

**An older quick database backup** (`backups/guheadlines-....sql.gz`, from
before an update) goes back like this:

1. Stop the website and the worker: `docker compose stop web worker`
2. Load the backup (use your file's name):

   ```sh
   gunzip -c backups/guheadlines-20260927-0315.sql.gz | docker compose exec -T db psql -U guheadlines -d guheadlines
   ```

   Many lines scroll past; that's normal.
3. Start everything again: `docker compose up -d`

---

## Updating

New versions are listed on the project's GitHub **Releases** page, and
[CHANGELOG.md](../CHANGELOG.md) says what changed in each one. To update:

```sh
git pull
docker compose up -d --build
```

The first command downloads the new version; the second rebuilds and
restarts it. Database changes are applied automatically. Your stories,
photos and `.env` are kept.

**If `git pull` says your local changes would be overwritten**, it's because
you edited files that the update also changes (usually `config/sources.yaml`
or `config/categories.yaml`). Set your changes aside, update, and put them
back:

```sh
git stash
git pull
git stash pop
docker compose up -d --build
```

If `git stash pop` reports a **CONFLICT**, open the file it names. You'll see
blocks like this:

```
<<<<<<< Updated upstream
  (the new version's lines)
=======
  (your lines)
>>>>>>> Stashed changes
```

Keep the lines you want, delete the other lines and the three marker lines,
save, then run `git stash drop`. Check the file with
`docker compose exec worker guheadlines sources`.

It's a good idea to take a backup before updating: `./scripts/site-backup.sh now`.

### Automatic updates

On a server that is always on, such as a rented server, GU Headlines can
update itself. Every 5 minutes the server checks whether the `main` branch
on GitHub has new commits (for example after a pull request is merged). If
it has, the server does the steps above by itself: it downloads the new
version, makes a database backup, and rebuilds and restarts the site. If the
new version doesn't answer within 5 minutes, the server goes back to the
version before. It also checks every 5 minutes that the site answers. With
[your own email server](email-server.md) on, it downloads new versions of the
mail server and the webmail once a week too (they bring security fixes).

It's meant for a Linux server. On a Windows or Mac computer, update by hand.

**Turn it on** (once). On the server, in the `GU-headlines` folder, as the
user that runs `docker compose` (on a rented server, usually `root`):

1. Set up [alerts in Discord](#alerts-in-discord) first, so you hear about
   updates and problems. (It works without them too.)
2. Make sure the folder is on the `main` branch and up to date:

   ```sh
   git checkout main
   git pull
   docker compose up -d --build
   ```

3. Turn on the rounds every 5 minutes:

   ```sh
   ./scripts/auto-update.sh --install
   ```

   It says `Done`, and posts a test message in your Discord channel. It adds
   one line to the server's **cron** schedule (see
   [Backing up automatically every night](#backing-up-automatically-every-night));
   `crontab -l` shows it. If it says `Permission denied`, run
   `bash scripts/auto-update.sh --install` instead; that works too.

**What happens after a merge.** Within 5 minutes, the server starts the
update. A minute or two later the site runs the new version, and Discord
shows "🚀 guamheadlines.com updated" with the list of changes. This message
comes without a phone notification.

**Its log.** Each round writes what it did to `backups/auto-update.log`
(a round with nothing to do writes nothing). To see the newest lines:

```sh
tail -n 30 backups/auto-update.log
```

**Other commands:**

| Command | What it does |
|---|---|
| `./scripts/auto-update.sh` | One round now, and says where things stand. |
| `./scripts/auto-update.sh --test-alert` | Posts a test message in your Discord channel. |
| `./scripts/auto-update.sh --uninstall` | Turns the rounds off. Update by hand again. |

**Good to know:**

- `.env` is never changed by an update. A new setting uses its default
  until you add it to `.env` (see
  [Getting the newest notes in .env](settings.md#getting-the-newest-notes-in-env)).
- Files you changed on the server, such as `config/sources.yaml`, are kept.
  If an update changes the same file, the update stops, and Discord shows
  "Automatic update stopped". Follow the steps under "If `git pull` says
  your local changes would be overwritten" above. Within 5 minutes after
  that, the update continues by itself.
- A version that failed to start is not tried again. The next new commit
  is. The site's messages from the failed start are in
  `backups/.auto-update/failed-update.log`.
- If a failed version changed the database, going back may not be enough,
  and Discord shows "Update failed; ... is down". The backup from just
  before the update is the newest file in `backups`; see
  [Restoring from a backup](#restoring-from-a-backup).
- Old versions of the program are deleted after each update to save disk
  space (`docker image prune`). Stories and photos are never touched.

---

## Alerts in Discord

GU Headlines can post a message in a Discord channel when something needs
your attention. Discord is a free chat app; its phone app shows each
message as a notification. These alerts come:

| Message | Posted by | When |
|---|---|---|
| ⚠️ *Name* has stopped working, ✅ *Name* works again | the worker | A news site, job board or event calendar has failed for a day (`ALERT_AFTER_HOURS`), and when it works again. |
| ⚠️ *Name* has gone quiet, ✅ *Name* has new stories again | the worker | A news site still answers, but has had no new story for much longer than usual ([Source health](#source-health)), and when a new story comes. |
| 🔔 A new story mentions *word* | the worker | A new story's headline or intro has one of your words ([Keyword alerts](#keyword-alerts)). |
| 🚨 *site* is not answering, ✅ *site* is back | the server ([automatic updates](#automatic-updates)) | The site didn't answer for about 10 minutes, and when it answers again. |
| ⚠️ news not checked for *N* hours, ✅ news checked again | the server | The worker hasn't checked the news for 3 hours. |
| 🚀 updated, ⚠️ update failed, ⚠️ automatic update stopped | the server | After each [automatic update](#automatic-updates). |
| ⚠️ the nightly backup failed, ⚠️ no backup for *N* hours, ✅ backups work again | the server | A [nightly backup](#backups) failed (once for each night), or none has worked for more than a day, and when one works again. |
| 📦 a backup was downloaded | the website | Each time a backup is downloaded from the stats page. If it wasn't you, change `STATS_PASSWORD` and the other passwords and keys in `.env`. |
| 🚨 *site* does not answer, ✅ *site* answers again | GitHub ([outside check](#watching-from-outside-the-server)) | The site didn't answer from outside for 3 minutes. |

Each problem is posted once, not every hour. Nothing is posted while all is
well.

### Source health

The private [stats page](settings.md#visitor-stats) (`/stats`, while
`STATS_PASSWORD` is set) ends with **Source health**: a table of the news
sites the worker reads, and one of the job boards and event calendars. A
note at the top of the stats page names the sources that need a look. Each
source is:

- **Working**: its latest check worked.
- **Failing**: its latest check went wrong. The table shows the error, and
  when the source last worked. After `ALERT_AFTER_HOURS` (a day), Discord
  gets an alert.
- **Quiet** (news sites only): its checks work, but it has had no new story
  for longer than its **Quiet after** time. Discord gets an alert at once,
  and one more when a new story comes. A quiet site's feed or pages may have
  changed, so the worker no longer finds the stories (open the site and look
  for new stories there), or the site may have stopped posting.
- **Not checked yet**: just added.

Sites post at different speeds, so each site gets its own **Quiet after**
time: twice the longest wait between two of its stories in the two months
before its newest story, and at least 2 days. A busy newspaper is quiet
after 2 days; a site that posts once a week, and once waited 10 days, is
quiet after 20 days. A site with fewer than 5 stories in those two months
posts too rarely to tell, so it gets at least 30 days. There's nothing to
set.

### Keyword alerts

Discord can also tell you about news: when a new story mentions a word you
choose, such as `typhoon` or `shooting`. Put the words in `.env`, with a
comma between them, and apply it:

```
ALERT_KEYWORDS=typhoon, shooting, power outage
```

```sh
docker compose up -d
```

- The worker looks at each new story's headline and intro (the first lines),
  after every check. One message lists the new stories that mention your
  words, with the news site, the words found and the link. A story is never
  posted twice.
- Upper and lower case don't matter, and neither do accents (`Hagatna` finds
  `Hagåtña`). Only whole words count, also with `s` or `es` after them:
  `shooting` finds `shootings`, but `fire` doesn't find `firefighters`.
- End a word with `*` to find every word that starts with it: `evacuat*`
  finds `evacuate`, `evacuated` and `evacuation`.
- A phrase of several words, such as `power outage`, must be found in that
  order.
- Muted stories (`muted:` in `config/sources.yaml`) are left out, like on
  the site.
- Only news from the last 2 days counts. When you add a news site, its older
  stories are not posted.
- `docker compose exec worker guheadlines alert-test` sends a test message
  that names your words, and the stats page names them too.

### Getting the webhook link

A **webhook** is a secret link that lets a program post messages in one
Discord channel. It can only post; it can't read your messages. Making one
is easiest on a computer, in the Discord app or at
[discord.com/app](https://discord.com/app) in a web browser.

1. Sign in to Discord. If you have no account, make a free one at
   [discord.com/register](https://discord.com/register).
2. Make your own Discord **server** (in Discord, a server is a private
   group, not a computer). In the column on the left, press the **+**
   button (**Add a Server**), then **Create My Own**, then **For me and my
   friends**. Give it a name, for example `GU Headlines alerts`, and press
   **Create**. It comes with a channel named **general**, which you can use.
3. Point at the channel name and press the gear button next to it
   (**Edit Channel**). Open **Integrations**, then **Webhooks**, then press
   **New Webhook**.
4. Press the new webhook to open it. Change its name to `GU Headlines` (the
   name shown on each alert), then press **Copy Webhook URL**. Press
   **Save Changes** if Discord shows that button.
5. On the server, put the link in `.env`:

   ```
   DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/1234567890123456789/AbCd...
   ```

   and apply it:

   ```sh
   docker compose up -d
   ```

6. Check it:

   ```sh
   docker compose exec worker guheadlines alert-test
   ```

   A test message shows in the channel within a few seconds.
7. **On your phone:** install the Discord app and sign in. Press and hold
   your server's icon, open **Notifications** and choose **All Messages**.
   Discord sends phone notifications only while you aren't using Discord on
   a computer.

Keep the link private, like a password: a person who has it can post in
your channel. If someone else learns it, delete the webhook (step 3), make a
new one, and put the new link in `.env` (and in GitHub, below).

### Watching from outside the server

The server's own checks can't post anything when the whole server is down
or cut off from the internet. GitHub can check the site from outside every
30 minutes and post in the same Discord channel. It's free for public
repositories. Only the owner of the repository on GitHub can set it up:

1. On GitHub, open the repository, then **Settings** › **Secrets and
   variables** › **Actions**.
2. On the **Secrets** tab, press **New repository secret**. Name:
   `DISCORD_WEBHOOK_URL`. Secret: the same webhook link as in `.env`. Press
   **Add secret**. GitHub never shows a secret again, also not in logs.
3. On the **Variables** tab, press **New repository variable**. Name:
   `SITE_CHECK_URL`. Value: your site's address, for example
   `https://guamheadlines.com`. Press **Add variable**.
4. Try it: open the **Actions** tab, choose **Site check** on the left, press
   **Run workflow**, then the green **Run workflow** button. Within a minute
   the run gets a green check mark: the site answers. Nothing is posted in
   Discord while the site works.

Good to know:

- GitHub starts scheduled checks some minutes late when it's busy, so an
  outage of the whole server can take up to an hour to be reported.
- While the site doesn't answer, each check run is marked as failed (a red
  ✗), and GitHub may also email you about it.
- GitHub pauses scheduled checks in a public repository after 60 days
  without any new commit. To turn them on again, open **Actions** ›
  **Site check** and press **Enable workflow**.

### When an alert comes

| Alert | What to do |
|---|---|
| *Name* has stopped working | Open the **Sources** page; the site's last error is listed there. See [Reading the Sources page](news-sources.md#reading-the-sources-page). To try the site now: `docker compose exec worker guheadlines check --source SLUG` (the slug is in `config/sources.yaml`). |
| *site* is not answering | On the server: `docker compose ps`. If a part isn't **Up**, start it with `docker compose up -d`, then look at `docker compose logs --tail 50 web caddy`. |
| news not checked for *N* hours | `docker compose logs --tail 50 worker` shows why; `docker compose restart worker` starts it again. |
| Update failed | The site runs the version before. The reason is in `backups/.auto-update/failed-update.log`; pass it on to whoever made the change. |
| Automatic update stopped | Read the message: usually a file changed on the server is in the way (see [Updating](#updating)). |
| The nightly backup failed | The message says why. "Not enough disk space": see [Disk space](#disk-space). Otherwise `docker compose logs --tail 30 backup` shows more; try again with `./scripts/site-backup.sh now`. |
| No backup for *N* hours | The backup part may have stopped: `docker compose ps backup`. Start it again with `docker compose up -d`. |
| *site* does not answer (from GitHub), but the server posted nothing | The whole server may be down. Look at it in your server provider's control panel, and restart it there if needed. GU Headlines starts again by itself when the server starts. |

---

## Disk space

The collection grows by roughly **2 to 4 GB a year**, almost all of it
photos. The photos stop growing at `IMAGE_STORAGE_GB` (5 GB unless you change
it): above it, the worker deletes the photos of the oldest stories, which then
show the news site's own photo. To see how much space the photos use:

```sh
docker compose exec worker guheadlines photos
```

To see how much Docker uses in all:

```sh
docker system df
```

Old versions of the program pile up after updates. Remove unused ones with:

```sh
docker image prune
```

(This never touches your stories or photos.) If disk space gets tight, lower
`IMAGE_STORAGE_GB` in `.env`, or set `STORE_IMAGES=false` to save no photos
at all; see [Changing settings](settings.md#photos).

The [nightly backups](#backups) use space too: about one copy of the photos,
plus the database (and the mailboxes) for each night kept.
`./scripts/site-backup.sh list` shows how much. To use less, make
`BACKUP_KEEP_DAYS` in `.env` smaller; the oldest backups go after the next
backup. A backup is skipped (and the stats page says so) when it would leave
less than 1 GB free, because the database stops working when the disk is
full.

---

## Putting it on the internet

The site always answers on port 80 with plain `http://`: at
`http://localhost` on the server itself, and at the server's address from
other devices (for example `http://192.168.1.50` at home, or
`http://203.0.113.5` on a rented server).

For a real address with https, such as `https://guamheadlines.com`, you need
a domain name and a server that the internet can reach: a rented server (a
"VPS", such as a RackNerd KVM server), or a home server with port forwarding.
**Caddy**, the `caddy` part of GU Headlines, gets the certificate for https
(the padlock) from Let's Encrypt and renews it by itself. A certificate is
given only for a domain name, never for a bare IP address.

1. **Get a domain name** from a domain registrar, such as Cloudflare,
   Porkbun or Namecheap. A `.com` name costs about 10 to 15 US dollars a
   year.
2. **Point it at your server.** In the registrar's DNS settings for the
   domain, add an **A record**:

   | Type | Name | Value |
   |---|---|---|
   | `A` | `@` (the domain itself) | your server's public IP address |
   | `A` | `www` (optional, for `www.guamheadlines.com`) | the same IP address |

   - On a rented server, the IP address is in the provider's control panel
     and in its welcome email. On a Linux server, `curl -4 ifconfig.me`
     prints it too.
   - At home, it's your internet connection's address (search "what is my
     IP" from the server's network). If your provider changes it now and
     then, use a "dynamic DNS" service.
   - On Cloudflare, set **Proxy status** to **DNS only** (grey cloud) for
     both records. Cloudflare's proxy gets in the way of the certificate.
   - Don't add an `AAAA` (IPv6) record unless you know IPv6 works on your
     server. Let's Encrypt and many visitors try IPv6 first, and a broken
     IPv6 address makes the site slow or unreachable for them.

   The change takes from a few minutes to a few hours to reach everyone. To
   check, run this on the server (with your own domain). It must print your
   server's IP address:

   ```sh
   getent hosts guamheadlines.com
   ```

3. **Make sure ports 80 and 443 are open.** Let's Encrypt checks the domain
   through one of them, and visitors use both.
   - **On a rented server:** some providers have a firewall in their control
     panel; if yours does, allow TCP 80, TCP 443 and UDP 443 there. On the
     server itself, Docker opens the ports it uses by itself, even when the
     `ufw` firewall is on. If you use `ufw`, add the rules anyway so they show
     what's open:

     ```sh
     sudo ufw allow 80/tcp
     sudo ufw allow 443/tcp
     sudo ufw allow 443/udp
     ```

     (`ufw allow OpenSSH` too, before you ever run `sudo ufw enable`, or you
     lock yourself out.)
   - **At home:** set your router to "port forward" 80 and 443 to the server.
   - **Is another program already using them?** Some servers come with a
     web server such as Apache or nginx running. Docker then stops with
     "address already in use". See what's there with
     `sudo ss -ltnp | grep -E ':(80|443) '`, and switch it off with
     `sudo systemctl disable --now apache2` (or `nginx`, `httpd`). If you
     installed Caddy on the server itself with an earlier version of this
     guide, switch that one off too: `sudo systemctl disable --now caddy`.
4. **Tell GU Headlines the domain.** In `.env`, set `SITE_DOMAIN` to your
   domain (no `https://`), with `www.` too if you added that record:

   ```
   SITE_DOMAIN=guamheadlines.com, www.guamheadlines.com
   ```

   then apply it:

   ```sh
   docker compose up -d
   ```

5. **Check it.** Within a minute, `https://guamheadlines.com` shows your
   site with a padlock, and `http://` addresses lead there. If it doesn't,
   look at what Caddy says:

   ```sh
   docker compose logs --tail 30 caddy
   ```

   | Caddy's log mentions | What it means |
   |---|---|
   | `NXDOMAIN`, `no valid A records` or `DNS problem` | The domain doesn't point at the server yet (step 2). Wait, then run `docker compose restart caddy`. |
   | `Timeout during connect`, `connection refused` or `firewall` | Let's Encrypt can't reach port 80 or 443 (step 3). |
   | `rateLimited` or `too many` | Let's Encrypt's limit on new certificates. Caddy tries again later by itself. Don't remove the `caddy_data` volume: it holds the certificates. |

Search engines are given `https://guamheadlines.com` (the first name in
`SITE_DOMAIN`) as the address of every page, so `www.` and the IP address
never count as a second copy of the site. Next, see
[Search engines](#search-engines).

**Can't open ports at home?** A
[Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
publishes the site without port forwarding, and Cloudflare provides the
https. Point it at `http://localhost:80`, leave `SITE_DOMAIN` empty, and set
`SITE_URL=https://guamheadlines.com` (your domain) instead.

Before making the site public, read the [note on content](../README.md#a-note-on-content).

### Search engines

The site is ready for Google and Bing: every page has a title, a summary,
one canonical address and a link preview card, and
`https://news.example.com/sitemap.xml` lists every page worth finding (the
home page, each topic and news site, the Jobs, Events and Sources pages, each
upcoming event's own page, and the archive down to each day). The event pages
describe their event in the form Google reads for its event results (see
[Each event's page](events.md#each-events-page)). Search engines find it through `robots.txt`.

To get listed sooner and see how people find the site:

1. Set `SITE_DOMAIN` (step 4 above).
2. Add the site to [Google Search Console](https://search.google.com/search-console).
   The **Domain** option asks you to add a DNS record at your registrar. The
   **URL prefix** option can instead use the **HTML tag** method: put its
   `content="..."` value in `.env` as `GOOGLE_SITE_VERIFICATION=...`, run
   `docker compose up -d`, then press **Verify**.
3. In Search Console, open **Sitemaps** and submit `sitemap.xml`.
4. Optionally do the same in [Bing Webmaster Tools](https://www.bing.com/webmasters),
   which can import the site from Search Console, or use
   `BING_SITE_VERIFICATION`. Bing also feeds DuckDuckGo and Yahoo.

New sites usually take a few days to a few weeks to appear in results.
Search results pages are kept out of search engines on purpose, and so are the
stored copies of the news sites' photos, so image searches lead to the
publishers' own pages.

---

## Moving to a new computer

1. On the old computer: stop the worker, so no news comes in after the
   backup (`docker compose stop worker`), then make a backup as one file:
   `./scripts/site-backup.sh export`. Copy that file to the new computer.
2. On the new computer: install Docker and Git, and download the code
   ([Getting started](getting-started.md) steps 1 to 3). Don't make a `.env`
   file: it comes from the backup.
3. Put the backup file in the new `GU-headlines` folder and run
   `./scripts/site-backup.sh restore site-backup-....tar` (with your file's
   name). It makes `.env` from the backup, puts back everything else, and
   starts the site. The first start builds the program, which takes a few
   minutes.
4. Switch the old computer off (`docker compose down`), so both aren't
   collecting news at the same time. If you turned on
   [automatic updates](#automatic-updates) there, turn them off first:
   `./scripts/auto-update.sh --uninstall`. Turn them on again on the new
   computer.

---

## Starting over or uninstalling

**Stop it** (everything is kept; `docker compose up -d` starts it again).
With [automatic updates](#automatic-updates) on, the next update starts it
again, and the server posts "is not answering" alerts; run
`./scripts/auto-update.sh --uninstall` first to stop those too:

```sh
docker compose down
```

**Delete all stories and photos and start fresh.** This cannot be undone;
make a backup first if in doubt. With [your own email server](email-server.md)
on, it deletes all mailboxes and mail too, and the mail server's DKIM key (you
would have to change that DNS record):

```sh
docker compose down -v
docker compose up -d --build
```

**Uninstall completely:** run `./scripts/auto-update.sh --uninstall` (if
you turned on [automatic updates](#automatic-updates)) and
`docker compose down -v`, then delete the `GU-headlines` folder.
