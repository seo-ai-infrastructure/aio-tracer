"""Brand-name triangulation (aio_linkification Part 6 — hypothesis, not a confirmed causal rule).

Entity linking of a business name appears more likely when (1) the GBP listing name, (2) the cited
page ``<title>``/H1 and (3) the LocalBusiness schema ``name`` agree. sameAs profiles reinforce the
entity; priority: state licensing board > BBB > Yelp/HomeAdvisor/Angi. Standalone module.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Optional
from urllib.parse import urlsplit

LEGAL_SUFFIX_RE = re.compile(r"\b(llc|l\.l\.c\.|inc|incorporated|corp|co|company|ltd|pllc)\b\.?", re.I)
TITLE_SPLIT_RE = re.compile(r"\s+[|\-–—:]\s+")
SAMEAS_TIERS = (  # (strength, label, domain patterns)
    (3, "state_license_board", ("myfloridalicense.com", "license", "cslb.ca.gov", "tdlr.texas.gov", "dpor.virginia.gov")),
    (2, "bbb", ("bbb.org",)),
    (1, "directory", ("yelp.com", "homeadvisor.com", "angi.com", "angieslist.com")),
)


@dataclass
class TriangulationResult:
    gbp_name: str
    page_title: Optional[str]
    h1_text: Optional[str]
    schema_name: Optional[str]
    consistency_status: str  # consistent | partial_mismatch | full_mismatch
    recommended_canonical: str
    sameas_signals: list[dict] = field(default_factory=list)
    co_citation_found: Optional[bool] = None  # None = not checked (needs external backlink data)
    evidence_grade: str = "hypothesis"
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def normalize_brand(s: Optional[str]) -> str:
    s = (s or "").replace("’", "'")
    s = LEGAL_SUFFIX_RE.sub("", s)
    return re.sub(r"[^a-z0-9']+", " ", s.lower()).strip(" '")


class BrandTriangulator:
    def __init__(self, sameas_strengths=None):
        self.sameas_strengths = sameas_strengths or {}

    def check(self, gbp_name: str, page_title: Optional[str] = None, h1_text: Optional[str] = None,
              schema_name: Optional[str] = None, sameas_urls: Optional[list[str]] = None) -> TriangulationResult:
        gbp_n = normalize_brand(gbp_name)
        # A <title> often carries "Service | Brand" — compare the segment closest to the GBP name.
        title_brand = self._title_brand(page_title, gbp_n)
        others = {"page_title": title_brand, "h1_text": normalize_brand(h1_text) if h1_text else None,
                  "schema_name": normalize_brand(schema_name) if schema_name else None}
        present = {k: v for k, v in others.items() if v}
        matches = [k for k, v in present.items() if v == gbp_n]
        notes = []
        if not present:
            status = "partial_mismatch"
            notes.append("Only the GBP name supplied — fetch the cited page title/H1 and schema to triangulate.")
        elif len(matches) == len(present):
            status = "consistent"
        elif matches:
            status = "partial_mismatch"
        else:
            status = "full_mismatch"
        for k, v in present.items():
            if k not in matches:
                notes.append(f"{k} normalizes to {v!r}, GBP to {gbp_n!r}.")
        raw = {"gbp_name": gbp_name, "page_title": page_title, "h1_text": h1_text, "schema_name": schema_name}
        if any(LEGAL_SUFFIX_RE.search(v or "") for v in raw.values()) and \
                len({bool(LEGAL_SUFFIX_RE.search(v)) for v in raw.values() if v}) > 1:
            notes.append("Legal suffix (LLC/Inc) used inconsistently — pick one form everywhere.")
        canonical = self._canonical_score([gbp_name, title_brand_raw(page_title, gbp_n), h1_text, schema_name])
        sameas = self._check_sameas(sameas_urls or [])
        for item in sameas:
            item["strength"] = self.sameas_strengths.get(item["tier"])
            item["evidence_grade"] = "hypothesis"
        if sameas and not any(s["tier"] == "state_license_board" for s in sameas):
            notes.append("No licensing-board URL supplied; no causal ranking strength is established.")
        return TriangulationResult(gbp_name, page_title, h1_text, schema_name, status, canonical or gbp_name,
                                   sameas, co_citation_found=None, notes=notes)

    @staticmethod
    def _title_brand(page_title: Optional[str], gbp_n: str) -> Optional[str]:
        if not page_title:
            return None
        parts = [normalize_brand(p) for p in TITLE_SPLIT_RE.split(page_title) if p.strip()]
        return next((p for p in parts if p == gbp_n), None) or max(
            parts, key=lambda p: len(set(p.split()) & set(gbp_n.split())), default=None)

    @staticmethod
    def _canonical_score(names: list[Optional[str]]) -> Optional[str]:
        """Most common normalized form; return the first original string carrying it (prefers no suffix)."""
        cleaned = [n for n in names if n]
        if not cleaned:
            return None
        counts = Counter(normalize_brand(n) for n in cleaned)
        best = counts.most_common(1)[0][0]
        candidates = [n for n in cleaned if normalize_brand(n) == best]
        return min(candidates, key=lambda n: (bool(LEGAL_SUFFIX_RE.search(n)), len(n))).strip()

    @staticmethod
    def _check_sameas(urls: list[str]) -> list[dict]:
        out = []
        for u in urls:
            host = (urlsplit(u).hostname or "").lower()
            tier = next(((s, label) for s, label, pats in SAMEAS_TIERS
                         if any(host == p or host.endswith("." + p) for p in pats if p != "license")), (0, "other"))
            out.append({"url": u, "tier": tier[1], "strength": tier[0]})
        return sorted(out, key=lambda d: -d["strength"])


def title_brand_raw(page_title: Optional[str], gbp_n: str) -> Optional[str]:
    """Original-case title segment that best matches the GBP name (for canonical voting)."""
    if not page_title:
        return None
    parts = [p.strip() for p in TITLE_SPLIT_RE.split(page_title) if p.strip()]
    return max(parts, key=lambda p: len(set(normalize_brand(p).split()) & set(gbp_n.split())), default=None)
