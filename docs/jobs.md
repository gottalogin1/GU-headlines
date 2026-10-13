# Jobs

The **Jobs** page lists openings on Guam collected from job boards. Each
posting shows, when the board gives it:

- the job title (linked to the posting on its board) and who is hiring,
- the pay, the location, the open date and the closing date,
- the start of the description,
- a link to the job announcement PDF,
- **Scraped**: the last time the posting was found on its board. When a
  posting doesn't say when it opened or closes, this still tells you how
  fresh it is. "First seen" appears when that was on an earlier day.

Anything a posting doesn't say is shown as a dash, never guessed. Postings
leave the page when their board stops listing them or their closing date has
passed. You can search the postings by word and narrow them to one board.

**Contents**

- [The job boards](#the-job-boards)
- [How often they are read](#how-often-they-are-read)
- [USAJOBS](#usajobs)
- [When a board can't be read](#when-a-board-cant-be-read)
- [Adding or changing a board](#adding-or-changing-a-board)

---

## The job boards

They are listed in `config/jobs.yaml`. Out of the box:

| Board | What it gives |
|---|---|
| GuamJobsOnline | Title, employer, location, pay, posting date, description |
| Guam Department of Education | Title and announcement PDF (dates and pay are inside the PDFs) |
| Federal jobs (federalgovernmentjobs.us) | Title, agency, location, open and close dates; pay and summary from each posting's page |
| USAJOBS | Everything, once you add a free key (see [USAJOBS](#usajobs)) |
| University of Guam | Title and announcement PDF from the HR page |
| Guam Community College | Title, open date, closing date or "open until filled", announcement PDF |
| Guam Community College partners | The same, for partner employers (DOCOMO PACIFIC, MedPharm and others) |
| GuamTemps | Title, pay, posting date, description |

**Listed but not collected.** These turn away automated visitors, so the Jobs
page lists them with a link and the reason instead:

- **GovGuam Jobs** (jobs.guam.gov). The site is built with JavaScript, and
  the service behind it answers automated requests with a server error.
- **American Job Center** (Guam Department of Labor). Its page lists no
  openings; its job bank, HireGuam, is behind a firewall that blocks
  automated visitors.

The University of Guam also posts openings on its PeopleAdmin portal
(uog.peopleadmin.com), which would add dates and pay, but that portal's
`robots.txt` asks robots other than a few search engines to stay out, so only
the HR page is read.

## How often they are read

The worker reads every board when it starts and then every
`JOBS_INTERVAL_HOURS` hours (default 6), right after a news check. A
posting's own page is read once, when the posting first appears; after that
only the listing is checked. To read them now:

```
docker compose exec worker guheadlines jobs
```

`--source SLUG` reads one board. `guheadlines jobs --check` shows what each
board gives without saving anything (`--limit N` postings per board,
`--details N` of them with their own page read).

## USAJOBS

The USAJOBS website needs JavaScript, so GU Headlines reads its official
search API instead, which needs a free key:

1. Request one at <https://developer.usajobs.gov/apirequest/>.
2. Add both lines to `.env`, with the email address the key was sent to:
   ```
   USAJOBS_API_KEY=your-key
   USAJOBS_EMAIL=you@example.com
   ```
3. `docker compose up -d` to apply.

Until then the Jobs page shows that the board needs a key. Many of the same
federal openings come in through federalgovernmentjobs.us anyway.

## When a board can't be read

A board that is blocked, needs JavaScript, has no postings where they used
to be, or answers with an error is never reported as fine. Its last good
postings stay on the page, and the board's entry in the Jobs page sidebar
shows a ⚠ with the reason. It is tried again at the next check.

## Adding or changing a board

Each board in `config/jobs.yaml` names a `parser`, the kind of page it is.
Changes take effect at the next check; no rebuild needed.

| Parser | For |
|---|---|
| `hafajobs` | apply.hafajobs.com boards |
| `pdf_links` | A page that links each opening's announcement PDF |
| `uog` | The University of Guam (its portal and HR page) |
| `peopleadmin` | Any `*.peopleadmin.com` portal that allows robots |
| `federalgovernmentjobs` | federalgovernmentjobs.us location pages |
| `usajobs` | The USAJOBS API |
| `guamjobsonline` | guamjobsonline.com search results |
| `govguam` | jobs.guam.gov's job list service |
| `sections` | A page with one heading per opening and its description below |

Other options per board: `pages` (the listing pages; "next page" links are
followed), `employer` and `location` (used when a posting doesn't say),
`request_delay`, `ignore_robots`, `enabled: false` to pause a board, and
`cannot_scrape: <reason>` to list a board without reading it. Delete the
`cannot_scrape` line to try a board again; check it first with:

```
docker compose exec worker guheadlines jobs --check --source govguam
```
