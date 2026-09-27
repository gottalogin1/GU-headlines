# Getting started

This guide takes you from nothing to a working GU Headlines website. It takes
about 20 minutes, most of it waiting for downloads. You will type (or paste) a
few commands; each one is explained.

**Contents**

1. [What you need](#1-what-you-need)
2. [Words used in this guide](#2-words-used-in-this-guide)
3. [Install Docker](#3-install-docker)
4. [Download GU Headlines](#4-download-gu-headlines)
5. [Create your settings file](#5-create-your-settings-file)
6. [Start it](#6-start-it)
7. [Open the website](#7-open-the-website)
8. [Watch the first news arrive](#8-watch-the-first-news-arrive)
9. [Everyday commands](#9-everyday-commands)
10. [If something goes wrong](#10-if-something-goes-wrong)

---

## 1. What you need

- **A computer that stays switched on.** GU Headlines checks the news every
  hour, so it needs to be running all the time. Good choices:
  - a small home server or mini PC running Linux (Ubuntu or Debian are the
    easiest),
  - a rented cloud server (a "VPS"), or
  - a Raspberry Pi 4 or 5 with the 64-bit operating system (it should work,
    but has not been tested).

  To try it out first, your own Windows or Mac computer works fine.
- **About 2 GB of free memory and 10 GB of free disk space.** The collection
  grows by roughly 2 to 4 GB a year, mostly photos.
- **An internet connection.** A home or business connection is best: some
  news sites block traffic that comes from cloud servers (see the
  [README](../README.md#the-news-sites)).

## 2. Words used in this guide

| Word | Meaning |
|---|---|
| **Terminal** | The window where you type commands. On Linux and Mac it is called *Terminal*; on Windows use *PowerShell*. |
| **Command** | A line you type in the terminal and run by pressing **Enter**. In this guide commands are in grey boxes. Copy them exactly. |
| **Docker** | A free program that runs GU Headlines and its database in sealed-off boxes called **containers**, so you don't have to install anything else. |
| **Container** | One running part of GU Headlines. There are three: `db` (the database), `web` (the website) and `worker` (the program that collects the news). |
| **`.env` file** | A small text file holding your settings, such as your database password. |
| **Port** | A number that tells your browser which program on a computer to talk to. GU Headlines uses port 8000 unless you change it. |

## 3. Install Docker

Docker does all the heavy lifting. Install it once.

### On Linux (Ubuntu, Debian and similar)

Open a terminal and run these one at a time:

```sh
sudo apt update
sudo apt install -y git curl
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
```

What these do:
- The first two install `git` (for downloading GU Headlines) and `curl`.
- The third runs Docker's official installer.
- The last one lets you use Docker without typing `sudo` every time.

**Log out and log back in** (or restart the computer) so the last command
takes effect. Then check that Docker works:

```sh
docker compose version
```

You should see something like `Docker Compose version v2.x.x`. Also make sure
Docker starts by itself when the computer boots:

```sh
sudo systemctl enable docker
```

### On Windows or Mac

1. Download **Docker Desktop** from <https://www.docker.com/products/docker-desktop/>
   and install it.
2. Open Docker Desktop and wait until it says it is running.
3. In Docker Desktop's settings, turn on **Start Docker Desktop when you sign
   in**, so GU Headlines keeps running after a restart.
4. Install **Git** from <https://git-scm.com/downloads> (Windows), or run
   `xcode-select --install` in Terminal (Mac).

## 4. Download GU Headlines

In the terminal, go to the folder where you want to keep it (your home folder
is fine) and run:

```sh
git clone https://github.com/gottalogin1/GU-headlines.git
cd GU-headlines
```

This creates a folder called `GU-headlines` and moves you into it.

> **Important:** every command in these guides must be run from inside the
> `GU-headlines` folder. If you open a new terminal later, first type
> `cd GU-headlines` (or `cd ~/GU-headlines`).

No Git? On the GitHub page, click the green **Code** button, then
**Download ZIP**, unzip it, and open a terminal in that folder. (Updating is
easier with Git, so Git is recommended.)

## 5. Create your settings file

GU Headlines comes with a settings template called `.env.example`. Make your
own copy called `.env`:

```sh
cp .env.example .env
```

(On Windows PowerShell: `Copy-Item .env.example .env`)

Now choose a database password. It protects your database; you will never
need to type it anywhere else. This command makes a strong one for you:

```sh
openssl rand -hex 24
```

It prints something like `3f9c1e0b7a...`. Copy it.

Open the settings file in a text editor:

```sh
nano .env
```

(On Windows: `notepad .env`. On Mac you can also use `open -e .env`.)

Find this line:

```
POSTGRES_PASSWORD=change-me-to-a-long-random-string
```

Replace the part after `=` with your password, with no spaces or quotes:

```
POSTGRES_PASSWORD=3f9c1e0b7a...
```

Save and close. In `nano`: press **Ctrl+O**, then **Enter** to save, then
**Ctrl+X** to exit.

> **Choose your password now and keep it.** The database remembers the
> password it was first started with. Changing it in `.env` later will lock
> GU Headlines out of its own database (see
> [If something goes wrong](#10-if-something-goes-wrong)).

Everything else in `.env` can stay as it is. [Changing settings](settings.md)
explains every line when you are ready.

## 6. Start it

```sh
docker compose up -d --build
```

What happens:
- `--build` prepares GU Headlines. The first time takes 3 to 10 minutes and
  prints a lot of text; that is normal.
- `-d` ("detached") keeps it running in the background after the command
  finishes and after you close the terminal.

When it finishes, check that all three parts are running:

```sh
docker compose ps
```

You should see `db`, `web` and `worker`, each with a status of **Up** (`db`
also shows **healthy**).

## 7. Open the website

- **On the same computer:** open your web browser and go to
  **<http://localhost:8000>**
- **From another computer, phone or tablet on your network:** find your
  server's address with

  ```sh
  hostname -I
  ```

  It prints something like `192.168.1.50`. On the other device, go to
  `http://192.168.1.50:8000` (with your own address).

  If it doesn't load and your server uses the `ufw` firewall, allow the port:
  `sudo ufw allow 8000/tcp`.

At first the page says **No stories here yet**. That's expected; the news
arrives in the next step.

To make the site reachable on the internet with your own web address (like
`news.example.com`), see [Looking after it](maintenance.md#putting-it-on-the-internet).

## 8. Watch the first news arrive

The news collector (the `worker`) starts checking all the news sites as soon
as it starts. To watch it work:

```sh
docker compose logs -f worker
```

You'll see lines like:

```
[postguam] 101 candidates, 40 new, 0 failed, 0 skipped
scrape finished in 290s: 139 new articles from 13 sources
not fully loaded yet: postguam (11 pages), kuam (30 pages)
catching up at 18:51:02 UTC: postguam (11 pages), kuam (30 pages)
next scrape at 2026-09-27 19:00 UTC
```

In plain words:
- **candidates** are links it found; **new** are stories it saved.
- **not fully loaded yet** / **catching up** means some sites had more new
  stories than one round takes, or asked it to slow down. It comes back for
  them every minute until they are done.
- **next scrape at** is the next regular hourly check. Times in the log are in
  UTC; Guam is UTC+10.

Press **Ctrl+C** to stop watching. This only closes the log view; GU
Headlines keeps running.

Refresh the website: stories appear as they are saved. The first full load
usually takes 5 to 15 minutes.

Now open the **Sources** page on your site (link at the top). Each news site
should show a recent **Last check** time. A red **⚠** message means that site
had a problem; [News sources](news-sources.md#reading-the-sources-page)
explains what each message means.

## 9. Everyday commands

Run these from inside the `GU-headlines` folder.

| To do this | Run |
|---|---|
| See whether it's running | `docker compose ps` |
| Watch the news collector | `docker compose logs -f worker` (Ctrl+C to stop watching) |
| See website errors | `docker compose logs --tail 100 web` |
| Check for news right now, without waiting for the hour | `docker compose exec worker guheadlines scrape` |
| Apply changes you made to `.env` | `docker compose up -d` |
| Stop everything (data is kept) | `docker compose stop` |
| Start again after stopping | `docker compose start` |
| Update to the newest version | see [Looking after it](maintenance.md#updating) |

GU Headlines starts by itself when the computer restarts, as long as Docker
does (step 3 set that up).

> **Never run `docker compose down -v`** unless you really want to start over.
> The `-v` deletes the database and all saved photos. Plain
> `docker compose down` is safe: it removes the containers but keeps your
> data.

## 10. If something goes wrong

**"permission denied while trying to connect to the Docker daemon"**
You haven't logged out and back in since installing Docker (step 3). Do that,
or put `sudo` in front of the command for now.

**"set POSTGRES_PASSWORD in .env"**
The `.env` file is missing, or the password line is empty. Go back to
[step 5](#5-create-your-settings-file). Check the file is called exactly
`.env` (with the dot) and sits in the `GU-headlines` folder.

**"port is already allocated" or "address already in use"**
Another program already uses port 8000. Open `.env`, change `WEB_PORT=8000` to
another number such as `WEB_PORT=8080`, then run `docker compose up -d` and
use `http://localhost:8080`.

**The page won't load**
Run `docker compose ps`. If `web` is not **Up**, run
`docker compose logs --tail 50 web` and look at the last lines for an error.
From another device, also check the firewall note in
[step 7](#7-open-the-website).

**The page loads but no stories appear after 15 minutes**
Run `docker compose logs --tail 100 worker`. Lines ending in
`ConnectError`, `ProxyError` or `Name or service not known` mean the server
can't reach the internet. `HTTP 403` on every site means your internet
address is being blocked; that's rare on home connections.

**"password authentication failed for user guheadlines"**
The password in `.env` no longer matches the one the database was created
with (usually because it was changed after the first start). Put the original
password back in `.env` and run `docker compose up -d`. If you've lost it and
have **no stories you want to keep** yet, you can start fresh with
`docker compose down -v` followed by `docker compose up -d --build`. This
deletes everything collected so far.

Still stuck? The logs usually say what's wrong:
`docker compose logs --tail 200 worker web db`.
