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

All four (`db`, `web`, `worker`, `caddy`) should say **Up**. To see what the
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

Your collection lives in two places:

1. **The database:** every story, its headline, intro, topics and dates.
   This is the important part.
2. **The saved photos**, in Docker's `media` storage.

### Backing up the database

```sh
./scripts/backup.sh
```

This saves a compressed copy to the `backups` folder, named with the date and
time, for example `backups/guheadlines-20260927-0315.sql.gz`, and deletes
backups older than 30 days. It's safe to run while the site is running.

### Backing up automatically every night

Linux and Mac have a built-in scheduler called **cron**. Open your schedule:

```sh
crontab -e
```

(If it asks which editor to use, pick `nano`.) Add this line at the bottom,
changing `/home/you/GU-headlines` to your folder's real location (run `pwd`
inside the folder to see it):

```
15 3 * * * cd /home/you/GU-headlines && ./scripts/backup.sh >> backups/backup.log 2>&1
```

Save and exit. It now runs every night at 3:15 AM (server time). The five
fields at the start are *minute, hour, day of month, month, day of week*.
`backups/backup.log` records each run, so you can check it works.

To keep backups for a different number of days, put `KEEP_DAYS=90` before
the command: `... && KEEP_DAYS=90 ./scripts/backup.sh >> ...`.

**Keep a copy somewhere else.** A backup on the same computer won't help if
its disk fails. Copy the `backups` folder to a USB drive, another computer or
a cloud drive now and then.

### Backing up the photos

Photos can be downloaded again only while the news sites still have them, so
back them up too. First find the storage's name:

```sh
docker volume ls
```

Look for the one ending in `_media`, for example `gu-headlines_media`. Then
(with your volume's name):

```sh
docker run --rm -v gu-headlines_media:/media -v "$PWD/backups":/backup alpine \
  tar czf /backup/photos-$(date +%Y%m%d).tgz -C /media .
```

This creates `backups/photos-YYYYMMDD.tgz`.

**Windows:** `backup.sh` needs a Linux-style shell. Run it from
[WSL](https://learn.microsoft.com/windows/wsl/install) or Git Bash.

If it fails with errors like `$'\r': command not found`, your copy was
downloaded before the project fixed its line endings. Fix it once, from the
same WSL or Git Bash window, then run the backup again:

```sh
sed -i 's/\r$//' scripts/backup.sh .env
```

---

## Restoring from a backup

Restoring **replaces** the current database with the backup.

1. Start only the database:

   ```sh
   docker compose up -d db
   ```

2. Load the backup (use your file's name):

   ```sh
   gunzip -c backups/guheadlines-20260927-0315.sql.gz | docker compose exec -T db psql -U guheadlines -d guheadlines
   ```

   Many lines scroll past; that's normal.
3. Start everything again:

   ```sh
   docker compose up -d
   ```

To restore photos (with your volume's name and file):

```sh
docker run --rm -v gu-headlines_media:/media -v "$PWD/backups":/backup alpine \
  tar xzf /backup/photos-20260927.tgz -C /media
```

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

It's a good idea to take a backup before updating: `./scripts/backup.sh`.

### Automatic updates

On a server that is always on, such as a rented server, GU Headlines can
update itself. Every 5 minutes the server checks whether the `main` branch
on GitHub has new commits (for example after a pull request is merged). If
it has, the server does the steps above by itself: it downloads the new
version, makes a database backup, and rebuilds and restarts the site. If the
new version doesn't answer within 5 minutes, the server goes back to the
version before. It also checks every 5 minutes that the site answers.

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
| ⚠️ *Name* has stopped working, ✅ *Name* works again | the worker | A news site or job board has failed for a day (`ALERT_AFTER_HOURS`), and when it works again. |
| 🚨 *site* is not answering, ✅ *site* is back | the server ([automatic updates](#automatic-updates)) | The site didn't answer for about 10 minutes, and when it answers again. |
| ⚠️ news not checked for *N* hours, ✅ news checked again | the server | The worker hasn't checked the news for 3 hours. |
| 🚀 updated, ⚠️ update failed, ⚠️ automatic update stopped | the server | After each [automatic update](#automatic-updates). |
| 🚨 *site* does not answer, ✅ *site* answers again | GitHub ([outside check](#watching-from-outside-the-server)) | The site didn't answer from outside for 3 minutes. |

Each problem is posted once, not every hour. Nothing is posted while all is
well.

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
| *site* does not answer (from GitHub), but the server posted nothing | The whole server may be down. Look at it in your server provider's control panel, and restart it there if needed. GU Headlines starts again by itself when the server starts. |

---

## Disk space

The collection grows by roughly **2 to 4 GB a year**, almost all of it
photos. To see how much Docker uses:

```sh
docker system df
```

Old versions of the program pile up after updates. Remove unused ones with:

```sh
docker image prune
```

(This never touches your stories or photos.) If disk space gets tight, you
can set `STORE_IMAGES=false` in `.env`; see [Changing settings](settings.md#photos).

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

1. On the old computer: make a [database backup](#backing-up-the-database) and
   a [photo backup](#backing-up-the-photos). Copy the `backups` folder, your
   `.env` file, and the `config` folder if you changed it.
2. On the new computer: follow [Getting started](getting-started.md) steps 3
   and 4, then put your `.env` and `config` into the new `GU-headlines`
   folder (instead of step 5), and the `backups` folder too.
3. [Restore the database and photos](#restoring-from-a-backup), then run
   `docker compose up -d --build`.
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
make a backup first if in doubt:

```sh
docker compose down -v
docker compose up -d --build
```

**Uninstall completely:** run `./scripts/auto-update.sh --uninstall` (if
you turned on [automatic updates](#automatic-updates)) and
`docker compose down -v`, then delete the `GU-headlines` folder.
