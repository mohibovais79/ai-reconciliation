"""Load typed settings from config/default.toml (plus optional overrides)."""

from __future__ import annotations

import copy
import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "default.toml"


class AppCfg(BaseModel):
    data_dir: str = ".data"
    max_file_mb: int = 10
    max_pages: int = 30
    demo_page_cap: int = 6
    image_max_side: int = 2000
    jpeg_quality: int = 85


class ProviderCfg(BaseModel):
    model: str
    base_url: str
    demo_env: str


class ProvidersCfg(BaseModel):
    default: str = "gemini"
    timeout_s: float = 120
    max_attempts: int = 4
    backoff_base_s: float = 1.5
    backoff_max_s: float = 30
    gemini: ProviderCfg
    openrouter: ProviderCfg
    groq: ProviderCfg

    def get(self, name: str) -> ProviderCfg:
        """Return the config block for a provider by name."""
        return getattr(self, name)


class ExtractionCfg(BaseModel):
    prompt_version: str = "v1"
    verification: str = "second_pass"


class SourcesCfg(BaseModel):
    order: list[str]
    labels: dict[str, str]
    owners: dict[str, str]


class MatchingCfg(BaseModel):
    source_of_truth: str = "none"
    compare_fields: list[str]
    supplier_threshold: int = 85
    item_threshold: int = 85
    tier2_min_score: float = 0.75
    tier2_margin: float = 0.15
    tier2_date_window_days: int = 2


class TolerancesCfg(BaseModel):
    qty_abs: Decimal = Decimal("0.01")
    qty_rel: Decimal = Decimal("0")
    date_days: int = 0


class ThresholdsCfg(BaseModel):
    late_entry_days: int = 3


class DatesCfg(BaseModel):
    default_dayfirst: bool = False


class NormalizeCfg(BaseModel):
    legal_suffixes: list[str]
    ocr_confusions: list[list[str]]


class Conversion(BaseModel):
    from_: str = Field(alias="from")
    to: str
    factor: Decimal


class UnitsCfg(BaseModel):
    aliases: dict[str, list[str]]
    conversions: list[Conversion] = []


class MappingCfg(BaseModel):
    synonyms: dict[str, list[str]]
    required: list[str] = ["qty"]
    header_min_score: int = 80


class ReportCfg(BaseModel):
    summary_max_items: int = 6


class Settings(BaseModel):
    app: AppCfg
    providers: ProvidersCfg
    extraction: ExtractionCfg
    sources: SourcesCfg
    matching: MatchingCfg
    tolerances: TolerancesCfg
    thresholds: ThresholdsCfg
    dates: DatesCfg
    normalize: NormalizeCfg
    units: UnitsCfg
    mapping: MappingCfg
    report: ReportCfg

    @property
    def data_path(self) -> Path:
        """Absolute path of the local data directory."""
        p = Path(self.app.data_dir)
        return p if p.is_absolute() else ROOT / p


def _deep_merge(base: dict, extra: dict) -> dict:
    """Recursively merge `extra` into a copy of `base`."""
    out = copy.deepcopy(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_settings(path: Path | None = None, overrides: dict[str, Any] | None = None) -> Settings:
    """Read the TOML config and apply optional nested overrides."""
    with open(path or DEFAULT_CONFIG, "rb") as f:
        raw = tomllib.load(f)
    if overrides:
        raw = _deep_merge(raw, overrides)
    return Settings.model_validate(raw)
