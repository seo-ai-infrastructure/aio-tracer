"""Parse an aio-source-trace/1.0 JSON into per-business :class:`BusinessSignal` objects.

Visible-text pattern handled (from the reference trace)::

    Stuart's Plumbing            <- name
    4.9                          <- rating
    (1.7K)                       <- abbreviated review count
    Plumber                      <- category
    Open3233 US-98               <- open status glued to short address
    Call / Directions / Website  <- action buttons
    Address: 3233 US-98, Lakeland, FL 33803
    Phone: 863-877-3108
    Hours: Open 24/7
    Rating: 4.9/5 (1,682 reviews)   <- exact review count
    Details: Known for rapid response times ...

Other ``Label: value`` rows the AIO sometimes adds (``Current Offer``, ``Services``,
``Credentials``, ``Location``) are kept in ``synthesized_fields``. Trailing related-result
chips (``Roto-Rooter`` / ``+5``) are split off into ``related_results``.

Each signal is joined to the Google-embedded entity payload for its card (KG id, feature
id/CID, coordinates, tel:, directions and website URLs) so every field can be traced.
"""

from __future__ import annotations

import csv
import json
import logging
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from entity_payloads import EntityProvenance, PayloadDecodeResult, decode_outer_html

logger = logging.getLogger(__name__)

RATING_RE = re.compile(r"^[0-5](?:\.\d)?$")
COUNT_RE = re.compile(r"^\(([\d.,]+)\s*([KkMm]?)\)$")
STATUS_RE = re.compile(
    r"^(Open 24 hours|Open now|Opens soon|Closes soon|Temporarily closed|Permanently closed|Open|Closed)"
    r"\s*(?:[⋅·•\-]\s*)?(.*)$"
)
LABEL_RE = re.compile(r"^([A-Z][A-Za-z /&]{1,30}):\s*(.*)$")
RATING_FIELD_RE = re.compile(r"([\d.]+)\s*/\s*5\s*\(([\d,]+)\s*reviews?\)", re.I)
RELATED_RE = re.compile(r"^\+\d+$")
ACTION_LINES = {"Call", "Directions", "Website", "Save", "Share", "Order", "Book", "Menu"}
CORE_LABELS = {"Address", "Phone", "Hours", "Rating", "Location"}


@dataclass
class BusinessSignal:
    name: str
    rating: Optional[float] = None
    review_count: Optional[int] = None
    category: Optional[str] = None
    is_open: Optional[bool] = None
    hours: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    details_text: Optional[str] = None
    photo_url: Optional[str] = None
    # --- extended trace fields ---
    position: int = 0
    open_status: Optional[str] = None
    short_address: Optional[str] = None
    review_count_display: Optional[str] = None
    review_count_is_exact: bool = False
    phones: list[str] = field(default_factory=list)
    website_url: Optional[str] = None
    synthesized_fields: dict[str, str] = field(default_factory=dict)
    raw_block: list[str] = field(default_factory=list)
    provenance: Optional[dict] = None
    # entity_linked: a role="button" element carries the name (DOM) · plain_text: none found · unknown: no DOM
    binding_status: str = "unknown"
    binding_evidence: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ExtractionResult:
    query: Optional[str]
    page_url: Optional[str]
    captured_at: Optional[str]
    signals: list[BusinessSignal]
    related_results: list[dict]
    source_chips: list[dict]
    payload_summary: dict
    warnings: list[str]
    four_layer_summary: Optional[dict] = None


def _norm(s: Optional[str]) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _parse_count(num: str, suffix: str) -> int:
    value = float(num.replace(",", ""))
    mult = {"k": 1_000, "m": 1_000_000}.get(suffix.lower(), 1)
    return int(round(value * mult))


class SignalExtractor:
    """Turn raw AIO traces into structured business signals."""

    def extract(self, trace: dict) -> ExtractionResult:
        warnings: list[str] = []
        root = trace.get("root") or {}
        text = root.get("visibleText") or ""
        outer = root.get("outerHTML") or ""
        if not text:
            warnings.append("trace has no root.visibleText (no AIO captured)")

        decoded = decode_outer_html(outer) if outer else PayloadDecodeResult([], [], [], [], {}, 0)
        lines, related = self._split_related(self._lines(text))
        known_names = {_norm(e.name) for e in decoded.entities if e.name}
        starts = self._block_starts(lines, known_names)
        if text and not starts:
            warnings.append("no business blocks recognised in visibleText")

        signals: list[BusinessSignal] = []
        for n, s in enumerate(starts):
            end = starts[n + 1] if n + 1 < len(starts) else len(lines)
            sig = self._parse_block(lines[s:end], position=n + 1)
            if sig:
                signals.append(sig)

        self._attach_provenance(signals, decoded, warnings)
        if outer or trace.get("interactive") or trace.get("attributedNodes"):
            for sig in signals:
                sig.binding_status, sig.binding_evidence = classify_entity_binding(
                    sig.name, outer, (trace.get("interactive") or []) + (trace.get("attributedNodes") or []))
        chips = [asdict(c) for c in decoded.source_chips]
        extended = trace.get("sourceChipsExtended")
        if isinstance(extended, list):
            for c in extended:
                if not isinstance(c, dict) or not c.get("citation_url"):
                    continue
                # Only explicit citation controls count; action/organic links are not citations.
                if c.get("kind") != "citation":
                    continue
                normalized = {**c, "url": c["citation_url"], "domain": c.get("display_domain")}
                old = next((x for x in chips if x.get("url") == normalized["url"]), None)
                if old:
                    old.update(normalized)
                else:
                    chips.append(normalized)
        try:
            from layer_capture import build_four_layer_capture  # local import: layer_capture is standalone

            cap = (trace.get("capture") or {})
            four = build_four_layer_capture(signals, chips, outer, query=trace.get("query"),
                                            location=cap.get("city") if isinstance(cap, dict) else None,
                                            timestamp=trace.get("capturedAtUTC"), user_agent=trace.get("userAgent"), image_records=trace.get("imageLayer"),
                                            card_records=trace.get("cardLayer"))
            four_summary = four.summary()
        except Exception as exc:  # never let the summary break extraction
            logger.warning("four-layer summary failed: %s", exc)
            four_summary = None
        return ExtractionResult(
            query=trace.get("query"),
            page_url=trace.get("pageURL"),
            captured_at=trace.get("capturedAtUTC"),
            signals=signals,
            related_results=related,
            source_chips=chips,
            payload_summary={"kinds": decoded.payload_counts, "unknown": decoded.unknown_payloads,
                             "entities": len(decoded.entities)},
            warnings=warnings,
            four_layer_summary=four_summary,
        )

    # ------------------------------------------------------------ text parsing
    @staticmethod
    def _lines(text: str) -> list[str]:
        return [ln.replace("\xa0", " ").strip() for ln in text.split("\n") if ln.strip()]

    @staticmethod
    def _split_related(lines: list[str]) -> tuple[list[str], list[dict]]:
        """Strip trailing ``Name`` / ``+N`` related-result chips."""
        related: list[dict] = []
        out = list(lines)
        i = len(out) - 1
        while i >= 1 and RELATED_RE.match(out[i]):
            related.insert(0, {"label": out[i - 1], "more": int(out[i][1:])})
            del out[i - 1:i + 1]
            i -= 2
        return out, related

    @staticmethod
    def _block_starts(lines: list[str], known_names: set[str]) -> list[int]:
        starts = []
        for i, ln in enumerate(lines):
            rating_pattern = (i + 2 < len(lines) and RATING_RE.match(lines[i + 1])
                              and COUNT_RE.match(lines[i + 2]) and not LABEL_RE.match(ln))
            if rating_pattern or (_norm(ln) in known_names and not LABEL_RE.match(ln)):
                if not starts or i > starts[-1]:
                    starts.append(i)
        return starts

    def _parse_block(self, block: list[str], position: int) -> Optional[BusinessSignal]:
        if not block:
            return None
        sig = BusinessSignal(name=block[0], position=position, raw_block=list(block))
        i = 1
        if i < len(block) and RATING_RE.match(block[i]):
            sig.rating = float(block[i])
            i += 1
        if i < len(block) and (m := COUNT_RE.match(block[i])):
            sig.review_count_display = f"{m.group(1)}{m.group(2)}"
            sig.review_count = _parse_count(m.group(1), m.group(2))
            i += 1
        if i < len(block) and not STATUS_RE.match(block[i]) and not LABEL_RE.match(block[i]) \
                and block[i] not in ACTION_LINES:
            sig.category = block[i]
            i += 1
        if i < len(block) and (m := STATUS_RE.match(block[i])):
            sig.open_status = m.group(1)
            sig.is_open = m.group(1).lower().startswith("open") or m.group(1) == "Closes soon"
            sig.short_address = m.group(2).strip() or None
            i += 1

        labeled: dict[str, str] = {}
        last_label: Optional[str] = None
        for ln in block[i:]:
            if ln in ACTION_LINES:
                continue
            m = LABEL_RE.match(ln)
            if m:
                last_label = m.group(1).strip()
                labeled[last_label] = m.group(2).strip()
            elif last_label:  # wrapped continuation of the previous row
                labeled[last_label] = f"{labeled[last_label]} {ln}".strip()

        sig.address = labeled.get("Address") or labeled.get("Location") or sig.short_address
        if "Phone" in labeled:
            sig.phones = [p.strip() for p in re.split(r"\s*/\s*|\s*,\s*", labeled["Phone"]) if p.strip()]
            sig.phone = sig.phones[0] if sig.phones else None
        sig.hours = labeled.get("Hours")
        if sig.hours and sig.is_open is None:
            sig.is_open = sig.hours.lower().startswith("open")
        if "Rating" in labeled and (m := RATING_FIELD_RE.search(labeled["Rating"])):
            sig.rating = float(m.group(1))
            sig.review_count = int(m.group(2).replace(",", ""))
            sig.review_count_is_exact = True
        sig.details_text = labeled.get("Details")
        sig.synthesized_fields = {k: v for k, v in labeled.items() if k not in CORE_LABELS}
        return sig

    # ------------------------------------------------------------ provenance
    @staticmethod
    def _attach_provenance(signals: list[BusinessSignal], decoded: PayloadDecodeResult,
                           warnings: list[str]) -> None:
        by_name = {}
        for entity in decoded.entities:
            if entity.name:by_name.setdefault(_norm(entity.name), []).append(entity)
        unused = list(decoded.entities)
        for idx, sig in enumerate(signals):
            candidates = [e for e in by_name.get(_norm(sig.name), []) if e in unused]
            matched_by = "name"
            if len(candidates) > 1:
                narrowed = [e for e in candidates if sig.address and e.directions_address and _norm(sig.address)==_norm(e.directions_address)]
                if len(narrowed) == 1:candidates=narrowed;matched_by="name_and_address"
                else:candidates=[];warnings.append(f"{sig.name!r}: ambiguous duplicate-name entities; no join made")
            ent: Optional[EntityProvenance] = candidates[0] if len(candidates)==1 else None
            if ent is None:
                warnings.append(f"{sig.name!r}: no entity payload found in outerHTML")
                continue
            if ent in unused:
                unused.remove(ent)
            sig.photo_url = ent.photo_url or sig.photo_url
            sig.website_url = ent.website_url
            prov = ent.to_dict()
            prov["matched_by"] = matched_by
            if idx < len(decoded.rating_labels):
                r, c = decoded.rating_labels[idx]
                prov["rating_aria_label"] = {"rating": r, "count_display": c}
            sig.provenance = prov
        if unused:
            warnings.append(f"{len(unused)} entity payload(s) had no matching visible block: "
                            + ", ".join(e.name or e.kg_id or "?" for e in unused))


_ROLE_BUTTON_RE = re.compile(r"<[a-z]+\b[^>]*\brole=\"button\"[^>]*>", re.I)
_ARIA_RE = re.compile(r'aria-label="([^"]*)"', re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def classify_entity_binding(name: str, outer_html: str = "", nodes: Optional[list] = None) -> tuple[str, Optional[str]]:
    """Prove a named control has entity-specific attributes or an entity destination.

    A generic button proves interactivity only, not entity resolution or underlining.
    """
    from bs4 import BeautifulSoup
    target = _norm(name)
    if not target:
        return "unknown", None
    soup = BeautifulSoup(outer_html or "", "html.parser")
    interactive = False
    for el in soup.select('a, [role="button"], [role="link"]'):
        label = _norm(el.get("aria-label") or el.get_text(" ", strip=True))
        if label != target and not label.startswith(target + " "):
            continue
        attrs = str(el.attrs)
        href = el.get("href", "")
        interactive = True
        if (re.search(r"(?:kgmid=|[?&]cid=|/maps/place/|/searchviewer/)", href) or
                any(k in el.attrs for k in ("data-entity-id", "data-kgmid", "data-cid")) or
                "TgQPHd|||" in str(el)):
            return "entity_linked", "named control with entity-specific destination/payload"
    if interactive:
        return "interactive_unverified", "named control exists; entity resolution and underline not verified"
    return "plain_text", "no entity-specific named control observed; resolver cause unknown"


def html_unescape(s: str) -> str:
    import html

    return html.unescape(s or "")


CSV_FIELDS = [
    "position", "name", "rating", "review_count", "review_count_display", "category", "is_open",
    "open_status", "hours", "address", "phone", "website_url", "details_text", "photo_url",
    "kg_id", "feature_id", "cid", "lat", "lon", "tel_uri", "maps_cid_url",
    "synthesized_fields", "binding_status",
]


def signals_to_rows(signals: list[BusinessSignal]) -> list[dict[str, Any]]:
    rows = []
    for s in signals:
        p = s.provenance or {}
        rows.append({
            "position": s.position, "name": s.name, "rating": s.rating, "review_count": s.review_count,
            "review_count_display": s.review_count_display, "category": s.category, "is_open": s.is_open,
            "open_status": s.open_status, "hours": s.hours, "address": s.address, "phone": s.phone,
            "website_url": s.website_url, "details_text": s.details_text, "photo_url": s.photo_url,
            "kg_id": p.get("kg_id"), "feature_id": p.get("feature_id"), "cid": p.get("cid"),
            "lat": p.get("lat"), "lon": p.get("lon"), "tel_uri": p.get("tel_uri"),
            "maps_cid_url": p.get("maps_cid_url"),
            "binding_status": s.binding_status,
            "synthesized_fields": json.dumps(s.synthesized_fields, ensure_ascii=False) if s.synthesized_fields else "",
        })
    return rows


def write_csv(signals: list[BusinessSignal], path: Path | str, extra_columns: Optional[dict[int, dict]] = None) -> None:
    rows = signals_to_rows(signals)
    fields = list(CSV_FIELDS)
    if extra_columns:
        for i, row in enumerate(rows):
            row.update(extra_columns.get(i, {}))
        for cols in extra_columns.values():
            fields += [k for k in cols if k not in fields]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
