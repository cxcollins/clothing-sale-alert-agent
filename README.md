# Sale Finder

A daily-running **AI agent** that scans your favorite retailers, decides which ones are worth your attention, and emails you a curated digest — powered by Google Gemini.

Each morning you get a single email with:
- Every site running a **sitewide sale**, ordered by discount magnitude, with the promo banner.
- The **top-N most-discounted items** in your sizes across every site, with product photos and direct links.
- A transparency footer listing sites that were scanned but had no findings, and sites that failed.

## Flow

LLM decision points are marked with `[LLM]`:

```
                    ┌──────────────────────────────┐
                    │  GitHub Actions (daily cron) │
                    │     0 14 * * *  (UTC)        │
                    └──────────────┬───────────────┘
                                   │
                                   ▼
                ┌──────────────────────────────────────┐
                │  src/main.py                         │
                │  load_config()  ←  config/sites.yaml │
                │                  ←  .env / secrets   │
                └──────────────┬───────────────────────┘
                               │
                               ▼
       ┌─────────────────────────────────────────────────────┐
       │  PASS 1 — Homepage scan                             │
       │                                                     │
       │  fetcher.fetch_all(sites)                           │
       │   ├─ httpx GET each homepage (concurrent, sem=5)    │
       │   └─ clean_html: strip script/style/svg/data-*,     │
       │      inject <base>, cap at 250K chars               │
       │                                                     │
       │  analyzer.analyze_all(..., HOMEPAGE_INSTRUCTION)    │
       │   └─ Gemini 2.5 Flash per site (concurrent, sem=3)  │
       │      structured output → SiteAnalysis               │
       │      ├─ sitewide_sale + discount_pct + banner_url   │
       │      ├─ sale_url   ◄── pointer for pass 2           │
       │      └─ items_of_note (usually empty on homepages)  │
       └────────────────────────────┬────────────────────────┘
                                    │
                                    ▼
       ┌─────────────────────────────────────────────────────┐
       │  PASS 2 — Sale-page enrichment                      │
       │  (only sites where pass 1 surfaced a sale_url)      │
       │                                                     │
       │  fetcher.fetch_all(sale_urls)                       │
       │  analyzer.analyze_sale_pages(..., SALE_INSTRUCTION) │
       │   └─ extracts discounted product cards              │
       │                                                     │
       │  _merge_sale_items()                                │
       │   └─ items appended to matching pass-1 analysis     │
       └────────────────────────────┬────────────────────────┘
                                    │
                                    ▼
       ┌─────────────────────────────────────────────────────┐
       │  Selection (src/main.py)                            │
       │                                                     │
       │  select_sitewide()  → all sitewide=true, sorted by  │
       │                       discount_pct desc             │
       │  select_top_items() → flatten all items, sort by    │
       │                       discount_pct desc, take top N │
       │  collect_failures() → fetch errors + Gemini errors  │
       │  collect_empty_sites() → scanned but no findings    │
       │                                                     │
       │  → ScanReport                                       │
       └────────────────────────────┬────────────────────────┘
                                    │
                                    ▼
       ┌─────────────────────────────────────────────────────┐
       │  emailer.render_email(report) → (subject, html)     │
       │  emailer.send_email() → resend.Emails.send()        │
       │                       → 📧 your inbox                │
       └─────────────────────────────────────────────────────┘
```

**Per-day API budget:** 2 Gemini calls per site (one per pass, when a sale page is found) — comfortably inside Gemini 2.5 Flash's free tier even at 20+ sites.

## Setup

### 1. Edit your config

[config/sites.yaml](config/sites.yaml) is the only file you need to change:

```yaml
sites:
  - name: J.Crew
    url: https://www.jcrew.com
  - name: Uniqlo
    url: https://www.uniqlo.com/us
  # ...add your favorites

sizes:
  tops: M
  bottoms: 32x32
  shoes: 10.5

filters:
  top_n_items_of_note: 5

email:
  to: you@example.com
  from: "onboarding@resend.dev"
```

> The default `onboarding@resend.dev` sender works out of the box but **only sends to the email address you signed up with at Resend.** To send to other addresses, verify a domain in the Resend dashboard and update `from:`.

### 2. Get the two secrets

| Secret | Where |
|---|---|
| `GEMINI_API_KEY` | https://aistudio.google.com/apikey |
| `RESEND_API_KEY` | https://resend.com/api-keys — create with **Sending access** only (scoped, not full account) |

### 3. Add them to GitHub

Repo → Settings → Secrets and variables → Actions → New repository secret. Add both.

### 4. Push the repo

The workflow runs automatically every day. To trigger it on demand: Actions → **Daily Sale Scan** → **Run workflow**.

## Local development

```bash
# install uv (https://docs.astral.sh/uv/)
brew install uv

# install deps
uv sync

# create .env from the template and fill in both secrets
cp .env.example .env

# run
uv run python -m src.main
```

## Project layout

```
sale-finder/
├── .github/workflows/daily-scan.yml   # cron trigger
├── config/sites.yaml                  # your sites, sizes, filters, email
├── src/
│   ├── main.py                        # orchestrator + selection logic
│   ├── config.py                      # YAML + env loader (pydantic-validated)
│   ├── fetcher.py                     # async HTTP + HTML cleaner
│   ├── analyzer.py                    # Gemini calls (both prompts) + retry
│   ├── emailer.py                     # HTML email rendering + Resend send
│   └── schemas.py                     # pydantic models
├── pyproject.toml                     # deps managed by uv
├── .env.example                       # secret names for local dev
└── README.md                          # this file
```

## Notes

- **Schedule.** GitHub Actions cron is UTC-only. `0 14 * * *` lands at 7am PDT in summer, 6am PST in winter. Edit [.github/workflows/daily-scan.yml](.github/workflows/daily-scan.yml) to change.
- **Failures don't crash the run.** Per-site errors (fetch timeout, Gemini disconnect, malformed JSON) surface in the "Sites that failed" section of the email. Transient Gemini errors auto-retry with exponential backoff (3 attempts, 2s → 4s → 8s).
- **Visibility.** Every site is accounted for in the email — even if no sitewide sale and no discounted items, it appears under "Scanned, no findings" so nothing silently vanishes.
- **Cost.** Gemini 2.5 Flash is on the free tier for low volume. Resend's free tier is 3k emails/month, 100/day. Both are massively over-provisioned for one daily email.
- **Cost fallback.** If Gemini quota becomes a concern, the two-pass design can collapse to one pass by adding hand-curated `sale_url:` fields per site in `sites.yaml`. See the comment block in that file and the COST NOTE in [src/main.py](src/main.py).
- **Security.** Both secrets are scoped — the Resend key only sends email (no account access), the Gemini key only calls the generative API. Rotate either from its respective dashboard if compromised.
