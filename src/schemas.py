from __future__ import annotations

from pydantic import BaseModel, Field


class ItemOfNote(BaseModel):
    name: str
    price: str
    original_price: str | None = None
    discount_pct: int = Field(ge=0, le=100)
    url: str
    image_url: str | None = None
    size_category: str
    notes: str | None = None


class SiteAnalysis(BaseModel):
    site_name: str
    sitewide_sale: bool
    sale_description: str | None = None
    sitewide_discount_pct: int | None = Field(default=None, ge=0, le=100)
    sale_url: str | None = None
    sale_banner_url: str | None = None
    items_of_note: list[ItemOfNote] = Field(default_factory=list)
    error: str | None = None


class SiteFetchResult(BaseModel):
    site_name: str
    url: str
    cleaned_html: str | None = None
    error: str | None = None


class FailedSite(BaseModel):
    site_name: str
    reason: str


class ScanReport(BaseModel):
    sitewide: list[SiteAnalysis]
    items: list[ItemOfNote]
    item_site_lookup: dict[str, str]
    failures: list[FailedSite]
    empty_sites: list[str] = Field(default_factory=list)
    total_sites: int
