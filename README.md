# Buscador de propiedades — Puerto Rico

Scans the newest real-estate listings on ClasificadosOnline twice a day, remembers
what it has already seen, and publishes a page with anything new that matches your
filters. Runs free on GitHub Actions. No server, no database.

## Setup (about 10 minutes)

**1. Make the repo.** New GitHub repo — private is fine if you'll read the JSON
directly; make it **public** if you want the GitHub Pages URL to work, since Pages
on private repos needs a paid plan. Upload these four files, keeping the folders:

```
scraper.py
config.json
README.md
.github/workflows/scrape.yml
```

**2. Let the workflow commit.** Settings → Actions → General → Workflow
permissions → **Read and write permissions** → Save. Without this the run works
but can't save results.

**3. Turn on Pages.** Settings → Pages → Source: **Deploy from a branch** →
branch `main`, folder `/docs` → Save. Your page will be at
`https://<user>.github.io/<repo>/`. It appears after the first successful run.

**4. Run it once by hand.** Actions tab → "Buscar propiedades" → Run workflow.
Check the log: it should say something like `120 escaneadas · 120 nuevas`.

**5. Filters are already set.** `config.json` is configured for apartments and
multifamily under $350,000 in your areas. Two things worth understanding:

`zones_include` uses ClasificadosOnline's own area names, which must match the
site exactly — `San Juan - Viejo SJ`, `San Juan - Condado-Miramar`,
`San Juan - Hato Rey`. Those three map cleanly to what you want. Note that
Condado and Miramar share one zone on the site, so you get both together.

`zones_review` holds the two areas the site doesn't split finely enough.
Santurce is one zone covering both the blocks near Condado and the ones you'd
rather avoid, and Guaynabo isn't subdivided at all, so Torrimar and Finca Elena
land in the same bucket. Listings from those two areas go into a separate
**Por confirmar** section instead of being guessed at, with two exceptions:

- A title mentioning something in `zone_promote_keywords` (torrimar, san
  patricio, caparra, condado, miramar, ocean park…) gets promoted to a normal
  match.
- A title mentioning something in `zone_reject_keywords` (finca elena, villa
  mercedes, barrio obrero, cantera…) is dropped entirely.

Those two lists are the ones to grow over time. Every time something lands in
Por confirmar and you recognize the urbanización or the condo name, add it to
whichever list it belongs in and it'll be sorted automatically from then on.

One thing to decide: the $350,000 cap currently applies to multifamily too, and
multis in these areas often list higher. If you want a different ceiling for
those, say so and I'll add a per-type cap.

## Schedule

7:00 AM and 6:00 PM Puerto Rico time. To change it, edit the two `cron` lines in
`.github/workflows/scrape.yml`. Those are in **UTC**, so subtract 4 hours to get
Puerto Rico time. GitHub sometimes delays scheduled runs by a few minutes when
it's busy — normal, not a bug.

Note: GitHub disables scheduled workflows on repos with no activity for 60 days.
It emails you first; one manual run re-enables it.

## If the page comes up empty

The site's HTML could change and break the parsing. To see what happened:

```bash
pip install requests beautifulsoup4
python scraper.py --debug
```

That saves the raw page to `data/debug_page.html` and prints how many listings it
found per page. If it's `0 listings`, the row structure moved and the selectors in
`parse_listings()` need a small update.

## Being a good neighbor

The defaults scan 8 pages (about 120 listings) with a 3-second pause between
requests — roughly 10 requests, twice a day. That's less traffic than one person
browsing the site. Please keep it there. Raising `pages_to_scan` a lot or dropping
the delay turns a personal tool into something the site owners would reasonably
block, and this is their data being shown for free.

## Adding a second source

`clasificadospr.com` is a separate site with its own inventory (~1,200 properties)
and real filter parameters in the URL, which makes it easier to scrape than this
one. The listing dict format is simple — `id`, `url`, `title`, `price`, `beds`,
`baths`, `municipio`, `type`, `broker`, `source` — so a second scraper function
just has to return the same shape.
