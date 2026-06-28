from __future__ import annotations

import datetime as dt
import html
import logging

import resend

from .config import Config
from .schemas import ScanReport, SiteAnalysis

log = logging.getLogger(__name__)

PLACEHOLDER_STYLE = (
    "display:inline-block;width:140px;height:140px;background:#f1f1f1;"
    "border-radius:6px;color:#999;font-size:12px;text-align:center;line-height:140px;"
)
THUMB_STYLE = (
    "width:140px;height:140px;object-fit:cover;border-radius:6px;display:block;"
)
BANNER_STYLE = (
    "max-width:120px;max-height:60px;object-fit:cover;border-radius:4px;"
    "vertical-align:middle;margin-right:10px;"
)


def render_email(report: ScanReport, config: Config) -> tuple[str, str]:
    today = dt.date.today().strftime("%A, %B %-d")
    n_sitewide = len(report.sitewide)
    subject = (
        f"{config.email.subject_prefix} {n_sitewide} sitewide "
        f"{'sale' if n_sitewide == 1 else 'sales'} · {today}"
    )

    parts: list[str] = []
    parts.append(
        f"<h1 style='font-family:-apple-system,Segoe UI,Roboto,sans-serif;"
        f"font-size:22px;margin:0 0 4px 0;'>Sale Finder</h1>"
        f"<p style='color:#666;margin:0 0 24px 0;font-family:-apple-system,sans-serif;'>"
        f"{html.escape(today)} · scanned {report.total_sites} sites · "
        f"{n_sitewide} sitewide {'sale' if n_sitewide == 1 else 'sales'} found"
        f"</p>"
    )
    parts.append(_render_sitewide(report.sitewide))
    parts.append(_render_items(report))
    if report.empty_sites:
        parts.append(_render_empty(report.empty_sites))
    if report.failures:
        parts.append(_render_failures(report))

    body_html = (
        "<html><body style='font-family:-apple-system,Segoe UI,Roboto,sans-serif;"
        "color:#222;max-width:680px;margin:0 auto;padding:20px;'>"
        + "".join(parts)
        + "</body></html>"
    )
    return subject, body_html


def _render_sitewide(sitewide: list[SiteAnalysis]) -> str:
    if not sitewide:
        return (
            "<h2 style='font-size:18px;margin:24px 0 8px 0;'>Sitewide sales</h2>"
            "<p style='color:#888;'>None today.</p>"
        )

    rows: list[str] = []
    for s in sitewide:
        link = s.sale_url or _maybe(s.sale_description, "")
        href = s.sale_url or ""
        pct = (
            f"<strong>{s.sitewide_discount_pct}% off</strong>"
            if s.sitewide_discount_pct is not None
            else "<strong>Sale</strong>"
        )
        banner = (
            f"<img src='{html.escape(s.sale_banner_url)}' alt='' style='{BANNER_STYLE}'>"
            if s.sale_banner_url
            else ""
        )
        site_label = (
            f"<a href='{html.escape(href)}' style='color:#1a73e8;text-decoration:none;'>"
            f"{html.escape(s.site_name)}</a>"
            if href
            else html.escape(s.site_name)
        )
        desc = html.escape(s.sale_description or "")
        rows.append(
            f"<tr><td style='padding:8px 0;border-bottom:1px solid #eee;vertical-align:middle;'>"
            f"{banner}{site_label} — {pct}"
            f"<div style='color:#666;font-size:14px;margin-top:2px;'>{desc}</div>"
            f"</td></tr>"
        )

    return (
        "<h2 style='font-size:18px;margin:24px 0 8px 0;'>Sitewide sales</h2>"
        f"<table style='width:100%;border-collapse:collapse;'>{''.join(rows)}</table>"
    )


def _render_items(report: ScanReport) -> str:
    if not report.items:
        return (
            "<h2 style='font-size:18px;margin:32px 0 8px 0;'>Items of note</h2>"
            "<p style='color:#888;'>No discounted items in your sizes today.</p>"
        )

    rows: list[str] = []
    for item in report.items:
        site = report.item_site_lookup.get(item.url, "")
        thumb = (
            f"<img src='{html.escape(item.image_url)}' alt='' style='{THUMB_STYLE}'>"
            if item.image_url
            else f"<div style='{PLACEHOLDER_STYLE}'>no image</div>"
        )
        original = (
            f"<span style='color:#999;text-decoration:line-through;margin-right:6px;'>"
            f"{html.escape(item.original_price)}</span>"
            if item.original_price
            else ""
        )
        notes = (
            f"<div style='color:#a26100;font-size:12px;margin-top:4px;'>"
            f"{html.escape(item.notes)}</div>"
            if item.notes
            else ""
        )
        site_line = (
            f"<div style='color:#888;font-size:12px;'>"
            f"{html.escape(site)} · {html.escape(item.size_category)}</div>"
            if site
            else ""
        )
        rows.append(
            "<tr>"
            f"<td style='padding:12px 0;border-bottom:1px solid #eee;width:160px;vertical-align:top;'>"
            f"<a href='{html.escape(item.url)}'>{thumb}</a></td>"
            "<td style='padding:12px 0 12px 16px;border-bottom:1px solid #eee;vertical-align:top;'>"
            f"<a href='{html.escape(item.url)}' style='color:#222;font-weight:600;text-decoration:none;'>"
            f"{html.escape(item.name)}</a>"
            f"<div style='margin-top:4px;'>{original}"
            f"<strong>{html.escape(item.price)}</strong> "
            f"<span style='color:#1a8c4a;margin-left:6px;'>({item.discount_pct}% off)</span></div>"
            f"{site_line}{notes}"
            "</td></tr>"
        )

    return (
        "<h2 style='font-size:18px;margin:32px 0 8px 0;'>Items of note</h2>"
        f"<table style='width:100%;border-collapse:collapse;'>{''.join(rows)}</table>"
    )


def _render_failures(report: ScanReport) -> str:
    rows = "".join(
        f"<li>{html.escape(f.site_name)} — "
        f"<span style='color:#888;'>{html.escape(f.reason)}</span></li>"
        for f in report.failures
    )
    return (
        "<h2 style='font-size:14px;margin:32px 0 4px 0;color:#888;'>Sites that failed</h2>"
        f"<ul style='color:#888;font-size:13px;margin:0;padding-left:18px;'>{rows}</ul>"
    )


def _render_empty(empty_sites: list[str]) -> str:
    rows = "".join(
        f"<li>{html.escape(name)}</li>" for name in empty_sites
    )
    return (
        "<h2 style='font-size:14px;margin:32px 0 4px 0;color:#888;'>"
        "Scanned, no findings</h2>"
        f"<ul style='color:#888;font-size:13px;margin:0;padding-left:18px;'>{rows}</ul>"
    )


def _maybe(value: str | None, default: str) -> str:
    return value if value is not None else default


def send_email(subject: str, body_html: str, config: Config) -> None:
    resend.api_key = config.secrets.resend_api_key
    params: resend.Emails.SendParams = {
        "from": config.email.from_,
        "to": [config.email.to],
        "subject": subject,
        "html": body_html,
    }
    result = resend.Emails.send(params)
    log.info("email sent to %s (id=%s)", config.email.to, result.get("id"))
