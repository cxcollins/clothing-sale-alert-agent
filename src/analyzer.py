from __future__ import annotations

import asyncio
import json
import logging
import time

from google import genai
from google.genai import types

from .schemas import SiteAnalysis, SiteFetchResult

log = logging.getLogger(__name__)

MODEL = "gemini-2.5-flash"
ANALYZER_CONCURRENCY = 3
MAX_ATTEMPTS = 3
RETRY_BASE_DELAY_SECONDS = 2.0

HOMEPAGE_INSTRUCTION = """\
You are a precise sale-detection assistant for a personal shopping digest.

Given the cleaned HTML of a retailer's homepage, extract structured JSON describing:

1. Whether the site is running a SITEWIDE sale — a promotion that covers most or all
   merchandise (e.g. "30% off everything", "extra 20% off sale with code", a hero
   banner offering site-wide savings). Do NOT mark sitewide_sale=true for category-
   specific sales (e.g. "tops on sale"), single-brand promos, or loyalty/free-
   shipping offers.

   - When sitewide_sale is true and the headline is a percentage discount, return
     `sitewide_discount_pct` as a single integer (the headline % — for "20-50% off
     everything" use 50). For non-percentage promos (BOGO, "$50 off $200"), set
     sitewide_discount_pct to null.
   - Set `sale_banner_url` to the absolute URL of the hero/promo image representing
     this sale. Resolve relative URLs against the page's <base href>. Omit (null) if
     no clear banner image is present — never fabricate.

2. ALWAYS extract `sale_url` if the homepage links anywhere (nav, footer, hero
   banner, mega-menu) to a dedicated sale / clearance / outlet / markdowns page.
   Prefer the most general/comprehensive one (e.g. "/sale" over "/sale/mens-tees").
   Resolve relative URLs against <base href>. This is the pointer the orchestrator
   uses to drill in for actual discounted items, so it is important even when
   sitewide_sale is false. If no sale page is linked at all, set sale_url to null.

3. Discounted items surfaced on this homepage that fit the user's size categories.
   Return ALL such items you find — do NOT filter by % off; the orchestrator selects
   the top-N globally.

   - Match the item's product type to the closest user size key (e.g. a t-shirt →
     "t-shirts", chinos → "bottoms", a jacket → "outerwear"). Use the closest match
     even if not exact. If no category fits at all, omit the item.
   - Do NOT require visible size selectors. Homepages rarely expose them; the user
     will check sizing on the product page. Just match by product type.
   - An item is "discounted" if you can see either: a strikethrough/original price, an
     explicit "% off" marker, or sale/clearance language directly on the item card.
   - Set `discount_pct` to a single integer percent off (round to nearest). If you
     can see a sale and original price, compute the % yourself.
   - Set `image_url` to the absolute product image URL (resolve relative URLs against
     <base href>). Omit if no image is present.
   - Set `url` to the absolute product page URL. Omit the entire item if you cannot
     determine a real URL or price — never fabricate.

GENERAL RULES:
- Never fabricate URLs, prices, discount percentages, or product names.
- If you are uncertain about a field, omit it (use null) or omit the whole item.
- Return strictly valid JSON matching the provided schema.
- It's OK to return many items — the orchestrator picks the top N. Better to be
  generous than to return an empty list.
"""


SALE_PAGE_INSTRUCTION = """\
You are extracting discounted product cards from a retailer's SALE / CLEARANCE
page for a personal shopping digest.

Focus ONLY on populating `items_of_note`. Leave sitewide_sale=false and all other
sitewide fields null — those were already determined from the homepage.

For each visible product card on the page:
- Match the item's product type to the closest user size key (e.g. a t-shirt →
  "t-shirts", chinos → "bottoms", a jacket → "outerwear"). Use the closest match
  even if not exact. If no category fits at all, omit the item.
- Do NOT require visible size selectors — the user checks sizes on the product page.
- An item qualifies if you can see either: a strikethrough/original price, a "% off"
  marker, or "sale price" / "was $X / now $Y" language on the card.
- Set `discount_pct` to a single integer percent off (round to nearest). If you can
  see sale + original price, compute it yourself.
- Set `image_url` to the absolute product image URL (resolve relative URLs against
  <base href>). Omit if no image is present.
- Set `url` to the absolute product page URL. Omit the entire item if you cannot
  determine a real URL or price — never fabricate.

GENERAL RULES:
- Never fabricate URLs, prices, discount percentages, or product names.
- It's OK to return many items — the orchestrator picks the top N globally.
- Return strictly valid JSON matching the provided schema.
"""


async def analyze_all(
    fetched: list[SiteFetchResult],
    sizes: dict[str, str],
    api_key: str,
) -> list[SiteAnalysis]:
    client = genai.Client(api_key=api_key)
    sem = asyncio.Semaphore(ANALYZER_CONCURRENCY)
    tasks = [_analyze_one(client, sem, item, sizes, HOMEPAGE_INSTRUCTION) for item in fetched]
    return await asyncio.gather(*tasks)


async def analyze_sale_pages(
    fetched: list[SiteFetchResult],
    sizes: dict[str, str],
    api_key: str,
) -> list[SiteAnalysis]:
    """Pass 2: extract items from already-fetched sale-page HTML.

    Returns SiteAnalysis objects with items_of_note populated. The orchestrator
    is responsible for merging these items back into the pass-1 results.
    """
    client = genai.Client(api_key=api_key)
    sem = asyncio.Semaphore(ANALYZER_CONCURRENCY)
    tasks = [_analyze_one(client, sem, item, sizes, SALE_PAGE_INSTRUCTION) for item in fetched]
    return await asyncio.gather(*tasks)


async def _analyze_one(
    client: genai.Client,
    sem: asyncio.Semaphore,
    fetched: SiteFetchResult,
    sizes: dict[str, str],
    system_instruction: str,
) -> SiteAnalysis:
    if fetched.error or not fetched.cleaned_html:
        return SiteAnalysis(
            site_name=fetched.site_name,
            sitewide_sale=False,
            error=f"fetch failed: {fetched.error or 'no html'}",
        )

    prompt = _build_prompt(fetched, sizes)

    async with sem:
        resp, err = await _generate_with_retry(
            client, prompt, fetched.site_name, system_instruction
        )

    if resp is None:
        return SiteAnalysis(
            site_name=fetched.site_name,
            sitewide_sale=False,
            error=f"gemini call failed: {err}",
        )

    try:
        parsed = resp.parsed
        if isinstance(parsed, SiteAnalysis):
            parsed.site_name = fetched.site_name
            return parsed
        if isinstance(parsed, dict):
            return SiteAnalysis(**{**parsed, "site_name": fetched.site_name})
        if resp.text:
            data = json.loads(resp.text)
            return SiteAnalysis(**{**data, "site_name": fetched.site_name})
    except Exception as e:
        log.warning("Could not parse Gemini response for %s: %s", fetched.site_name, e)
        return SiteAnalysis(
            site_name=fetched.site_name,
            sitewide_sale=False,
            error=f"parse failed: {e}",
        )

    return SiteAnalysis(
        site_name=fetched.site_name,
        sitewide_sale=False,
        error="empty response from gemini",
    )


async def _generate_with_retry(
    client: genai.Client, prompt: str, site_name: str, system_instruction: str
):
    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        t0 = time.monotonic()
        try:
            resp = await asyncio.to_thread(
                client.models.generate_content,
                model=MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    response_mime_type="application/json",
                    response_schema=SiteAnalysis,
                    temperature=0.2,
                ),
            )
            elapsed = time.monotonic() - t0
            _log_gemini_success(site_name, resp, elapsed, attempt)
            return resp, None
        except Exception as e:
            last_error = e
            elapsed = time.monotonic() - t0
            if attempt == MAX_ATTEMPTS or not _is_retryable(e):
                break
            delay = RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
            log.info(
                "gemini transient error site=%s attempt=%d/%d elapsed=%.1fs retry_in=%.1fs err=%s",
                site_name, attempt, MAX_ATTEMPTS, elapsed, delay, e,
            )
            await asyncio.sleep(delay)
    log.warning("gemini failed site=%s err=%s", site_name, last_error)
    return None, str(last_error)


def _log_gemini_success(site_name: str, resp, elapsed: float, attempt: int) -> None:
    usage = getattr(resp, "usage_metadata", None)
    prompt_tokens = getattr(usage, "prompt_token_count", None) if usage else None
    output_tokens = getattr(usage, "candidates_token_count", None) if usage else None
    total_tokens = getattr(usage, "total_token_count", None) if usage else None

    finish_reason = None
    try:
        finish_reason = resp.candidates[0].finish_reason
        if hasattr(finish_reason, "name"):
            finish_reason = finish_reason.name
    except (AttributeError, IndexError):
        pass

    response_chars = len(resp.text) if getattr(resp, "text", None) else 0

    log.info(
        "gemini ok site=%s elapsed=%.1fs attempt=%d "
        "tokens_in=%s tokens_out=%s tokens_total=%s finish=%s resp_chars=%d",
        site_name, elapsed, attempt,
        prompt_tokens, output_tokens, total_tokens, finish_reason, response_chars,
    )


def _is_retryable(e: Exception) -> bool:
    msg = str(e).lower()
    retryable_markers = (
        "server disconnected",
        "timeout",
        "timed out",
        "connection reset",
        "connection aborted",
        "503",
        "502",
        "504",
        "429",
        "unavailable",
        "overloaded",
        "resource_exhausted",
    )
    return any(m in msg for m in retryable_markers)


def _build_prompt(fetched: SiteFetchResult, sizes: dict[str, str]) -> str:
    sizes_block = "\n".join(f"- {k}: {v}" for k, v in sizes.items())
    return (
        f"SITE: {fetched.site_name}\n"
        f"URL: {fetched.url}\n\n"
        f"USER SIZES:\n{sizes_block}\n\n"
        f"--- CLEANED HOMEPAGE HTML BELOW ---\n"
        f"{fetched.cleaned_html}\n"
        f"--- END HTML ---"
    )
