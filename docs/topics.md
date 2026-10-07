# Topics

Stories are sorted into topics: **Local, Military, Business and Labor** out of
the box, plus **Community** for posts from Reddit's r/guam. A story can belong
to more than one (a contract for the military buildup can be Military *and*
Business). Topics are defined in
**`config/categories.yaml`**, which you can edit to add keywords or whole new
topics.

## How a story gets its topics

Each topic has a list of **keywords**. For every story, GU Headlines looks for
those keywords and adds up points:

| Where the keyword is found | Points |
|---|---|
| In the **headline** | 3 |
| In the news site's own section name, tags or web address (e.g. `/business/`) | 2 |
| In the **first paragraph** | 1 |

A story gets a topic when it reaches **3 points**. So one keyword in the
headline is enough, but a keyword that only appears in the first paragraph
needs two more matches.

Three more rules:
- If the news site itself filed the story under a section with the topic's
  name (for example its address contains `/business/`), it gets that topic
  directly.
- Some news sites always get a topic. DVIDS and Stars and Stripes are always
  **Military**, for example. This is set with `categories:` on the site in
  `sources.yaml` (see [News sources](news-sources.md#every-option-explained)).
- A story that matches no topic goes to **Local**, the "everyday life on
  island" topic, marked `fallback: true`.
- **Community** has no keywords, so news stories never land in it. It holds
  the r/guam posts, because that site in `sources.yaml` has
  `only_in_topic: community`: its posts get only that topic, whatever they
  mention, and appear only on the Community page (not on the front page,
  the other topics or the archive).

## How keywords match

- **Whole words only:** `army` matches "the Army band" but not "armyworm".
- **Capital letters and accents don't matter:** `Hagåtña` matches "HAGATNA".
- **A `*` at the end matches any ending:** `employ*` matches employ,
  employer, employees, employment.
- **Phrases work:** `minimum wage`, `Camp Blaz`, `service member*`. A space
  in a keyword also matches a hyphen ("service-members").

## Editing categories.yaml

Open it from inside the `GU-headlines` folder:

```sh
nano config/categories.yaml
```

Each topic looks like this (shortened):

```yaml
categories:
  - slug: labor
    name: Labor
    description: Jobs, wages, workforce, unions and foreign-worker programs.
    keywords:
      - labor
      - workforce
      - worker*
      - minimum wage
      - H-2B
```

The same YAML rules as `sources.yaml` apply: spaces not tabs, items lined up,
one keyword per line starting with `- `
([details](news-sources.md#editing-sourcesyaml-safely)).

**Changes are used within a minute** for new stories and for the website's
menu. No restart is needed.

**To re-sort stories you already have** with the new keywords, run:

```sh
docker compose exec worker guheadlines reclassify
```

It prints something like `re-tagged 57 of 2310 articles`.

To check the file for mistakes, run
`docker compose exec worker guheadlines sources`. It reports any error in
either file.

## Common tasks

### Add a keyword

Add a line under the topic's `keywords:`. For example, to catch stories about
the Port under Business:

```yaml
      - Port Authority
      - cargo
```

Then run `reclassify` to update older stories.

### Stop a wrong match

If a word puts stories in the wrong topic, remove it, or replace it with a
more specific phrase. For example, `market` put the Chamorro Village *night
market* under Business, so it was replaced with `stock market`. Words that
are safe on their own are ones that almost always mean that topic:
*H-2B*, *Andersen*, *GVB*.

### Make a topic stricter or looser

Add `min_score:` to the topic (the default is 3):

```yaml
  - slug: business
    name: Business
    min_score: 4       # needs a headline keyword AND another match
```

A lower number (e.g. `2`) makes the topic catch more stories.

### Add a new topic

Add a new block at the end of the file, lined up with the others. For
example, Education:

```yaml
  - slug: education
    name: Education
    description: Schools, the University of Guam, GCC and students.
    keywords:
      - school*
      - students
      - teachers
      - GDOE
      - University of Guam
      - UOG
      - Guam Community College
      - GCC
      - scholarship*
```

- `slug` is its permanent ID: lowercase letters, numbers or dashes. It is used
  in addresses like `/category/education`.
- `name` is what appears in the top menu and on story labels.
- `description` appears at the top of the topic's page.

Within a minute, **Education** appears in the top menu and sidebar. Run
`reclassify` to sort existing stories into it.

New topics get a grey label. To give one its own color, add these lines at
the end of `guheadlines/web/static/style.css` (use your topic's slug and any
hex color), then run `docker compose up -d --build`:

```css
.tag-education { color: #7c3aed; background: rgba(124, 58, 237, .12); }
.dot.tag-education { background: #7c3aed; }
```

### Rename a topic

Change its `name`. Keep the `slug` the same, otherwise it becomes a different
topic and needs `reclassify`. The five built-in slugs are also used in the
page colors and by some news sites in `sources.yaml` (`categories:`).

### Remove a topic

1. Delete its block from `categories.yaml`.
2. Remove it from any site's `categories:` in `sources.yaml`. The check
   command reports this if you forget.
3. Run `reclassify`.

### Change which topic catches everything else

Move the `fallback: true` line to another topic. Only one topic should have
it. Without any, stories that match nothing get no topic label (they still
appear under **Latest** and in search).
