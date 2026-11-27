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
- [Selling guides: guide packs](#selling-guides)
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

The site comes with thirty starter guides, written for this site. The ten
marked *pack* are for paying readers and come with the starter guide packs
(see [Selling guides](#selling-guides)):

| Guide | Section |
|---|---|
| Your first day on Guam | Start here |
| Getting around Guam | Start here |
| Your first week on Guam, day by day (*pack*) | Your first week |
| Three or four days on Guam (*pack*) | Your first week |
| Guam on a budget (*pack*) | Your first week |
| Before you fly: papers, money and packing (*pack*) | Your first week |
| Weather and the best time to visit | Plan your trip |
| Guam with kids | Plan your trip |
| What to do on a rainy day | Plan your trip |
| Things to do on Guam (a list that links to all the others) | Things to do |
| Beaches and staying safe in the water | Things to do |
| Tumon and Two Lovers Point | Things to do |
| Snorkeling and diving | Things to do |
| Hiking and waterfalls | Things to do |
| Boats, dolphins and island trips | Things to do |
| A morning in Hagåtña | Things to do |
| World War II sites | Things to do |
| Latte stones and ancient CHamoru sites | Things to do |
| Driving the southern villages | Things to do |
| A day in northern Guam | Things to do |
| Markets and shopping | Things to do |
| Local food to try | Food and culture |
| Fiestas and festivals through the year | Food and culture |
| Local customs and being a good guest | Food and culture |
| PCS to Guam: the move checklist (*pack*) | Moving to Guam |
| Finding a home on Guam: on base and off (*pack*) | Moving to Guam |
| Bringing your pets to Guam (*pack*) | Moving to Guam |
| Cars and driving on Guam (*pack*) | Moving to Guam |
| Schools and child care for military families (*pack*) | Moving to Guam |
| Getting your home ready for typhoon season (*pack*) | Moving to Guam |

On `/admin/guides`, the **Starter guides** box lists the ones you don't have
yet, each with a tick. Untick any you don't want and press **Add the ticked
guides as drafts**. They are added as drafts, which readers can't see. Open
each one, read it, change anything you know better (opening days, your
favorite places, what changed recently), and publish it.

The guides link to each other (*Things to do on Guam* links to all of them).
A link to a guide you haven't published yet leads to a "not found" page, so
publish the guides that others link to, or take those links out.

A starter guide you deleted shows in the box again, so you can get it back
later; just leave it unticked. New versions of the site may bring new
starter guides, which show in the same box.

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
- **Who can read it all**: *Free* (everyone), or *Guide pack readers* (see
  [Selling guides](#selling-guides)).
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
You can sell guides in **guide packs**: a few guides sold together for one
price, such as a *PCS move pack* for military families or a *First week on
Guam pack* for visitors. Readers pay once through PayPal and read the guides
on all their devices. There are no accounts and no passwords.

### What readers see

- A guide set to **Guide pack readers** shows everyone its opening (the text
  before its first `##` heading), then the list of its parts and the packs
  that hold it, with their prices. Write a good opening: it is what makes
  people want the rest. A guide with no text before its first heading shows
  only its summary and the list of its parts.
- `/guides` lists the packs for sale at the top, and marks paid guides
  **Guide pack**.
- Each pack has its own page, `/guides/packs/<address>`, with what is in it,
  your "About the pack" text and a **Buy with PayPal** button.
- After paying, the buyer lands on their own page with their guides and a
  private link. That link is their key: opening it keeps their pass in that
  browser (in one cookie, used only on the guide pages), so the guides open
  in full there. On another phone or computer they open the link once.
- With email set up (see "Sending email" in `.env.example`), the link is also
  emailed to the address they paid with, and each pack page has a "Bought
  this before?" form that emails it again.
- Readers with a pack see no AdSense ads on its guides. A guide's own sponsor
  still shows.
- Search engines see the opening, like any reader without a pack, and are
  told the guide is for paying readers, as Google asks.

### What you need

- **PayPal**, set up as for the other sales (`PAYPAL_CLIENT_ID`,
  `PAYPAL_CLIENT_SECRET`, and `PAYPAL_MODE`; see the steps in
  `.env.example`). Until then, `/admin/guides` warns you, and paid guides show
  only their opening.
- **`STATS_PASSWORD`**, which you already use for `/admin/guides`.
- **Email** (optional but recommended), so buyers get their link by email.

No other setting: you make the packs and set their prices on
`/admin/guides`.

### The starter packs

`/admin/guides` has a **Starter guide packs** box with three packs written for
this site:

| Pack | Suggested price | Guides |
|---|---|---|
| PCS move pack for military families | $12 | The move checklist, finding a home, pets, cars and driving, schools and child care, typhoon season |
| First week on Guam pack | $5 | A seven-day plan, three or four days, Guam on a budget, before you fly |
| Every guide pack | $15 | Every guide for pack readers, also ones you add later |

Tick the ones you want and press **Add the ticked packs**. Each pack is added
**not for sale**, and its guides are added as drafts, set to *Guide pack
readers*. Then:

1. Open each new guide, read it, and change anything you know better. These
   guides give general advice and avoid prices and rules that change often,
   but check every fact you are not sure of (for example the pet import rules
   and the allowances for military housing) before you publish. The housing
   guide has a part about working with a local real estate agent; add your
   own details there if you like.
2. Publish the guides.
3. Open the pack (in the **Guide packs** list), check the price and the text,
   choose **For sale** and press **Save**.

### Making your own pack

Press **New guide pack** under **Guide packs** on `/admin/guides`:

- **Name**, **Address** (`/guides/packs/<address>`), **Price** in US dollars
  (from $1), **Order**, **For sale?**, **Summary**, and **About the pack** (in
  Markdown, like the guides).
- **Guides in this pack**: tick them, or tick **Every guide pack guide** for a
  pack that holds every paid guide, also the ones you add later.

Good to know:

- Readers see only the published guides of a pack. A pack whose guides are
  all drafts is not shown.
- Buyers read what the pack holds now: a guide you add later is theirs too,
  and a guide you take out is no longer theirs. So add, don't take out.
- A free guide can be in a pack (as a bonus); it stays free for everyone.
- A new price applies to new buyers only.
- To stop selling a pack, choose **Not for sale**. Its buyers keep their
  guides. A pack that was bought can't be deleted, so their links keep
  working.

### Sales, refunds and shared links

The **Guide pack sales** list on `/admin/guides` shows each sale: the day,
the pack, the price, the buyer's PayPal email address, how many times their
link was opened, and its state.

- **Turn off** a pass whose link was shared in public (a link opened very
  many times is a hint). Its guides stop opening with that link. **Turn on
  again** undoes it.
- **Refund** gives all the money back through PayPal and turns the pass off.
  Tick the box first.
- New sales are posted quietly in Discord (if the alerts are on) and counted
  in the daily money report as "Guide packs".

### Testing it

Test with `PAYPAL_MODE=sandbox` first, as for the paid listings: put a pack on
sale, buy it on its page with PayPal's test buyer (step 8 of the PayPal steps in
`.env.example`), and check that you land on your guides page, that
the guides open in full, and that the email arrives. Then, in another browser
(or a private window), check that the same guides show only their opening.
When it all works, switch to `PAYPAL_MODE=live`.

## Where the guides are kept

The guides are kept in the site's database, and their photos with the site's
other photos (in the `media` volume, folder `guides`). The nightly backup
keeps both (see [maintenance.md](maintenance.md#backups)). They are not in the
code: the code is public, so text you write, and may sell one day, stays
yours.
