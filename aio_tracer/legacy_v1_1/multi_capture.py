"""Aggregate N captures of one query/location into a stability report (volatility, ≥5 captures).

AIO answers vary across captures (finding:volatility_5_captures; US20240289407A1 synthetic query fan-out
is a plausible mechanism). Observed: 1-Tom-Plumber appeared desktop-only; Ben Franklin appeared in 1 of 4
captures. Treat anything below 5 captures as anecdotal.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from itertools import combinations
from typing import Optional

from entity_payloads import extract_image_layer
from phone_analyzer import PhoneAnalyzer
from signal_extractor import ExtractionResult, SignalExtractor

MIN_CAPTURES = 5


@dataclass
class CaptureStabilityReport:
    query: Optional[str]
    location: Optional[str]
    n_captures: int
    entity_set_stability: dict
    phone_consistency: dict
    image_slot_stability: dict
    card_order_variance: dict
    min_captures_recommendation: int = MIN_CAPTURES
    conclusion: str = ""
    evidence_grade: str = "dom_confirmed"
    references: list[str] = field(default_factory=lambda: ["finding:volatility_5_captures", "US20240289407A1"])

    def to_dict(self) -> dict:
        return asdict(self)


def _spearman(a: list[str], b: list[str]) -> Optional[float]:
    common = [x for x in a if x in b]
    n = len(common)
    if n < 2:
        return None
    ra = {x: i for i, x in enumerate([x for x in a if x in common])}
    rb = {x: i for i, x in enumerate([x for x in b if x in common])}
    d2 = sum((ra[x] - rb[x]) ** 2 for x in common)
    return round(1 - 6 * d2 / (n * (n * n - 1)), 3)


class MultiCaptureAggregator:
    def __init__(self, extractor: Optional[SignalExtractor] = None):
        self.extractor = extractor or SignalExtractor()

    def aggregate(self, traces: list[dict], location: Optional[str] = None) -> CaptureStabilityReport:
        extractions = [self.extractor.extract(t) for t in traces]
        query = next((e.query for e in extractions if e.query), None)
        ent = self._entity_stability(extractions)
        phones = self._phone_consistency(extractions)
        imgs = self._image_stability(traces)
        cards = self._card_variance(extractions)
        n = len(traces)
        parts = [f"{n} capture(s)."]
        if n < MIN_CAPTURES:
            parts.append(f"Below the {MIN_CAPTURES}-capture minimum — treat results as anecdotal.")
        if ent["unstable"]:
            parts.append("Unstable entities: " + ", ".join(f"{k} ({v}/{n})" for k, v in ent["unstable"].items()) + ".")
        if phones["inconsistent"]:
            parts.append("Phone varied for: " + ", ".join(phones["inconsistent"]) + ".")
        if cards["mean_rank_correlation"] is not None:
            parts.append(f"Card order mean rank correlation {cards['mean_rank_correlation']}.")
        return CaptureStabilityReport(query, location, n, ent, phones, imgs, cards, conclusion=" ".join(parts))

    @staticmethod
    def _entity_stability(extractions: list[ExtractionResult]) -> dict:
        n = len(extractions)
        counts: dict[str, int] = {}
        for e in extractions:
            for name in {s.name for s in e.signals}:
                counts[name] = counts.get(name, 0) + 1
        sets = [frozenset(s.name for s in e.signals) for e in extractions]
        jacc = [len(a & b) / len(a | b) for a, b in combinations(sets, 2) if a | b]
        return {"appearances": counts,
                "frequency": {k: round(v / n, 2) for k, v in counts.items()} if n else {},
                "stable": sorted(k for k, v in counts.items() if v == n),
                "unstable": {k: v for k, v in sorted(counts.items()) if v < n},
                "mean_jaccard": round(sum(jacc) / len(jacc), 3) if jacc else (1.0 if n == 1 else None)}

    @staticmethod
    def _phone_consistency(extractions: list[ExtractionResult]) -> dict:
        seen: dict[str, dict[str, set]] = {}
        for e in extractions:
            for s in e.signals:
                d = seen.setdefault(s.name, {"card": set(), "tel": set()})
                if (c := PhoneAnalyzer.normalize_phone(s.phone)):
                    d["card"].add(c)
                if (t := PhoneAnalyzer.normalize_phone((s.provenance or {}).get("tel_uri"))):
                    d["tel"].add(t)
        per = {k: {"card_phones": sorted(v["card"]), "payload_tels": sorted(v["tel"]),
                   "consistent": len(v["card"]) <= 1 and len(v["tel"]) <= 1} for k, v in seen.items()}
        return {"per_business": per, "inconsistent": sorted(k for k, v in per.items() if not v["consistent"])}

    @staticmethod
    def _image_stability(traces: list[dict]) -> dict:
        per_capture = []
        for t in traces:
            outer = (t.get("root") or {}).get("outerHTML") or ""
            per_capture.append(sorted({i["host_url"] for i in extract_image_layer(outer)
                                       if "googleusercontent.com" not in i["host_url"]}))
        counts: dict[str, int] = {}
        for hosts in per_capture:
            for h in hosts:
                counts[h] = counts.get(h, 0) + 1
        n = len(traces)
        return {"per_capture": per_capture, "host_frequency": counts,
                "stable_hosts": sorted(h for h, c in counts.items() if c == n),
                "stable": bool(per_capture) and all(p == per_capture[0] for p in per_capture)}

    @staticmethod
    def _card_variance(extractions: list[ExtractionResult]) -> dict:
        orders = [[s.name for s in e.signals] for e in extractions]
        corrs = [c for a, b in combinations(orders, 2) if (c := _spearman(a, b)) is not None]
        return {"orders": orders, "pairwise_rank_correlation": corrs,
                "mean_rank_correlation": round(sum(corrs) / len(corrs), 3) if corrs else None,
                "note": "Card order is not sorted by rating; compare ranks only between captures."}
