# Events

The **Events** page lists what's coming up on Guam over the next 90 days,
collected from public calendars. At the top is a month calendar with the main
events, colored by the calendar they come from. Below it, every event is
listed, grouped by day (Today, Tomorrow, then each date), and each one shows,
when its calendar gives it:

- the event's name (linked to [its own page](#each-events-page) on this
  site) and who holds it,
- when it is, in Guam time, and where,
- the start of its description,
- a link to the event on the calendar that published it,
- for Guam Legislature sessions, a link to the session agenda (PDF).

Events leave the page once they are over, or when their calendar stops
listing them. Events whose title says they were cancelled or postponed are
left out. You can search the events by word and narrow them to one calendar.
Visitors can [add the events to their own calendar app](#adding-the-events-to-a-calendar-app),
on their phone or computer. Some calendars' events also show up
[in the home page's news feed](#in-the-home-page-news-feed).

**Contents**

- [The month calendar](#the-month-calendar)
- [Each event's page](#each-events-page)
- [Adding the events to a calendar app](#adding-the-events-to-a-calendar-app)
- [In the home page's news feed](#in-the-home-page-news-feed)
- [The calendars](#the-calendars)
- [How often they are read](#how-often-they-are-read)
- [Google Calendar key](#google-calendar-key)
- [When a calendar can't be read](#when-a-calendar-cant-be-read)
- [Adding or changing a calendar](#adding-or-changing-a-calendar)

---

## The month calendar

The calendar at the top of the page shows one month, Sunday to Saturday,
starting with this one. The **‹** and **›** buttons step through the months
events are collected for (the next 90 days). Each day shows its main events:

- at most 3 a day, all-day events first, then by start time, with "+2 more"
  (and so on) for the rest;
- an event that lasts more than a week, such as an exhibition or a
  registration period, on its first day only, so it doesn't fill every
  square;
- nothing on days that are over.

Click a day to see everything on it in the list below, including the events
that aren't main events. "All upcoming events" goes back to the full list.

On a phone the squares are too small for names, so each event is a small
colored square instead; tap the day to read them.

**Main events.** Each calendar's `main_events` option in `config/events.yaml`
says which of its events go on the month calendar: `all` (the default),
`none`, or a list of patterns (only events whose title matches one). Out of
the box, every calendar's events are main events except the GovGuam public
meetings, which are routine board meetings. When a visitor narrows the page to
one calendar or searches for a word, the month calendar shows every matching
event instead.

**Colors.** Each calendar's `color` option gives its events a color on the
month calendar and a matching mark in the list: green for the University of
Guam, red for the Guam Legislature, blue for Guam Community College, purple
for Life in the Son Christian Fellowship. The others are gray. The colors
are muted shades that suit both the light and the dark look; the legend
under the calendar names them.

## Each event's page

Every upcoming event has a page of its own on the site, at
`/events/<number>`: its name, who holds it, when and where it is, its whole
description, the links to its calendar and agenda, and two buttons that add
that one event to a calendar app:

- **Google Calendar** opens Google Calendar with the event filled in, ready
  to save.
- **Apple Calendar, Outlook or another app** gives a calendar file
  (`/events/<number>.ics`). A phone or computer opens it in its calendar
  app, ready to save.

The page goes away (it answers "not found") once the event is over, or when
its calendar stops listing it.

**In Google's event results.** Google lists events in its search results
(the "Events" box) only when each event has a page of its own that describes
it in a form Google reads (Event structured data). Each event page has that
description, and the [sitemap](maintenance.md#search-engines) lists every
upcoming event's page, so Google finds them. Google only lists events held at
a place, so an event whose calendar gives no place, or only an online one
(such as Zoom), gets a page but no description for Google. Google decides by
itself which events it shows, and it can take a few weeks after the pages
are found.

Google also asks for an end time, a description, a picture, an organizer, a
performer and a price ("offers") with each event. Search Console warns
("Missing field") about each one it doesn't get, but it still lists the
event. The site fills in each one only with what it knows for sure:

- **End time:** the calendar's. When a calendar gives only a start time,
  the end is given as the start time, rather than a guessed length.
- **Description:** the calendar's. When it has none, a sentence made of the
  event's name, time, place and organizer, all of which the page shows.
- **Picture:** a card the site draws for each event (`/events/<number>.png`,
  1920 × 1080): the event's name, when and where it is and who holds it, with
  the calendar's color down its side, since calendars give no photos.
- **Organizer and performer:** who holds the event (the calendar's
  `organizer`, or the calendar itself). No calendar names performers, so the
  performer is the group that holds the event.
- **Price:** only for an event known to be free: its calendar has
  `free_admission: true` (the Legislature and GovGuam public meetings), or
  its name or description says so ("free and open to the public", "admission
  is free") without giving a price. Its page then also says
  **Admission: Free**. Every other event's page says **Admission: Unknown**,
  and Google gets no price for it: Google's price field takes only a number,
  so "unknown" can't go in it, and a made-up price would be wrong. Search
  Console keeps warning "Missing field offers" for these events; that
  warning doesn't stop Google from listing them.

To see what Google reads on one event's page, paste its address into
Google's [Rich Results Test](https://search.google.com/test/rich-results).

## Adding the events to a calendar app

Under the month calendar, **Get these events in your calendar** lets a
visitor subscribe to the events in their calendar app. The app then checks
for changes by itself, so new, moved and cancelled events show up without
anything to do:

- **Google Calendar** opens Google Calendar, which asks to add the calendar.
  On a phone, it may be easier on a computer; the calendar then shows on the
  phone too (in the Google Calendar app, and on an iPhone that has the Google
  account).
- **Apple Calendar or Outlook** opens the calendar app on an iPhone, iPad or
  Mac (and Outlook on Windows), which asks to subscribe.
- Any other app: subscribe to the address shown under the buttons
  (`https://your-site/events.ics`). On many phones, one tap selects it for
  copying.

What the calendar holds:

- Without a calendar picked on the page: the **main events** of every
  calendar, the same as the month calendar (see [Main events](#the-month-calendar)).
- With a calendar picked in **Calendar** (`/events?calendar=legislature`),
  the buttons subscribe to **every** event of that one calendar
  (`/events.ics?calendar=legislature`).
- The next 90 days, and the past 30 days, so the app keeps what happened
  recently. Each phone shows the times in its own time zone (Guam time on a
  phone on Guam).

Calendar apps check the address on their own schedule, from every few hours
to about once a day (Google Calendar). The feed asks for a check every
`EVENTS_INTERVAL_HOURS` hours, and some apps follow that. The address is not
in the sitemap, and search engines are asked not to list it.

## In the home page's news feed

A calendar with `news_feed: true` in `config/events.yaml` also puts its
events in the news feed on the home page, among the stories, out of the box
only Life in the Son Christian Fellowship's. Each event appears there:

- from 6 in the morning (Guam time) the day before it, or from when it was
  first found if that's later, and it takes its place in the list at that
  time, like a story published then;
- until its day is over;
- as an item marked **Event · Tomorrow** (or Today, or On now) with the
  calendar's color, its name (linked to [its own page](#each-events-page),
  where visitors can add it to their calendar), when and where it is, and
  the start of its description.

Only main events (see `main_events` above) go in. When a news story is about
the event (the church's own announcement, say, collected from its blog), the
story stays and gets a line, "On our events calendar", linking to the event's
page, so the event isn't listed twice. Topic, site and search pages show
stories only.

## The calendars

They are listed in `config/events.yaml`. Out of the box:

| Calendar | What it gives |
|---|---|
| Guam Legislature | Sessions, public hearings and committee meetings from the Legislature's Google Calendar (the one on guamlegislature.gov/calendar), with each session's agenda from the Agendas page. Needs a free Google key (see [Google Calendar key](#google-calendar-key)); without it, only the sessions on the Agendas page are listed. |
| GovGuam public meetings | Board, commission and agency meetings from the government's official notices site, notices.guam.gov (the newest few pages of meeting notices). |
| Guam Community College | Events from the college's events list. |
| Life in the Son Christian Fellowship | Events of the church in Tamuning (lifeinthesonguam.org/events), from the Google Calendar on its Events page, plus the events announced in the posts on its blog that give a date. Needs the same free Google key as the Legislature for the calendar; without it, only the blog posts are read. A post about an event that is on the calendar on the same day is merged into it, and the event links to the post. Also in the [home page's news feed](#in-the-home-page-news-feed), and its blog posts are also collected as news. |
| University of Guam | Events, academic dates and holidays from the university's events calendar (uog.edu/calendar), read from its RSS feed (the "Add to RSS" link). The calendar's "Outlook/iCAL" link gives the same events, but it also holds every past event since 2023 (2.5 MB, against 100 KB for the feed), so the feed is read instead. |

Some things are left out by default:

- **Certificate presentations.** The Legislature's calendar lists every
  presentation of a legislative certificate to a person or group, which
  would crowd out sessions and hearings. Delete its `exclude` lines in
  `config/events.yaml` to show them.
- **The Legislature's own notices** on notices.guam.gov, because its calendar
  already lists those hearings.
- **Chamber of Commerce events.** The `exclude` list at the top of
  `config/events.yaml` leaves out, from every calendar, events whose title,
  organizer, place or description matches one of its patterns.

Also looked at but not added: the Guam Daily Post's calendar (empty), the
Guam Department of Labor (no upcoming events, and its calendar export is
turned off), USO Guam (only a monthly programs flyer), Pacific Historic Parks
(no Guam events), and the Guam Visitors Bureau and Guampedia fiesta calendars
(their firewalls turn away automated visitors). Calendars that can't be
collected are not shown on the site.

## How often they are read

The worker reads every calendar when it starts and then every
`EVENTS_INTERVAL_HOURS` hours (default 3), right after a news check. The
Legislature's website asks for 20 seconds between requests, so its part takes
a little longer. To read them now:

```
docker compose exec worker guheadlines events
```

`--source SLUG` reads one calendar. `guheadlines events --check` shows what
each calendar gives without saving anything (`--limit N` events per
calendar).

## Google Calendar key

The calendar on guamlegislature.gov is a Google Calendar, and so is the one
on Life in the Son Christian Fellowship's Events page. Google's own
calendar file for it is too large to download, and Google asks robots not to
read it, so GU Headlines reads it through Google's Calendar API instead. That
needs a free key from Google; there is no charge for this use, and Google
does not ask for a credit card. The steps, one by one, are in `.env.example`
under "Google Calendar key". In short:

1. Sign in at <https://console.cloud.google.com/> and make a project.
2. Turn on the
   [Google Calendar API](https://console.cloud.google.com/apis/library/calendar-json.googleapis.com)
   for it.
3. Under [Credentials](https://console.cloud.google.com/apis/credentials),
   click **Create credentials → API key**, then restrict the key to the
   Google Calendar API.
4. Add it to `.env`:
   ```
   GOOGLE_CALENDAR_API_KEY=your-key
   ```
5. `docker compose up -d` to apply, then check it with
   `docker compose exec worker guheadlines events --check --source legislature`.

The key is sent only to Google, in a request header, so it never shows up in
a web address or a log. The live check on GitHub (see
[How it works](how-it-works.md#github-checks)) reads it from a repository
secret named `GOOGLE_CALENDAR_API_KEY` (Settings → Secrets and variables →
Actions).

Until the key is added, the Legislature's and the church's entries in the
Events page sidebar show that they need a key; only the sessions from the
Legislature's Agendas page and the church's dated blog posts are listed.

## When a calendar can't be read

A calendar that is blocked, has no events where they used to be, or answers
with an error is never reported as fine. Its last good events stay on the
page, and its entry in the Events page sidebar shows a ⚠ with the reason. It
is tried again at the next check. When a calendar has more than one input
(the Legislature's calendar and its Agendas page) and only one fails, the
events from the other are still updated.

## Adding or changing a calendar

Each calendar in `config/events.yaml` is read from one or more inputs.
Changes take effect at the next check; no rebuild needed.

| Input | For |
|---|---|
| `google_calendar` | A public Google Calendar's ID (it looks like an email address). Needs `GOOGLE_CALENDAR_API_KEY`. |
| `ical` | An iCal (`.ics`) address, the format calendar apps share calendars in ("Subscribe", "Export" or "iCal feed" links). Repeating events are expanded. |
| `moderncampus` | The RSS feed of a calendar built with Modern Campus (its "Add to RSS" link), like the University of Guam's. |
| `posts` | The RSS feed of a blog or news page whose posts announce events, like Life in the Son's. Posts whose title or text gives a date (and a time, if it says one) become events on that date; the rest are skipped. A date without a year, like "Saturday, December 12th", is taken as the next one after the post was written. |
| `agendas` | A page that lists meeting agendas (PDFs) under dated headings, like the Legislature's Agendas page. |
| `notices` | A notices.guam.gov listing. |
| `pages` | A web page that lists events. Needs CSS selectors: `item` (each event), `title` (its name and link), `when` (its date and time) and optionally `description`. |

Other options per calendar: `homepage` (linked from the Events page),
`location` and `organizer` (used when an event doesn't say), `exclude` (leave
out events whose title or organizer matches one of these patterns),
`main_events` and `color` (`green`, `red`, `blue` or `purple`; see
[The month calendar](#the-month-calendar)), `news_feed: true` (see
[In the home page's news feed](#in-the-home-page-news-feed)),
`free_admission: true` when every event on it is free to attend (see
[Each event's page](#each-events-page)), `request_delay`, `ignore_robots`, `enabled: false` to pause a calendar, and
`cannot_scrape: <reason>` to keep a calendar in the file without reading it
(it isn't shown on the site). Try a calendar first, without saving anything,
with:

```
docker compose exec worker guheadlines events --check --source uog
```

Before adding a site, read its terms of use, and leave it out if they forbid
collecting its listings.
