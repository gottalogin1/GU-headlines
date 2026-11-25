# Newsletter feed

GU Headlines makes a feed of its top stories for an email newsletter. An email
service such as [SendFox](https://sendfox.com) reads the feed and makes an
email from the stories for your subscribers. The feed is at your domain
followed by `/feed.xml`, for example `https://guamheadlines.com/feed.xml`.

- [How the stories are picked](#how-the-stories-are-picked)
- [Settings](#settings)
- [Connecting SendFox](#connecting-sendfox)
- [Checking the feed](#checking-the-feed)

## How the stories are picked

On each edition day, at the edition time (Guam time), the worker makes an
**edition**: the top stories that it found since the edition before.

1. It takes the stories that it first found between the two edition times. A
   story is in one edition only. An old story that the worker found late (for
   example from a site's archive) is left out. Obituaries and the stories of
   sources that belong only on one topic page are left out too, as on the
   home page.
2. The stories that readers clicked most come first. This needs the
   [visitor stats](settings.md#visitor-stats) (`STATS_PASSWORD`). Then come
   the stories with a photo, then the newest. Without the visitor stats, the
   newest stories with a photo come first.
3. It takes at most 3 stories from one news site, so one busy newsroom cannot
   fill the edition, and stops at `NEWSLETTER_STORIES` stories.

The feed then shows the same edition until the next one. So the email
service sees new stories only just after an edition is made, never in
between. Each edition is kept in the database (the `newsletter_editions`
table).

Each story in the feed has:

- the headline,
- the photo (the copy stored on your site, or the news site's photo),
- the news site's name,
- the first lines of the intro (at most 280 characters, never the full story),
- a link to the story. While the visitor stats are on, the link passes
  through `/go/<number>` on your site, so the clicks in the emails count on
  the stats page like other story clicks.

No page of the site links to the feed. The feed tells search engines not to
list it (the `X-Robots-Tag: noindex` header), and it is not in the sitemap.
Robots are not turned away (`robots.txt` does not block it), because the
email service reads the feed as a robot.

When the worker starts, it makes the newest edition that is due, if it is not
made yet. So after an update, the feed has stories at once.

## Settings

| Setting | Default | What it does |
|---|---|---|
| `NEWSLETTER_DAYS` | `tue,fri` | The days of the week when the worker makes an edition. Day names in English, or their first three letters, with commas. Empty: no feed (`/feed.xml` shows "Not found"). |
| `NEWSLETTER_TIME` | `10:00` | The time of the edition, in Guam time, on a 24-hour clock. Make it about two hours before the email goes out. |
| `NEWSLETTER_STORIES` | `12` | The number of stories in each edition (1 to 50). |

To change a setting, edit `.env` and run `docker compose up -d` (see
[How to change a setting](settings.md#how-to-change-a-setting-in-env)).

## Connecting SendFox

The defaults make an edition every Tuesday and Friday at 10:00 AM, for an
email at noon on those days. SendFox reads the feed about every 30 minutes,
and makes an email from the new stories.

SendFox changes the names of its menus now and then. If a name below is
different in your account, look for the nearest one.

1. **Check that the feed works.** Open your feed address in a browser, for
   example `https://guamheadlines.com/feed.xml`. You see a page of text with
   the stories (each between `<item>` and `</item>`). If you see "Not found",
   `NEWSLETTER_DAYS` is empty in `.env`.
2. **Set the time zone of your SendFox account to Guam.** In SendFox, open
   your account settings (your name or picture at the top right) and find the
   time zone. Choose Guam (it can show as "Pacific/Guam" or "GMT+10:00
   Guam"). If Guam is not in the list, choose another zone with GMT+10:00
   (such as Port Moresby or Brisbane). Guam does not change its clocks in
   summer, and these zones do not either.
3. **Connect the feed.** Open
   [Web Presence](https://sendfox.com/account/web-presence) (in your account
   settings). In the RSS feed field, paste your feed address, for example
   `https://guamheadlines.com/feed.xml`, and save.
4. **Turn on the smart campaign from the feed.** Open **Emails**, then the
   **Smart Campaigns** settings. Turn on the campaign that uses your RSS feed
   (SendFox calls it "Content Updates" or "RSS"). Choose the list of
   subscribers that gets the email.
5. **Send it at noon on Tuesday and Friday.** In the same settings, turn on
   automatic sending (auto-schedule), choose **Tuesday** and **Friday**, and
   the time **12:00 PM**.
   - If SendFox lets you choose only one day, choose Tuesday at 12:00 PM
     there. On Fridays, open the draft that SendFox made from the new stories
     (in **Emails**, after 10:30 AM) and schedule it for 12:00 PM yourself.
   - If you keep the account in another time zone, 12:00 PM in Guam is 2:00
     AM UTC on the same day, and 6:00 PM Pacific time (US west coast) the day
     before.
6. **Look at the first email.** SendFox makes a draft when it sees new
   stories in the feed. Right after you connect the feed, it can make one from
   the edition that the feed shows at that time. Open the draft, and send a
   test email to yourself. The
   headlines, photos and intros come from the feed. You can change the
   layout of the email in SendFox's editor.

Photos: the photos stored on your site are WebP pictures. Gmail, Apple Mail,
Yahoo and Outlook on the web and on phones show them. The Outlook program on
Windows does not show WebP pictures. It shows the headline and the text
without the photo.

## Checking the feed

- The worker's log tells when it makes an edition, and how many stories it
  has:

  ```
  docker compose logs worker | grep newsletter
  ```

  It shows lines such as `newsletter edition of Tue Nov 24 10:00 made: 12
  stories`.
- At the top of the feed, `<pubDate>` is the time of the edition that the
  feed shows now.
- An edition with no stories (no new stories were found, for example because
  the worker was stopped) makes a feed with no stories. The email service
  then finds no new stories to send.
