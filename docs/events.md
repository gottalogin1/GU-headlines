# Events

The **Events** page lists what's coming up on Guam over the next 90 days,
collected from public calendars. At the top is a month calendar with the main
events, colored by the calendar they come from. Below it, every event is
listed, grouped by day (Today, Tomorrow, then each date), and each one shows,
when its calendar gives it:

- the event's name (linked to its page on the calendar) and who holds it,
- when it is, in Guam time, and where,
- the start of its description,
- for Guam Legislature sessions, a link to the session agenda (PDF).

Events leave the page once they are over, or when their calendar stops
listing them. Events whose title says they were cancelled or postponed are
left out. You can search the events by word and narrow them to one calendar.

**Contents**

- [The month calendar](#the-month-calendar)
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
Guam, red for the Guam Legislature, blue for Guam Community College. The
others are gray. The colors are muted shades that suit both the light and the
dark look; the legend under the calendar names them.

## The calendars

They are listed in `config/events.yaml`. Out of the box:

| Calendar | What it gives |
|---|---|
| Guam Legislature | Sessions, public hearings and committee meetings from the Legislature's Google Calendar (the one on guamlegislature.gov/calendar), with each session's agenda from the Agendas page. Needs a free Google key (see [Google Calendar key](#google-calendar-key)); without it, only the sessions on the Agendas page are listed. |
| GovGuam public meetings | Board, commission and agency meetings from the government's official notices site, notices.guam.gov (the newest few pages of meeting notices). |
| Guam Community College | Events from the college's events list. |
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

The calendar on guamlegislature.gov is a Google Calendar. Google's own
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

Until the key is added, the Legislature's entry in the Events page sidebar
shows that it needs a key, and only the sessions from its Agendas page are
listed.

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
| `agendas` | A page that lists meeting agendas (PDFs) under dated headings, like the Legislature's Agendas page. |
| `notices` | A notices.guam.gov listing. |
| `pages` | A web page that lists events. Needs CSS selectors: `item` (each event), `title` (its name and link), `when` (its date and time) and optionally `description`. |

Other options per calendar: `homepage` (linked from the Events page),
`location` and `organizer` (used when an event doesn't say), `exclude` (leave
out events whose title or organizer matches one of these patterns),
`main_events` and `color` (see [The month calendar](#the-month-calendar)),
`request_delay`, `ignore_robots`, `enabled: false` to pause a calendar, and
`cannot_scrape: <reason>` to keep a calendar in the file without reading it
(it isn't shown on the site). Try a calendar first, without saving anything,
with:

```
docker compose exec worker guheadlines events --check --source uog
```

Before adding a site, read its terms of use, and leave it out if they forbid
collecting its listings.
