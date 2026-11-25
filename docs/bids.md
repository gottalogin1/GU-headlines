# Bids and RFPs

The **Bids** page lists the Government of Guam's open bids and requests for
proposals: the public notices in which an agency asks businesses to offer a
price, or a proposal, for something it wants to buy. Two kinds are common:

- **Invitation for Bid (IFB)**: the agency says exactly what it wants (a
  cargo van, office chairs, a repaving job) and the lowest price from a
  qualified company wins.
- **Request for Proposals (RFP)**: the agency describes a need (an audit, a
  new records system) and picks the best proposal, not only the cheapest.

Each bid on the page shows, when its source gives it:

- its kind and number, such as "Invitation for bid · GSA-003-27",
- what it is for (linked to the bid's page at the agency or portal), and the
  agency asking,
- when offers are due, in Guam time, and how many days are left (**bold**
  when it's due within 3 days),
- the day it was posted and the start of its text,
- a link to the notice (PDF) when there is one.

Soonest due comes first. Bids whose due date is only in the agency's
documents say "See the notice" and come last. You can search the bids by word
and narrow them to one agency or one source. Public hearing notices are not
listed.

**Contents**

- [The sources](#the-sources)
- [Which bids are shown](#which-bids-are-shown)
- [How often they are read](#how-often-they-are-read)
- [When a source can't be read](#when-a-source-cant-be-read)
- [Adding or changing a source](#adding-or-changing-a-source)

---

## The sources

They are listed in `config/bids.yaml`. Out of the box:

| Source | What it gives |
|---|---|
| GovGuam public notices | The government's official notices site, notices.guam.gov, narrowed to its IFB, RFP and emergency procurement topics. Many agencies post their bids there: the General Services Agency (which buys for most of the government), the Airport, the Port, Guam Community College, the Department of Revenue and Taxation, the Office of Public Accountability and others. The newest 6 pages (60 notices) are read. |
| Guam Power Authority | The Power Authority's bid list on the OpenGov Guam portal, with exact due dates and times. |
| Guam Housing and Urban Renewal Authority | GHURA's bid list on the OpenGov Guam portal. |
| Guam Memorial Hospital Authority | The hospital's procurement page. |
| Guam Waterworks Authority | GWA's bids page. |
| Port Authority of Guam | The Port's bids and RFPs page. Its list has no due dates, so each open bid's own page is read for its "Close Date" (once; a date found is kept). |

None of these sites' robots.txt files or terms of use forbid reading their
lists. Only the facts of each bid are shown (its title, number, agency and
dates, and the start of its text), every bid links to the page it came from,
and the page says to check the details with the agency before bidding.

Also looked at but not added: the General Services Agency's own bid page
(its bids are on notices.guam.gov too, with due dates; its own page has only
PDF links), Guam Community College's bid page (PDF links only; the college
posts on notices.guam.gov too), the Guam Legislature's procurement page
(rarely used), and the Airport's and Invest Guam's websites (their firewalls
turn away automated visitors; the Airport posts its bids on notices.guam.gov).
No bid lists were found for Public Works, the Department of Education or the
University of Guam. Sources that can't be collected are not shown on the
site.

## Which bids are shown

- **Open ones only.** A bid leaves the page once its due date has passed
  (this is checked on every page view, not only when the lists are read), or
  when its source stops listing it or lists it as closed, cancelled or
  awarded. A notice whose title says it was cancelled, or that all bids were
  rejected, ends the bid too.
- **Bids without a due date** stay for 30 days after they were posted (the
  `undated_days` option of a source changes this). Several agencies give the
  due date only inside their PDF.
- **One card per bid.** The same bid is often posted on notices.guam.gov and
  on the agency's own list. It is shown once (the listing with the most
  exact due date, then one with a document), with "also on" and a link to the
  other listing. Two listings count as the same bid when their numbers match,
  however each writes it: "ITB GPA-001-27" and "GPA-001-27" are one bid. A
  later notice for a bid ("2nd Notice of ...", an amendment) replaces the
  earlier one.
- **Left out:** sole-source notices (an agency buying from one named
  company, so there is nothing to bid on), the Power Authority's standing
  "Continuous Formal Tenders" notice, hearing notices, and anything about the
  Chamber of Commerce (the `exclude` list at the top of `config/bids.yaml`).

## How often they are read

The worker reads every source when it starts and then every
`BIDS_INTERVAL_HOURS` hours (default 6), right after a news check. Agencies
post a few new bids a week, so this is plenty. To read them now:

```
docker compose exec worker guheadlines bids
```

`--source SLUG` reads one source. `guheadlines bids --check` shows what each
source gives without saving anything (`--limit N` bids per source).

## When a source can't be read

A source that is blocked, has no table of bids where it used to be, or
answers with an error is never reported as fine. Its last good bids stay on
the page (until they are due), and its entry in the Bids page sidebar shows a
⚠ with the reason. It is tried again at the next check.

## Adding or changing a source

Each source in `config/bids.yaml` is read from one or more inputs. Changes
take effect at the next check; no rebuild needed.

| Input | For |
|---|---|
| `notices` | A notices.guam.gov listing, narrowed to topics with `topic_id[]` in its address. |
| `table` | A web page with a table of bids, one row each. The columns are found by their headings: the number ("Number", "No.", "Announcement"), the title ("Title", "Description", "Project"), the issue date ("Issued", "Posted", "Available"), the due date ("Due", "Deadline", "Submission", "Close") and the status. A page that leaves out the table's row tags (like GWA's) works too. |

Other options per source: `homepage` (linked from the Bids page), `agency`
(the agency's full name, for a table, whose rows don't say it; it also keeps
two agencies' bid numbers such as "001-2027" apart), `details: true` (read the
page of each open bid without a due date for one), `undated_days`, `exclude`
(leave out bids whose title, number or agency matches one of these patterns),
`request_delay`, `ignore_robots`, `enabled: false` to pause a source, and
`cannot_scrape: <reason>` to keep a source in the file without reading it (it
isn't shown on the site). Try a source first, without saving anything, with:

```
docker compose exec worker guheadlines bids --check --source gwa
```

Before adding a site, read its terms of use, and leave it out if they forbid
collecting its listings.
