# Looking after it

GU Headlines mostly runs by itself. This guide covers the occasional jobs:
checking it's healthy, backups, updates, and putting it on the internet.

All commands are run from inside the `GU-headlines` folder.

**Contents**

- [Is it healthy?](#is-it-healthy)
- [Backups](#backups)
- [Restoring from a backup](#restoring-from-a-backup)
- [Updating](#updating)
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

All three (`db`, `web`, `worker`) should say **Up**. To see what the news
collector has been doing:

```sh
docker compose logs --tail 50 worker
```

**Automatic monitoring (optional).** The address `/healthz` (for example
`http://your-server:8000/healthz`) answers like this:

```json
{"status": "ok", "last_scrape": "2026-09-27T19:00:41+00:00"}
```

Free services such as [UptimeRobot](https://uptimerobot.com/) or a
self-hosted [Uptime Kuma](https://github.com/louislam/uptime-kuma) can check
this address every few minutes and email you if the site stops answering.

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

When a new version of GU Headlines is published:

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

By default the site is reachable only on your own network. To give it a real
address such as `https://news.example.com`:

1. **Get a domain name** from any domain registrar.
2. **Point it at your server:** in the registrar's DNS settings, add an
   **A record** for `news` (or `@` for the bare domain) with your server's
   public IP address.
   - On a cloud server, that's the IP address your provider shows.
   - At home, it's your internet connection's address (search "what is my IP"
     from the server's network). If your provider changes it now and then, use
     a "dynamic DNS" service.
3. **Open the doors:** allow ports **80** and **443**. On a cloud server, do
   this in the provider's firewall settings. At home, set your router to
   "port forward" 80 and 443 to the server.
4. **Add HTTPS with Caddy.** [Caddy](https://caddyserver.com/docs/install) is
   a free web server that gets and renews the security certificate
   (the padlock) for you. Install it, then replace the contents of
   `/etc/caddy/Caddyfile` with (your own domain):

   ```
   news.example.com {
       reverse_proxy 127.0.0.1:8000
   }
   ```

   and reload it:

   ```sh
   sudo systemctl reload caddy
   ```

   Within a minute, `https://news.example.com` shows your site.

**Can't open ports at home?** A
[Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
publishes the site without port forwarding; point it at
`http://localhost:8000`.

Before making the site public, read the [note on content](../README.md#a-note-on-content).

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
   collecting news at the same time.

---

## Starting over or uninstalling

**Stop it** (everything is kept; `docker compose up -d` starts it again):

```sh
docker compose down
```

**Delete all stories and photos and start fresh.** This cannot be undone;
make a backup first if in doubt:

```sh
docker compose down -v
docker compose up -d --build
```

**Uninstall completely:** run `docker compose down -v`, then delete the
`GU-headlines` folder.
