# Events

The **Events** page lists what's coming up on Guam over the next 90 days,
collected from public calendars. Events are grouped by day (Today, Tomorrow,
then each date), and each one shows, when its calendar gives it:

- the event's name (linked to its page on the calendar) and who holds it,
- when it is, in Guam time, and where,
- the start of its description,
- for Guam Legislature sessions, a link to the session agenda (PDF).

Events leave the page once they are over, or when their calendar stops
listing them. Events whose title says they were cancelled or postponed are
left out. You can search the events by word and narrow them to one calendar.

**Contents**

- [The calendars](#the-calendars)
- [How often they are read](#how-often-they-are-read)
- [Google Calendar key](#google-calendar-key)
- [When a calendar can't be read](#when-a-calendar-cant-be-read)
- [Adding or changing a calendar](#adding-or-changing-a-calendar)

---

## The calendars

They are listed in `config/events.yaml`. Out of the box:

| Calendar | What it gives |
|---|---|
| Guam Legislature | Sessions, public hearings and committee meetings from the Legislature's Google Calendar (the one on guamlegislature.gov/calendar), with each session's agenda from the Agendas page. Needs a free Google key (see [Google Calendar key](#google-calendar-key)); without it, only the sessions on the Agendas page are listed. |
| GovGuam public meetings | Board, commission and agency meetings from the government's official notices site, notices.guam.gov (the newest few pages of meeting notices). |
| Guam Community College | Events from the college's events list. |

Two things are left out by default, and each can be switched back on by
deleting a few lines in `config/events.yaml`:

- **Certificate presentations.** The Legislature's calendar lists every
  presentation of a legislative certificate to a person or group, which
  would crowd out sessions and hearings.
- **The Legislature's own notices** on notices.guam.gov, because its calendar
  already lists those hearings.

**Listed but not collected.** These turn away automated visitors, so the
Events page lists them with a link and the reason instead:

- **Guam Visitors Bureau events** (visitguam.com). Its firewall (Akamai)
  blocks automated visitors on the events pages and the events feed.
- **Village fiesta calendar** (Guampedia). Its firewall (Cloudflare) blocks
  automated visitors.
- **Guam Chamber of Commerce events**. Its firewall (Cloudflare) blocks
  automated visitors.

Also looked at but not added: the University of Guam (no events calendar on
its site), the Guam Daily Post's calendar (empty), the Guam Department of
Labor (no upcoming events, and its calendar export is turned off), USO Guam
(only a monthly programs flyer) and Pacific Historic Parks (no Guam events).

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
| `agendas` | A page that lists meeting agendas (PDFs) under dated headings, like the Legislature's Agendas page. |
| `notices` | A notices.guam.gov listing. |
| `pages` | A web page that lists events. Needs CSS selectors: `item` (each event), `title` (its name and link), `when` (its date and time) and optionally `description`. |

Other options per calendar: `homepage` (linked from the Events page),
`location` and `organizer` (used when an event doesn't say), `exclude` (leave
out events whose title or organizer matches one of these patterns),
`request_delay`, `ignore_robots`, `enabled: false` to pause a calendar, and
`cannot_scrape: <reason>` to list a calendar without reading it. Delete the
`cannot_scrape` line to try a calendar again; check it first with:

```
docker compose exec worker guheadlines events --check --source gvb
```

Before adding a site, read its terms of use, and leave it out if they forbid
collecting its listings.
