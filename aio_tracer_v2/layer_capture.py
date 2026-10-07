"""Four independent linkification layers of one AIO capture (entity · source chip · image · card).

The course finding ``finding:independent_layers`` (confirmed) is that these four outcomes are resolved
by separate systems; the patent model (US11769017B1 response linkifying engine 138) describes linkifying
portions of a summary to different documents. A change in one layer implies nothing about another, so
each layer is recorded separately and its order is never assumed to match the others.

Standalone module: imports only ``entity_payloads`` helpers (no engine imports).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from entity_payloads import classify_chip_feature_type, extract_image_layer

ORDER_INDEPENDENCE_NOTE = "sentence_order, source_panel_order, card_order and map_order are recorded separately; independence is a hypothesis"


@dataclass
class EntityLayerRecord:
    binding_status: str  # entity_linked | plain_text | unknown
    business_name: str
    kg_id: Optional[str] = None
    cid: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    tel_uri: Optional[str] = None
    query: Optional[str] = None
    evidence_grade: str = "dom_confirmed"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SourceChipRecord:
    display_domain: Optional[str]
    citation_url: Optional[str]
    title: Optional[str] = None
    feature_type: str = "other"  # MAGI_FEATURE | Google | Facebook | Craigslist | Thumbtack | other
    secondary_sources: list = field(default_factory=list)
    uuid: Optional[str] = None  # session-specific; does not resolve in later captures
    evidence_grade: str = "dom_confirmed"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ImageLayerRecord:
    host_url: str
    filename: Optional[str] = None
    alt_text: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    has_exif_gps: Optional[bool] = None  # None = not checked (requires fetching the file)
    geographic_mismatch: Optional[bool] = None
    mismatch_details: Optional[str] = None
    evidence_grade: str = "dom_confirmed"  # the URL/alt are DOM facts; any "why" is a hypothesis

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CardLayerRecord:
    business_name: str
    rating: Optional[float] = None
    review_count: Optional[int] = None
    kg_id: Optional[str] = None
    in_source_drawer: Optional[bool] = None
    evidence_grade: str = "dom_confirmed"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FourLayerCapture:
    entity_layer: list[EntityLayerRecord] = field(default_factory=list)
    source_chip_layer: list[SourceChipRecord] = field(default_factory=list)
    image_layer: list[ImageLayerRecord] = field(default_factory=list)
    card_layer: list[CardLayerRecord] = field(default_factory=list)
    timestamp: Optional[str] = None
    query: Optional[str] = None
    location: Optional[str] = None
    device: Optional[str] = None
    order_independence_note: str = ORDER_INDEPENDENCE_NOTE

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> dict:
        """Compact, JSON-safe overview used in ``ExtractionResult.four_layer_summary``."""
        return {
            "query": self.query, "location": self.location, "device": self.device, "timestamp": self.timestamp,
            "entity_layer": {"count": len(self.entity_layer),
                             "entity_linked": [e.business_name for e in self.entity_layer
                                               if e.binding_status == "entity_linked"],
                             "plain_text": [e.business_name for e in self.entity_layer
                                            if e.binding_status == "plain_text"]},
            "source_chip_layer": {"count": len(self.source_chip_layer),
                                  "chips": [{"domain": c.display_domain, "url": c.citation_url,
                                             "feature_type": c.feature_type} for c in self.source_chip_layer]},
            "image_layer": {"count": len(self.image_layer),
                            "hosts": sorted({i.host_url for i in self.image_layer})[:10]},
            "card_layer": {"count": len(self.card_layer),
                           "order": [c.business_name for c in self.card_layer],
                           "not_in_source_drawer": [c.business_name for c in self.card_layer
                                                    if c.in_source_drawer is False]},
            "order_independence_note": self.order_independence_note,
            "evidence_grade": "mixed_observation_and_hypothesis",
        }


def _domain_of(url: Optional[str]) -> str:
    from urllib.parse import urlsplit

    host = (urlsplit(url).hostname or "") if url else ""
    return host[4:] if host.startswith("www.") else host


def _infer_device(user_agent: Optional[str]) -> Optional[str]:
    if not user_agent:
        return None
    return "mobile" if any(k in user_agent for k in ("Mobile", "Android", "iPhone")) else "desktop"


def build_four_layer_capture(signals: list, source_chips: list[dict], outer_html: str = "",
                             query: Optional[str] = None, location: Optional[str] = None,
                             device: Optional[str] = None, timestamp: Optional[str] = None,
                             user_agent: Optional[str] = None, image_records=None, card_records=None) -> FourLayerCapture:
    """Build the four layers from parsed signals (BusinessSignal-like objects) + source chip dicts."""
    chip_domains = {(c.get("domain") or _domain_of(c.get("url"))) for c in source_chips}
    chip_labels = {(c.get("label") or "").lower() for c in source_chips}
    cap = FourLayerCapture(query=query, location=location, device=device or _infer_device(user_agent),
                           timestamp=timestamp or datetime.now(timezone.utc).isoformat(timespec="seconds"))
    for s in signals:
        p: dict[str, Any] = getattr(s, "provenance", None) or {}
        cap.entity_layer.append(EntityLayerRecord(
            binding_status=getattr(s, "binding_status", "unknown"), business_name=s.name,
            kg_id=p.get("kg_id"), cid=p.get("cid"), lat=p.get("lat"), lon=p.get("lon"),
            tel_uri=p.get("tel_uri"), query=p.get("entity_query")))
        site_dom = _domain_of(p.get("website_url") or getattr(s, "website_url", None))
        in_drawer = bool((site_dom and site_dom in chip_domains) or s.name.lower() in chip_labels)
        cap.card_layer.append(CardLayerRecord(
            business_name=s.name, rating=s.rating, review_count=s.review_count, kg_id=p.get("kg_id"),
            in_source_drawer=in_drawer if source_chips else None,
            # card fields are DOM facts; drawer membership is a domain/label match against the captured
            # chips only (the full source drawer may hold more), hence heuristic.
            evidence_grade="heuristic" if source_chips else "dom_confirmed"))
    if isinstance(card_records, list):
        cap.card_layer = [CardLayerRecord(business_name=c.get("business_name") or "Unknown",
                                         rating=c.get("rating"), review_count=None,
                                         kg_id=c.get("kg_id"), in_source_drawer=None)
                          for c in card_records if isinstance(c, dict)]
    for c in source_chips:
        cap.source_chip_layer.append(SourceChipRecord(
            display_domain=c.get("domain") or _domain_of(c.get("url")), citation_url=c.get("url"),
            title=c.get("title"),
            feature_type=c.get("feature_type") or classify_chip_feature_type(c.get("url")),
            secondary_sources=list(c.get("secondary_sources") or []), uuid=c.get("uuid")))
    for img in (image_records if isinstance(image_records, list) else extract_image_layer(outer_html)):
        if "googleusercontent.com" in img["host_url"]:
            continue  # card thumbnails come from the entity record, not the image slot
        cap.image_layer.append(ImageLayerRecord(**{k: v for k, v in img.items() if k in ImageLayerRecord.__dataclass_fields__}))
    return cap
