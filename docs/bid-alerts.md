# Bid alerts

Bid alerts are a paid service on the site. A business pays a monthly price
through PayPal, and gets an email when a new bid on the Bids page matches its
trade or its own words. The page for it is `/bids/alerts`, for example
`https://guamheadlines.com/bids/alerts`.

This guide tells you how to turn bid alerts on, how to test them, and what
happens after a business subscribes.

## What a subscriber sees

1. On the bid alerts page, the business types its email address, picks one or
   more trades (such as "Construction and repairs" or "Medical and health"),
   and can type its own words (such as `roofing, generator`). No trade and no
   word means every new bid.
2. The next page shows the details and the price. "Pay with PayPal" opens
   PayPal's own page, where the business signs in to PayPal or pays with a
   card, and approves the monthly payment.
3. PayPal sends the business back to its own page on the site. The page says
   that the payment is done, and a welcome email comes. The welcome email
   lists the open bids that match now, and has the link to the business's
   own page.
4. After each bids check (every `BIDS_INTERVAL_HOURS`, 6 by default), the
   business gets one email with the new bids that match, if there are any.
   Each bid shows its kind and number, the agency, the due date, which trade
   or word it matched, and a link to the notice.

There is no login or password. Every email has the link to the business's own
page, where it can change its email address, trades and words, or cancel. A
business that lost the link types its address in "Lost the link to your page?"
on the bid alerts page, and the link is sent to that address again.

## What you need

- PayPal, set up as in the PayPal part of `.env.example`.
- A mail server, to send the emails (see step 1).
- The site's address: `SITE_DOMAIN` (you have it set already for https) or
  `SITE_URL`.
- A price in `BID_ALERTS_PRICE`.

Until all four are set, there is no bid alerts page, and nothing links to it.
This command says what is still missing:

```
docker compose exec web guheadlines bid-alerts
```

## 1. Set up sending email

The site hands each email to a mail server through SMTP (the standard way that
programs send email), and the mail server delivers it. SendFox cannot do this:
it sends one newsletter to a whole list, not one email to one person.

The mail server can run on your own server, or belong to an email company. Any
mail server that takes SMTP works. Its own setup steps give you the values
below, and the DNS records (often named SPF, DKIM and DMARC) that prove the
emails come from your domain and keep them out of spam folders.

How many emails: each subscriber gets at most one alert email per bids check
(4 a day with the default `BIDS_INTERVAL_HOURS=6`), plus a welcome email and,
when asked for, the link to their page.

The steps (they are in `.env.example` too):

1. Put the mail server's values in `.env`, for example:

   ```
   SMTP_HOST=mail.guamheadlines.com
   SMTP_PORT=587
   SMTP_USER=alerts
   SMTP_PASSWORD=the_password
   MAIL_FROM=GU Headlines <alerts@guamheadlines.com>
   ```

2. Run `docker compose up -d`, then send yourself a test email:

   ```
   docker compose exec web guheadlines mail-test you@example.com
   ```

If the test waits a long time and then fails, the site cannot reach the mail
server: check `SMTP_HOST` and `SMTP_PORT`. The site always encrypts the
connection (STARTTLS on port 587, or port 465), so the mail server needs a
certificate for the name in `SMTP_HOST`.

The sender address shows only in the emails, never on the site. It does not
need a mailbox: the emails tell subscribers to use the link to their own page.

## 2. Turn bid alerts on and test them

1. Keep `PAYPAL_MODE=sandbox` for now (test payments with pretend money).
2. Put a price in `.env`, for example `BID_ALERTS_PRICE=15`, and run:

   ```
   docker compose up -d
   docker compose exec web guheadlines bid-alerts
   ```

   It says "Bid alerts are on" and shows the address of the sign-up page.
3. Open the sign-up page. Sign up with your own email address and one trade.
4. On PayPal's test page, sign in with PayPal's pretend buyer (on
   developer.paypal.com: **Testing Tools**, then **Sandbox Accounts**, the
   account of type "Personal"). Approve the payment.
5. You come back to "Your bid alerts", which says the payment is done. The
   welcome email comes within a minute (look in the spam folder too).
6. Run `docker compose exec web guheadlines bid-alerts` again: your test
   subscriber shows as `active`.
7. On your page, click **Cancel my subscription**. The page says it is
   cancelled, and the alerts go on until the end of the paid month.

The first sign-up also makes the PayPal "plan" (the monthly price) by itself.
You can see it on sandbox.paypal.com with the pretend business account, under
subscriptions.

## 3. Take real payments

Change PayPal to live as in step 9 of the PayPal part of `.env.example`
(a Live app, its Client ID and Secret, and `PAYPAL_MODE=live`), then run
`docker compose up -d`. The site makes a new plan in live PayPal by itself the
first time that someone signs up. Test subscribers from the sandbox stay in
the list until they end; they get no more emails once their pretend paid time
ends.

## What happens after a business subscribes

| When | What happens |
|---|---|
| The business pays | PayPal sends it back to its page, which asks PayPal at once. If the business closes the window before that, the worker asks PayPal within the hour and starts the alerts anyway. |
| Each bids check | One email per subscriber with the new matching bids. A bid that a second source lists later is not sent again. When an email fails, its bids are tried again with the next check. |
| Twice a day | The worker asks PayPal about each subscription. A renewal moves the paid time on. |
| A payment fails | PayPal tries again. The alerts go on for 3 days past the payment date. After two failures in a row, PayPal suspends the subscription and the alerts pause. When the business fixes its payment in PayPal, the alerts start again. |
| The business cancels | On its own page, or in its PayPal account (automatic payments). PayPal takes no more payments. The alerts go on until the end of the time it paid for. |
| 30 days after a cancelled subscription ends | The address and the choices are deleted from the site. |
| A sign-up is never paid | It is deleted after 3 days. |

With `DISCORD_WEBHOOK_URL` set, your Discord channel gets a short note for a
new subscriber, a failed payment and a cancel. The notes do not show email
addresses.

## Commands

```
docker compose exec web guheadlines bid-alerts           # on or off, and the subscribers
docker compose exec web guheadlines bid-alerts --check   # ask PayPal about them now
docker compose exec web guheadlines bid-alerts --send    # send alerts for new bids now
docker compose exec web guheadlines mail-test you@example.com
docker compose exec web guheadlines paypal-check
```

## Changing the price

Change `BID_ALERTS_PRICE` and run `docker compose up -d`. New subscribers pay
the new price. Subscribers who joined before keep their old price, because
PayPal keeps each subscription on the plan it started with.

## Refunds and questions from subscribers

Payments are in your PayPal account. To give money back, open the payment in
PayPal and click **Refund**. A subscriber can also see and cancel the
subscription in their own PayPal account.

PayPal's own receipts show the name and the customer service email of your
PayPal business account (PayPal does this, not the site). You can change that
email in PayPal, under your business settings.

## Trades

The trades and their words are in `TRADES` in `guheadlines/bids/alerts.py`. A
word also finds itself with "s" or "es" at the end, and a word that ends in `*`
finds every word that starts with it (`roof*` finds "roofing" and "roofs"). A
bid matches when its title, number, kind, agency or description has one of the
words.

## Privacy

The site keeps each subscriber's email address, trades and words in its own
database, and uses them only for bid alerts. PayPal handles the payments, so
the site never sees a card or a PayPal password. The Sources page says this in
its disclaimer while bid alerts are on.
