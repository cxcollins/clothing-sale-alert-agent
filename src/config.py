from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, HttpUrl, field_validator


class Site(BaseModel):
    name: str
    url: HttpUrl


class Filters(BaseModel):
    top_n_items_of_note: int = Field(default=5, ge=1, le=50)


class EmailConfig(BaseModel):
    to: str
    from_: str = Field(alias="from")
    subject_prefix: str = "[Sale Finder]"

    model_config = {"populate_by_name": True}


class Secrets(BaseModel):
    gemini_api_key: str
    resend_api_key: str


class Config(BaseModel):
    sites: list[Site]
    sizes: dict[str, str]
    filters: Filters = Field(default_factory=Filters)
    email: EmailConfig
    secrets: Secrets

    @field_validator("sizes", mode="before")
    @classmethod
    def _coerce_sizes_to_str(cls, v: object) -> object:
        if isinstance(v, dict):
            return {k: str(val) for k, val in v.items()}
        return v


def load_config(config_path: Path | None = None) -> Config:
    path = config_path or Path(__file__).resolve().parent.parent / "config" / "sites.yaml"
    with path.open() as f:
        raw = yaml.safe_load(f)

    secrets = Secrets(
        gemini_api_key=_require_env("GEMINI_API_KEY"),
        resend_api_key=_require_env("RESEND_API_KEY"),
    )
    return Config(**raw, secrets=secrets)


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Required environment variable {name} is not set")
    return value
