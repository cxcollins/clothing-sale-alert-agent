from __future__ import annotations

import asyncio
import logging
import sys

from dotenv import load_dotenv

from .analyzer import analyze_all, analyze_sale_pages
from .config import Config, Site, load_config
from .emailer import render_email, send_email
from .fetcher import fetch_all
from .schemas import FailedSite, ItemOfNote, ScanReport, SiteAnalysis, SiteFetchResult


log = logging.getLogger("main")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("google_genai").setLevel(logging.WARNING)


def select_sitewide(analyses: list[SiteAnalysis]) -> list[SiteAnalysis]:
    sitewide = [a for a in analyses if a.sitewide_sale]
    return sorted(
        sitewide,
        key=lambda a: (
            a.sitewide_discount_pct is None,
            -(a.sitewide_discount_pct or 0),
            a.site_name.lower(),
        ),
    )


def select_top_items(
    analyses: list[SiteAnalysis], top_n: int
) -> tuple[list[ItemOfNote], dict[str, str]]:
    pool: list[tuple[ItemOfNote, str]] = []
    for a in analyses:
        for item in a.items_of_note:
            pool.append((item, a.site_name))

    pool.sort(key=lambda pair: (-pair[0].discount_pct, pair[0].name.lower()))
    chosen = pool[:top_n]
    items = [p[0] for p in chosen]
    lookup = {p[0].url: p[1] for p in chosen}
    return items, lookup


def collect_failures(
    fetched: list[SiteFetchResult], analyses: list[SiteAnalysis]
) -> list[FailedSite]:
    failures: list[FailedSite] = []
    by_name = {a.site_name: a for a in analyses}
    fetched_names = {f.site_name for f in fetched}
    for f in fetched:
        if f.error:
            failures.append(FailedSite(site_name=f.site_name, reason=f"fetch failed: {f.error}"))
            continue
        a = by_name.get(f.site_name)
        if a is None:
            failures.append(FailedSite(site_name=f.site_name, reason="analysis missing"))
        elif a.error:
            failures.append(FailedSite(site_name=f.site_name, reason=a.error))
    for a in analyses:
        if a.site_name not in fetched_names and a.error:
            failures.append(FailedSite(site_name=a.site_name, reason=a.error))
    return failures


async def run(config: Config) -> ScanReport:
    fetched = await fetch_all(config.sites)
    analyses = await analyze_all(
        fetched, config.sizes, api_key=config.secrets.gemini_api_key
    )

    # Pass 2: for every site where pass 1 surfaced a sale_url, fetch that page
    # and ask Gemini to extract discounted product cards. Homepages almost never
    # surface concrete items with strikethrough prices — the sale page does.
    #
    # COST NOTE: this roughly 2x's Gemini calls per scan. If quota becomes a
    # concern, switch to manual sale URLs in config/sites.yaml (each site gets
    # an optional `sale_url:` field) so pass 1 only runs for sitewide detection
    # and pass 2 uses the hand-curated URL without burning a Gemini call to
    # discover it.
    sale_targets = _sale_page_targets(analyses)
    if sale_targets:
        log.info("pass 2: fetching %d sale page(s)", len(sale_targets))
        sale_fetched = await fetch_all(sale_targets)
        sale_analyses = await analyze_sale_pages(
            sale_fetched, config.sizes, api_key=config.secrets.gemini_api_key
        )
        _merge_sale_items(analyses, sale_analyses)

    sitewide = select_sitewide(analyses)
    items, lookup = select_top_items(analyses, config.filters.top_n_items_of_note)
    failures = collect_failures(fetched, analyses)
    empty_sites = collect_empty_sites(analyses, failures)

    return ScanReport(
        sitewide=sitewide,
        items=items,
        item_site_lookup=lookup,
        failures=failures,
        empty_sites=empty_sites,
        total_sites=len(config.sites),
    )


def _sale_page_targets(analyses: list[SiteAnalysis]) -> list[Site]:
    targets: list[Site] = []
    for a in analyses:
        if a.error or not a.sale_url:
            continue
        try:
            targets.append(Site(name=a.site_name, url=a.sale_url))
        except Exception as e:
            log.warning("skipping invalid sale_url for %s: %s (%s)", a.site_name, a.sale_url, e)
    return targets


def _merge_sale_items(
    analyses: list[SiteAnalysis], sale_analyses: list[SiteAnalysis]
) -> None:
    by_name = {a.site_name: a for a in analyses}
    for sa in sale_analyses:
        target = by_name.get(sa.site_name)
        if target is None:
            continue
        if sa.error:
            log.info("sale page failed for %s, keeping pass-1 items: %s", sa.site_name, sa.error)
            continue
        if sa.items_of_note:
            target.items_of_note.extend(sa.items_of_note)


def collect_empty_sites(
    analyses: list[SiteAnalysis], failures: list[FailedSite]
) -> list[str]:
    failed_names = {f.site_name for f in failures}
    empty: list[str] = []
    for a in analyses:
        if a.site_name in failed_names:
            continue
        if a.error:
            continue
        if not a.sitewide_sale and not a.items_of_note:
            empty.append(a.site_name)
    return sorted(empty)


def main() -> int:
    _setup_logging()
    load_dotenv()
    config = load_config()

    log.info("scanning %d sites", len(config.sites))
    report = asyncio.run(run(config))

    log.info(
        "result: %d sitewide sales, %d items of note, %d empty, %d failures",
        len(report.sitewide),
        len(report.items),
        len(report.empty_sites),
        len(report.failures),
    )

    subject, body = render_email(report, config)
    send_email(subject, body, config)
    log.info("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
