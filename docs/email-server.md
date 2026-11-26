# Your own email server

GU Headlines can run its own mail server next to the website. With it you get:

- **Email addresses at your domain**, such as you@guamheadlines.com, as many as
  you like.
- **Webmail** at `https://mail.guamheadlines.com`: read and write your mail in
  the browser (Roundcube). Mail apps on your phone and computer work too.
- **The site's own emails** (the bid alerts) go out from your server, not
  through an email company.

It runs as two more containers: `mailserver` ([docker-mailserver]: Postfix,
Dovecot, the Rspamd spam filter and Fail2Ban) and `webmail` ([Roundcube]).
They stay off until you turn them on. Together they use about 350 MB of memory.

**Good to know before you start.** Big mail services (Gmail, Outlook,
Yahoo) trust a new mail server slowly. The steps below (reverse DNS, SPF,
DKIM and DMARC) are what they check first, so do all of them. Even then, the
first emails can land in spam, and Microsoft (Outlook, Hotmail and Microsoft
365) sometimes refuses mail from a new server for a while. Step 9 tells you
what to do then.

[docker-mailserver]: https://docker-mailserver.github.io/docker-mailserver/latest/
[Roundcube]: https://roundcube.net/

## What you need

- The site already works over https at your domain (`SITE_DOMAIN` in `.env`,
  see [maintenance.md](maintenance.md#putting-it-on-the-internet)).
- The DNS settings of your domain, at your domain registrar (or Cloudflare).
- The client area of your server company (RackNerd), to open a support
  ticket.
- About 600 MB of free memory. To see it, run `free -h` on the server and look
  at the "available" column.

All commands below run on the server, in the `GU-headlines` folder.

## Step 1. Check the server

```
./scripts/mail.sh check
```

It changes nothing. Each line starts with `OK`, `FIX` (something to do) or
`??` (could not check). Lines that say `FIX` now are normal: the next steps fix
them. Run it again after each step.

If the "dig" checks say `??`, install the program they need:

```
sudo apt install dnsutils
```

## Step 2. Stop a mail program that came with Linux

If `check` says "port 25 is used by another program", a mail program that came
with Linux is in the way. Stop it for good:

```
sudo systemctl disable --now postfix exim4 sendmail
```

(Messages about a program that is not installed are fine.)

## Step 3. Point mail.guamheadlines.com at the server

In the DNS settings of your domain, add one record, like the one you added for
the website:

| Type | Name | Value |
|---|---|---|
| A | `mail` | the IP address of your server |

On Cloudflare, set "Proxy status" to **DNS only** (grey cloud). Do not add an
AAAA record for `mail`.

Wait until it works. This command must print the IP address of your server:

```
getent hosts mail.guamheadlines.com
```

## Step 4. Ask RackNerd for port 25 and reverse DNS

Two things only your server company can do:

- **Outgoing port 25.** Mail servers deliver to each other on port 25. Many
  server companies block it on new servers against spam. If `check` says "it
  cannot reach other mail servers on port 25", it is blocked.
- **Reverse DNS** (also called PTR). It is the other direction of the A record:
  your server's IP address points back at `mail.guamheadlines.com`. Gmail
  refuses or sends to spam mail from servers without it. Only the company that
  owns the IP address can set it.

Open a support ticket in the RackNerd client area, with your own IP address
and name:

> Hello. I run a small mail server for my own domain on my VPS, IP address
> 203.0.113.5. Could you please (1) make sure that outgoing port 25 is open,
> and (2) set the reverse DNS (PTR) of 203.0.113.5 to mail.guamheadlines.com?
> The A record of mail.guamheadlines.com already points to this IP address.
> Thank you.

You can go on with the next steps while you wait. When they answer, run
`./scripts/mail.sh check`: the port 25 and reverse DNS lines must say `OK`.

## Step 5. Turn it on in .env

Open `.env` and set these two lines (add them if they are missing; their notes
are in `.env.example`, under "Your own email server"):

```
COMPOSE_PROFILES=mail
MAIL_HOSTNAME=mail.guamheadlines.com
```

## Step 6. Run the setup

```
./scripts/mail.sh setup
```

It asks for **your own address**, such as `sanjay@guamheadlines.com`, and a
password for it (at least 12 characters; nothing shows while you type). Then
it:

1. makes your mailbox, and sends mail for `postmaster@` and `abuse@` your
   domain to it (mail services expect these two addresses to work);
2. makes the **site's mailbox**, `alerts@guamheadlines.com`, with a random
   password, and prints five lines for `.env`;
3. starts the mail server and the webmail (Caddy gets the certificate for
   `mail.guamheadlines.com`, which takes up to a minute);
4. makes the DKIM key and lists the DNS records for step 7.

Put the five lines that it printed into `.env`. If `.env` has the empty
`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` and `MAIL_FROM` lines,
fill those in instead of adding new ones. They look like this:

```
SMTP_HOST=mail.guamheadlines.com
SMTP_PORT=587
SMTP_USER=alerts@guamheadlines.com
SMTP_PASSWORD=(the random password it printed)
MAIL_FROM=GU Headlines <alerts@guamheadlines.com>
```

Then apply them:

```
docker compose up -d
```

The site signs in to the mail server as `alerts@guamheadlines.com`, so
`MAIL_FROM` must use that same address. Inside Docker, `mail.guamheadlines.com`
leads straight to the mail server, so these emails never leave the server
before they go out.

## Step 7. Add the DNS records

```
./scripts/mail.sh dns
```

It lists the records with your own IP address and DKIM key filled in, and
after each one whether it is "in place", "missing" or "different now". Add
them where you added the A record in step 3. Keep the records that are there
already for the website.

| Type | Name | Value | What it does |
|---|---|---|---|
| A | `mail` | your server's IP address | Done in step 3. |
| MX | `@` | `mail.guamheadlines.com`, priority `10` | Mail for your addresses comes to this server. Remove any other MX record. |
| TXT | `@` | `v=spf1 mx ~all` | SPF: says that your mail server may send mail for your domain. |
| TXT | `mail` | `v=spf1 a ~all` | SPF for the mail server's own name (used for bounce messages). |
| TXT | `mail._domainkey` | `v=DKIM1; k=rsa; p=...` (long) | DKIM: the key that checks the signature on each email. Copy it from `mail.sh dns` in one piece. |
| TXT | `_dmarc` | `v=DMARC1; p=none; rua=mailto:postmaster@guamheadlines.com` | DMARC: tells mail services what to do with mail that fails SPF and DKIM, and to send you reports. |

"`@`" means the domain itself. If your registrar asks for "Host" instead of
"Name", it is the same thing. Some registrars want the full name, such as
`mail._domainkey.guamheadlines.com`.

New records take from a few minutes to a few hours to show. Run
`./scripts/mail.sh dns` again until each one says "in place".

## Step 8. Test it

1. **The site's email.** Send a test email the way the bid alerts go:

   ```
   docker compose exec web guheadlines mail-test you@gmail.com
   ```

   Use an address that you can read, at Gmail if you have one. In Gmail, open
   the email, click the three dots, then **Show original**. SPF, DKIM and DMARC
   must all say `PASS`.

2. **Your webmail.** Open `https://mail.guamheadlines.com` and sign in with
   your whole address and password. Send an email to your Gmail address, and
   answer it from Gmail: the answer must arrive in the webmail.

3. **A score.** Open [mail-tester.com](https://www.mail-tester.com/). It shows
   an address: send an email to it from the webmail, then click the button on
   the page. Aim for 9 out of 10 or more; the page says what to fix.

If an email does not arrive, see what happened:

```
./scripts/mail.sh logs
```

## Step 9. Sign up for the sender tools of Google and Microsoft

Both are free, and show how they see mail from your server.

- **Google Postmaster Tools**: [postmaster.google.com](https://postmaster.google.com/).
  Add `guamheadlines.com`; it asks you to add one more TXT record to prove the
  domain is yours.
- **Microsoft SNDS**: [sendersupport.olc.protection.outlook.com/snds](https://sendersupport.olc.protection.outlook.com/snds/).
  Sign in with a Microsoft account, and "Request access" for your server's IP
  address. Microsoft sends the confirmation to `postmaster@guamheadlines.com`,
  which comes to your mailbox.

**If Outlook or Hotmail refuses your mail**, the bounce message (it comes to
the mailbox that sent the email) mentions `S3150` or "block list". Fill in
Microsoft's form at [olcsupport.office.com](https://olcsupport.office.com/)
with your IP address and the error text, and say that it is a new, small mail
server for your own domain. They usually answer within a day or two.

## Using your mail

**Webmail**: `https://mail.guamheadlines.com`. Sign in with the whole address.
After 3 wrong passwords in a minute, the page makes you wait.

**Mail apps** (phone, Outlook, Thunderbird): choose "IMAP" or "other account",
and use:

| | Server | Port | Security |
|---|---|---|---|
| Incoming (IMAP) | `mail.guamheadlines.com` | `993` | SSL/TLS |
| Outgoing (SMTP) | `mail.guamheadlines.com` | `465` | SSL/TLS |

The user name is the whole address. (Port `587` with STARTTLS works for
outgoing too.)

**Spam** goes to the **Junk** folder. When you move an email into Junk, or out
of it ("Not junk"), the filter learns from it.

**More mailboxes and addresses**:

| What | Command |
|---|---|
| Make a mailbox | `./scripts/mail.sh add news@guamheadlines.com` |
| Give a mailbox a new password | `./scripts/mail.sh password you@guamheadlines.com` |
| Send an address's mail to a mailbox | `./scripts/mail.sh alias tips@guamheadlines.com you@guamheadlines.com` |
| Stop that | `./scripts/mail.sh unalias tips@guamheadlines.com you@guamheadlines.com` |
| See all mailboxes and aliases | `./scripts/mail.sh list` |
| Delete a mailbox and its mail | `./scripts/mail.sh remove news@guamheadlines.com` |

Each mailbox sends only as its own address (and the aliases that point to it),
so nobody who learns one password can send as another address.

None of these addresses shows on the website.

## Looking after it

**Updates.** With [automatic updates](maintenance.md#automatic-updates) on,
the server downloads new versions of the mail server and the webmail once a
week (they bring security fixes) and restarts them. Mail that arrives during
those few seconds waits at the sender and comes a little later. To update now:
`./scripts/mail.sh update`.

**Backups.** The [nightly backup](maintenance.md#backups) saves the mailboxes
and the mail server's settings (accounts, passwords, aliases, the DKIM key)
with the rest of the site, and the webmail's settings and contacts too. To put
back only the mail from a backup (a name from `./scripts/site-backup.sh list`):

```
./scripts/site-backup.sh restore 2026-11-26-0300 mail webmail
```

Mail that came after that backup is lost, so use the newest good backup.

**Blocked addresses.** Fail2Ban blocks an address for a week after 6 wrong
passwords in a week (in a mail app, for example). If that was you:

```
./scripts/mail.sh unban 203.0.113.5
```

(Your own address is shown at [whatismyip.com](https://www.whatismyip.com/).)
The webmail and the site are never blocked this way.

**DMARC reports.** Mail services send reports to `postmaster@` as attachments
(often `.zip` or `.gz`). After two to four weeks, if they show that your mail
passes, make DMARC stricter: change `p=none` to `p=quarantine` in the `_dmarc`
record. Then mail services send fake mail that claims to be from your domain to
spam.

## Block lists

`./scripts/mail.sh check` asks two big block lists (Spamhaus and Barracuda)
about your server's IP address. If one lists it, a server that had the same IP
address before you probably sent spam. Each list has a free removal form:
[Spamhaus](https://check.spamhaus.org/), [Barracuda](https://www.barracudacentral.org/rbl/removal-request).
[MXToolbox](https://mxtoolbox.com/blacklists.aspx) checks many lists at once.

## If something goes wrong

**`setup` says Caddy has no certificate for mail.guamheadlines.com.** The A
record of step 3 must point at the server, and ports 80 and 443 must be open
(they are, if the site works over https). See what Caddy says:
`docker compose logs --tail 30 caddy`. The mail server starts by itself once
the certificate is there; then run `./scripts/mail.sh dns`.

**The mail server keeps restarting.** See why:
`docker compose logs --tail 50 mailserver`. "certificate ... not found": see
above. "at least one mail account": run `./scripts/mail.sh setup` again.

**The webmail says "Connection to storage server failed".** The mail server is
not running: `docker compose ps`, then the point above.

**Mail to Gmail goes to spam.** Check step 8.1: SPF, DKIM and DMARC must say
`PASS`, and `./scripts/mail.sh check` must say `OK` for reverse DNS. A new
server needs a few weeks of normal mail to build trust. When people move your
emails out of spam, that helps too.

**No mail arrives at your addresses.** Check the MX record with
`./scripts/mail.sh dns`. Mail servers that tried while it was missing try
again for a few days.

**`mail-test` from the site fails.** "refused SMTP_USER and SMTP_PASSWORD":
give the site's mailbox a new password with
`./scripts/mail.sh password alerts@guamheadlines.com`, put it in
`SMTP_PASSWORD`, and run `docker compose up -d`. "Sender address rejected":
`MAIL_FROM` must use the same address as `SMTP_USER`.

## Turning it off

Empty `COMPOSE_PROFILES` in `.env` (`COMPOSE_PROFILES=`), then:

```
docker compose --profile mail rm -sf mailserver webmail
docker compose up -d
```

The mail stays in its Docker volumes (`mail_data`, `mail_config`). Also remove
the MX record, so that other mail servers stop trying to deliver to you, and
empty `SMTP_HOST` in `.env` (the site then sends no email, and bid alerts stay
off).

## What is where

| | |
|---|---|
| `docker-compose.yml` | The `mailserver` and `webmail` services and their settings. |
| `caddy/Caddyfile` | The webmail's address. Caddy's certificate for it is the mail server's too. |
| `mail/fail2ban-jail.cf` | Fail2Ban's rules (the webmail and the site are never blocked). |
| `mail/roundcube.php` | The webmail's settings. |
| `scripts/mail.sh` | The commands on this page. |
| Docker volumes | `mail_data` (the mailboxes), `mail_config` (mailboxes, passwords, DKIM key), `mail_state` (spam filter data, mail waiting to go out), `mail_logs`, `webmail_db` (webmail settings and contacts). |
