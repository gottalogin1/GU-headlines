# Visitor guides

The visitor guides are pages about visiting Guam that you write yourself and
keep up to date: getting around, the weather, beaches, food, what to see.
Readers find them on **/guides** (for example
`https://guamheadlines.com/guides`), and the top bar gets a **Guides** link
once the first guide is published.

You write and change the guides in your browser, on a private page of your
site: **/admin/guides**. No file to edit, no command to run, no restart.

- [Before you start](#before-you-start)
- [Your first guides: the starter guides](#your-first-guides-the-starter-guides)
- [Writing a guide](#writing-a-guide)
- [Formatting the text](#formatting-the-text)
- [Photos](#photos)
- [Drafts, previews and publishing](#drafts-previews-and-publishing)
- [Ads in the guides](#ads-in-the-guides)
- [Selling a sponsorship of a guide](#selling-a-sponsorship-of-a-guide)
- [Selling guides (later)](#selling-guides)
- [Where the guides are kept](#where-the-guides-are-kept)

## Before you start

The editor uses the same password as the visitor stats page, `STATS_PASSWORD`
in `.env`. If you have not set it yet, follow the steps above `STATS_PASSWORD`
in `.env.example` (in short: make a password with `openssl rand -hex 16`, put
it after `STATS_PASSWORD=` in `.env`, and run `docker compose up -d`).

Then open your domain followed by `/admin/guides`. The browser asks for a user
name and a password: type any user name and the password. The `/stats` page
also has a **Visitor guides** link.

Use the editor over https (with `SITE_DOMAIN` set), so the password is
protected on its way.

## Your first guides: the starter guides

The site comes with eight starter guides, written for this site:

| Guide | Section |
|---|---|
| Your first day on Guam | Start here |
| Getting around Guam | Start here |
| Weather and the best time to visit | Plan your trip |
| Beaches and staying safe in the water | Things to do |
| A morning in Hagåtña | Things to do |
| Driving the southern villages | Things to do |
| Local food to try | Food and culture |
| Local customs and being a good guest | Food and culture |

On `/admin/guides`, press **Add the starter guides as drafts**. They are added
as drafts, which readers can't see. Open each one, read it, change anything
you know better (opening days, your favorite places, what changed recently),
and publish it. Add only the ones you want: delete the others.

The starter guides avoid prices, opening hours and other facts that change
often. Before you publish, check the places and facts you are not sure about.

## Writing a guide

Press **New guide** on `/admin/guides`. The form has:

- **Title**: the guide's name, such as *Getting around Guam*.
- **Address**: the end of the guide's web address, such as `getting-around`
  for `/guides/getting-around`. Leave it empty and the site makes one from
  the title. Once a guide is published, keep its address: links that people
  saved or shared, and search engines, use it.
- **Section**: the heading the guide is listed under on `/guides`, such as
  *Start here* or *Things to do*. Guides with the same section are listed
  together. The box suggests the sections you used before.
- **Order**: lower numbers are listed first. Use 10, 20, 30 and so on, so a
  new guide fits in between (25) without changing the others.
- **Who can see it**: *Draft* (only you) or *Published* (everyone).
- **Summary**: one or two sentences, shown under the title on `/guides` and in
  search results (Google shows about 150 characters).
- **Text**: the guide itself, in Markdown (next part).
- **Photos** and **Sponsor of this guide**: see below.

Press **Save**. Problems are shown beside the fields, with everything you
typed kept.

## Formatting the text

The text is written in **Markdown**: plain text with a few marks for headings,
lists and links. The editor has a short table under **How to format the
text**. The most useful marks:

```
An empty line between paragraphs starts a new paragraph.

## A main heading

Each main heading starts a new part of the guide. A guide with three or
more parts gets an "On this page" list of them at the top.

### A smaller heading

**strong words** and *slanted words*

- a list
- with dots

1. a numbered
2. list

[a page of this site](/weather)
[another website](https://www.nps.gov/wapa/)
[a phone number people can tap](tel:+16715550123)

> A "good to know" box: start the line with >.

| Month | Weather |
|---|---|
| January | Dry and breezy |
```

Good to know:

- Links to other websites open in a new tab, and search engines are told not
  to count them as recommendations (`nofollow`). That is also how Google asks
  for paid links, such as affiliate links, to be marked.
- HTML code typed into the text shows as text: it is never run.
- Keep sentences short and practical. Visitors read guides on their phones.

## Photos

Photos make a guide. Each guide can have:

- **A cover photo**: the large photo at the top of the guide and on its card
  on `/guides`. A wide (landscape) photo works best. Fill in **What the cover
  photo shows** too: it is read aloud to readers who can't see the photo, and
  search engines read it.
- **Photos in the text**: choose one under **Add a photo to the text**, say
  what it shows, and press **Save**. The photo is added at the end of the
  text as a line like this:

  ```
  ![Red rice and kelaguen](/media/guides/3-1a2b3c4d5e6f-1400x933.webp)
  ```

  Move that line to where the photo belongs (cut and paste it, with an empty
  line above and below). To put a caption under the photo, add it in quotes
  after the address:

  ```
  ![Red rice and kelaguen](/media/guides/3-1a2b3c4d5e6f-1400x933.webp "A fiesta plate")
  ```

  Add one photo per save.

The site makes its own copies of each photo, at most 1400 pixels wide, plus a
smaller one for phones, so large photos from a phone or camera are fine (up to
8 MB each, JPG, PNG or WebP). Only photos added in the editor can be shown in a
guide, never photos from other websites.

Use only photos that are yours, or that you have permission to use.

## Drafts, previews and publishing

- A **draft** is yours alone. **Preview** (at the top of the editor, and in the
  list) shows the guide exactly as readers will see it, with a note that it is
  a preview. Previews have no ads, are not counted on `/stats`, and are hidden
  from search engines.
- **Published** guides are on `/guides`, in the site's map for search engines
  (`/sitemap.xml`), and in the **Guides** link of the top bar.
- Changes to a published guide show on the site within a minute.
- To take a guide off the site for a while, make it a draft again. **Delete**
  (at the bottom of the editor) removes the guide and its photos for good.

The list on `/admin/guides` shows each guide's page views in the last 30
days, counted while the visitor stats are on.

## Ads in the guides

The guides earn from day one, with what the site already has:

- **AdSense**: with `ADSENSE_CLIENT` and `ADSENSE_SLOT` set, every guide page
  and `/guides` show the ad in the right-hand column. With
  `ADSENSE_STORIES_SLOT` set too, each guide with three or more parts shows a
  second ad in its text, after the first part. See
  [docs/settings.md](settings.md#advertising).
- **The sponsor spot** at the top of the right-hand column shows on the guides
  too, like on every page (and so does a business that booked it on the
  Advertise page).
- **Plan a trip**: the affiliate links box (`AFFILIATE_*_LINK`) shows beside
  every guide, where visitors are most likely to book.

## Selling a sponsorship of a guide

A business can sponsor one guide, for example a car rental company on
*Getting around Guam*, or a dive shop on *Beaches and staying safe in the
water*. The guide then shows **Sponsored by** with the business's name, a link
to its website and a short line, near the top.

1. Agree with the business on the guide, the price and the dates. They pay you
   through PayPal (for example with a PayPal invoice, or your PayPal.Me link).
2. Open the guide in the editor and fill in **Sponsor of this guide**: the
   name, the website, a short line about them, and the **Last day it shows**.
3. Press **Save**. The box shows at once and goes away by itself after its last
   day (Guam time).

While the visitor stats are on, clicks on the sponsor's link are counted. The
list on `/admin/guides` shows each sponsor's clicks in the last 30 days, and
each guide's page views: the numbers to show a business before and after it
sponsors a guide.

## Selling guides

<a id="selling-guides"></a>
Selling access to guides (for example a guide pass bought through PayPal, with
a private link instead of a login) is **not built yet**. The site is ready for
it: every guide already has a free/paid setting in the database (all guides
are free for now), and a guide's text is already split into parts at its main
headings, so a paid guide could show its opening part to everyone and the
rest to readers with a pass. When you want it, ask for it to be added; your
guides and their addresses stay as they are.

## Where the guides are kept

The guides are kept in the site's database, and their photos with the site's
other photos (in the `media` volume, folder `guides`). The nightly backup
keeps both (see [maintenance.md](maintenance.md#backups)). They are not in the
code: the code is public, so text you write, and may sell one day, stays
yours.
