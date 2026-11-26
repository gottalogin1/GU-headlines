# Keeping the site safe

The site guards itself against the usual attacks (the first part below). A
few things can only be done on the server itself, by you: they take about
half an hour, once (the second part). The last part says what to do if you
ever think someone got in.

**Contents**

- [What the site does by itself](#what-the-site-does-by-itself)
- [What you do on the server](#what-you-do-on-the-server)
  1. [Sign in with a key instead of a password](#1-sign-in-with-a-key-instead-of-a-password)
  2. [Turn off password sign-in](#2-turn-off-password-sign-in)
  3. [Block password guessers](#3-block-password-guessers)
  4. [Turn on the firewall](#4-turn-on-the-firewall)
  5. [Let Linux install its security updates](#5-let-linux-install-its-security-updates)
  6. [Use a long STATS_PASSWORD](#6-use-a-long-stats_password)
  7. [Two-step sign-in on your accounts](#7-two-step-sign-in-on-your-accounts)
- [If you think someone got in](#if-you-think-someone-got-in)

---

## What the site does by itself

- **Private pages.** `/stats`, `/admin/listings`, `/admin/guides`,
  `/advertise/review` and the backup downloads need `STATS_PASSWORD`. After
  10 wrong passwords within 15 minutes, that address is blocked for an hour,
  even if it then types the right one, and Discord gets an alert (at most
  one an hour). Typing no password at all (the browser's first visit) does
  not count. The stats page says so when the password is short.
- **Backups.** A backup holds every password of the site, so it can only be
  downloaded over https, with a `STATS_PASSWORD` of at least 12 characters,
  and each download is posted in Discord.
- **Buttons on the private pages** work only from the site's own pages:
  another website can't make your browser press them.
- **Payments.** Prices are set by the server, never by the buyer's browser.
  When a buyer comes back from PayPal, the site checks with PayPal that the
  payment is for this very listing, ad or renewal, and for its price. One
  payment can't pay for two things.
- **Pages** carry strict security rules (Content-Security-Policy): no
  scripts run on them except the site's own, and AdSense on the pages with
  ads. Text that visitors type (listings, ads) is always shown as text.
- **Links** through `/go/...` only lead to addresses that the site has
  stored (a story, an approved ad), never to an address someone puts in the
  link.
- **Uploads** must be real pictures (JPG, PNG, WebP, GIF), and are saved as
  a new WebP copy, which leaves anything hidden in the file behind. Requests
  larger than 20 MB are refused by Caddy before they reach the site.
- **The server.** Only Caddy answers from the internet (ports 80 and 443,
  plus the mail ports with your own mail server). The database and the
  website itself can't be reached from outside. The site's own programs
  run without special Linux permissions.
- **Updates.** With [automatic updates](maintenance.md#automatic-updates)
  on, the site updates itself when its code changes, and once a week it
  downloads new versions of the database, Caddy, Python and the mail server,
  which bring security fixes. The [server checks](maintenance.md#server-checks)
  make `.env` and the `backups` folder readable by their owner only, and
  post in Discord when Linux needs a restart to finish its own security
  updates.

## What you do on the server

These steps are for Ubuntu or Debian, which RackNerd servers usually run.
Do them in order. Commands start with `sudo`; when you sign in as `root`,
they work the same.

> Before you start: find the **VNC console** (or "Console") in your
> RackNerd control panel. It opens the server's screen in your browser,
> also when ssh doesn't let you in. If a step below ever locks you out,
> sign in there with the server's root password and undo the step.

### 1. Sign in with a key instead of a password

A key is a pair of long secret files: one stays on your computer, the other
goes on the server. Nobody can guess it, unlike a password.

1. On your Windows computer, open **PowerShell** and run:

   ```powershell
   ssh-keygen -t ed25519
   ```

   Press Enter to keep the file name it suggests. It then asks for a
   passphrase: a password for the key itself, which you type when you sign
   in. Use one (a few words are fine), so a stolen laptop doesn't open your
   server.

2. Copy the key to the server (put your server's IP address in place of
   `203.0.113.10`; it asks for the server's password one last time):

   ```powershell
   type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh root@203.0.113.10 "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
   ```

3. Check it: open a **new** PowerShell window and run
   `ssh root@203.0.113.10`. It should let you in after the key's passphrase,
   without the server's password. Keep this window open for step 2.

### 2. Turn off password sign-in

Only after step 1 works. On the server:

```sh
sudo tee /etc/ssh/sshd_config.d/10-no-passwords.conf > /dev/null <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
sudo sshd -t && (sudo systemctl restart ssh || sudo systemctl restart sshd)
```

`sshd -t` checks the settings first; when it prints nothing, they are fine.
Then, in another **new** PowerShell window, sign in again with
`ssh root@203.0.113.10`: it must still work. To see that passwords are off:

```sh
sudo sshd -T | grep -E '^(passwordauthentication|permitrootlogin)'
```

It shows `passwordauthentication no` and `permitrootlogin without-password`.

To sign in from another computer later, repeat step 1 on that computer
(steps 1 and 2 of it from a computer that can already sign in). To undo this
step, delete the file and restart ssh: `sudo rm
/etc/ssh/sshd_config.d/10-no-passwords.conf && sudo systemctl restart ssh`.

### 3. Block password guessers

Robots try passwords on every server on the internet, all day. Fail2Ban
blocks an address for an hour after 5 wrong tries within 10 minutes:

```sh
sudo apt update && sudo apt install -y fail2ban python3-systemd
sudo tee /etc/fail2ban/jail.d/sshd.local > /dev/null <<'EOF'
[sshd]
enabled = true
backend = systemd
maxretry = 5
findtime = 10m
bantime = 1h
EOF
sudo systemctl enable fail2ban && sudo systemctl restart fail2ban
sudo fail2ban-client status sshd
```

The last line shows how many addresses are blocked now. If you ever block
yourself (for example from a new place, before its key is set up), wait an
hour, or unblock your address from the VNC console:
`sudo fail2ban-client set sshd unbanip YOUR.IP.ADDRESS`. (Your own mail
server has a Fail2Ban of its own for mail passwords: see
[email-server.md](email-server.md).)

### 4. Turn on the firewall

The firewall refuses connections to anything on the server that isn't meant
for the internet:

```sh
sudo apt install -y ufw
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow 443/udp
sudo ufw enable
sudo ufw status
```

Allow `OpenSSH` before `ufw enable`, or ssh stops working. With your own
mail server, also allow its ports: `sudo ufw allow 25,465,587,993/tcp`.

Good to know: Docker opens the ports of its containers (80, 443 and the
mail ports) by itself, past `ufw`. That is fine: those are meant to be open,
and the database and the website have no ports outside Docker at all. The
firewall protects ssh and anything else that runs on the server.

### 5. Let Linux install its security updates

```sh
sudo apt install -y unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades
```

Answer **Yes**. Linux then installs its security updates every day by
itself. Some (such as a new Linux kernel) only work after a restart: the
[server checks](maintenance.md#server-checks) post in Discord when that is
so. Then run `sudo reboot` when it suits you; the site is back by itself
within a minute or two.

Once a month or so, also run `sudo apt update && sudo apt upgrade`, which
updates the rest (Docker too).

### 6. Use a long STATS_PASSWORD

It opens every private page and the backups. Make one of 32 letters and
digits on the server:

```sh
openssl rand -hex 16
```

Put it after `STATS_PASSWORD=` in `.env`, then run `docker compose up -d`.
Keep it in a password manager (Bitwarden is free), and don't type it on
someone else's computer. The browser remembers it until you close the
browser; there is no "log out" button, so close the browser when you are
done on a shared computer.

### 7. Two-step sign-in on your accounts

Whoever gets into these accounts can take over the site, so turn on two-step
sign-in (a code from an app such as Google Authenticator, or a security key)
on each:

- **GitHub** (most important): the server installs whatever reaches the
  `main` branch, by itself. Settings, then Password and authentication, then
  Two-factor authentication.
- **PayPal**: Settings, then Security, then 2-step verification.
- **RackNerd**: the client area and the control panel. Use a long, unique
  password there too.
- Your **domain registrar** (where guamheadlines.com is registered): someone
  in it could point the domain elsewhere.
- **Google** (AdSense), **Discord** and **SendFox**.

The Discord webhook link in `.env` is like a password too: anyone who has it
can post in your alerts channel.

## If you think someone got in

1. Make new passwords and keys, and put them in `.env`:
   - `STATS_PASSWORD`: a new one (step 6).
   - `PAYPAL_CLIENT_SECRET`: in the PayPal developer dashboard, open the app
     and make a new secret, then delete the old one.
   - `DISCORD_WEBHOOK_URL`: in Discord, delete the webhook and make a new one
     ([how](maintenance.md#getting-the-webhook-link)).
   - `SENDFOX_API_TOKEN`, `USAJOBS_API_KEY`, `GOOGLE_CALENDAR_API_KEY` and
     other keys: make new ones on those sites.
   - With your own mail server: new mailbox passwords
     (`./scripts/mail.sh password NAME@DOMAIN`, and the site's own mailbox,
     whose password is `SMTP_PASSWORD`).

   Then run `docker compose up -d`.
2. Look at who signed in to the server: `last -a | head -20`, and the ssh
   log: `sudo journalctl -u ssh --since "7 days ago" | grep -i accepted`.
3. Look at the site's log for blocked addresses and backup downloads:
   `docker compose logs --since 168h web | grep -E "blocked|backup"`.
4. Look at PayPal's activity for payments and refunds you don't know.
5. If the server itself may be taken over (programs you didn't install,
   ssh keys you didn't add in `~/.ssh/authorized_keys`), the safe way is a
   fresh server: [move the site](maintenance.md#moving-to-a-new-computer)
   with a backup from before, and make all the new passwords of step 1.
