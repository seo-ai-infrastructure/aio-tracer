"""Decode Google's serialized ``<!--TgQPHd|||[...]-->`` comment payloads from AIO outerHTML.

These comment nodes are not rendered; Google's JS consumes them when a user clicks a
card. They are the closest thing in the DOM to "source" for each card field. Shapes
observed in the reference capture (emergency plumber near me, Lakeland FL):

* NAME_IMAGE  ``[[[photo_url,...], name, ..., [name, kg_id, null, null, "LOCAL_PLACE", ...]]]``
* ENTITY      ``[[[kg_id, feature_id, [kg_id, lon, lat, feature_id], null, null, query], ...]]``
* ACTION      ``[6, [[null, "tel:+1..."]]]`` (Call) · ``[4, [["", maps_dir_url]]]`` (Directions)
              · ``[5, [["", website_url]]]`` (Website) — plus wrapper variants of each
* SOURCE_CHIP ``[[uuid, null, null, 0, favicon_url, label, 25, url, ...]]``

Shapes are inferred from captured DOM, not documented by Google; unknown shapes are
kept as ``UNKNOWN`` so nothing is silently discarded.
"""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterator, Optional
from urllib.parse import parse_qs, unquote_plus, urlsplit

logger = logging.getLogger(__name__)

COMMENT_RE = re.compile(r"<!--(.*?)-->", re.S)
FEATURE_ID_RE = re.compile(r"^0x[0-9a-f]+:0x[0-9a-f]+$", re.I)
KG_ID_RE = re.compile(r"^/[gm]/[0-9a-z_]+$", re.I)
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
RATED_RE = re.compile(r'aria-label="Rated ([\d.]+) out of 5, ([\d.,]+[KkMm]?) reviews?\.?"')
IMG_SRC_RE = re.compile(r'<img[^>]+src="(https://lh[0-9]\.googleusercontent\.com/[^"]+)"')

ACTION_CALL, ACTION_DIRECTIONS, ACTION_WEBSITE = 6, 4, 5


def _flatten(obj: Any) -> Iterator[Any]:
    if isinstance(obj, list):
        for item in obj:
            yield from _flatten(item)
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _flatten(v)
    else:
        yield obj


def feature_id_to_cid(feature_id: Optional[str]) -> Optional[str]:
    """``0xAAAA:0xBBBB`` -> decimal CID of the second half (as string; exceeds JS int range)."""
    if not feature_id or not FEATURE_ID_RE.match(feature_id):
        return None
    return str(int(feature_id.split(":")[1], 16))


def digits(s: Optional[str]) -> str:
    d = re.sub(r"\D", "", s or "")
    return d[1:] if len(d) == 11 and d.startswith("1") else d


@dataclass
class RawPayload:
    index: int
    kind: str
    data: Any
    raw: str


@dataclass
class SourceChip:
    uuid: str
    label: Optional[str]
    url: Optional[str]
    favicon_url: Optional[str]
    domain: Optional[str]
    payload_index: int
    title: Optional[str] = None
    feature_type: str = "other"  # MAGI_FEATURE | Google | Facebook | Craigslist | Thumbtack | other
    secondary_sources: list = field(default_factory=list)


@dataclass
class EntityProvenance:
    """Everything Google embedded for one card, in payload order."""

    name: Optional[str] = None
    kg_id: Optional[str] = None
    feature_id: Optional[str] = None
    cid: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    entity_query: Optional[str] = None
    entity_type: Optional[str] = None
    photo_url: Optional[str] = None
    tel_uri: Optional[str] = None
    directions_url: Optional[str] = None
    directions_address: Optional[str] = None
    directions_feature_id: Optional[str] = None
    website_url: Optional[str] = None
    website_utm: dict = field(default_factory=dict)
    payload_indices: list[int] = field(default_factory=list)

    @property
    def maps_cid_url(self) -> Optional[str]:
        return f"https://maps.google.com/?cid={self.cid}" if self.cid else None

    @property
    def kgmid_url(self) -> Optional[str]:
        return f"https://www.google.com/search?kgmid={self.kg_id}" if self.kg_id else None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["maps_cid_url"] = self.maps_cid_url
        d["kgmid_url"] = self.kgmid_url
        return d


@dataclass
class PayloadDecodeResult:
    entities: list[EntityProvenance]
    source_chips: list[SourceChip]
    rating_labels: list[tuple[float, str]]
    image_srcs: list[str]
    payload_counts: dict
    unknown_payloads: int


def iter_payloads(outer_html: str) -> Iterator[RawPayload]:
    idx = 0
    for m in COMMENT_RE.finditer(outer_html or ""):
        body = m.group(1)
        if not body.startswith("TgQPHd|||"):
            continue
        raw = html.unescape(body[len("TgQPHd|||"):])
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("Undecodable TgQPHd payload: %.80s", raw)
            continue
        yield RawPayload(idx, classify_payload(data), data, raw)
        idx += 1


def classify_payload(data: Any) -> str:
    flat = [x for x in _flatten(data) if isinstance(x, str)]
    if "LOCAL_PLACE" in flat:
        return "NAME_IMAGE"
    try:
        head = data[0][0]
        if isinstance(head, list) and len(head) >= 2 and isinstance(head[0], str) \
                and KG_ID_RE.match(head[0]) and isinstance(head[1], str) and FEATURE_ID_RE.match(head[1]):
            return "ENTITY"
        if isinstance(data[0], list) and data[0] and isinstance(data[0][0], str) and UUID_RE.match(data[0][0]):
            return "SOURCE_CHIP"
    except (IndexError, TypeError, KeyError):
        pass
    if any(s.startswith("tel:") for s in flat):
        return "ACTION_CALL"
    if any("google.com/maps/dir/" in s for s in flat):
        return "ACTION_DIRECTIONS"
    if any(s.startswith("http") and "googleusercontent" not in s and "gstatic" not in s for s in flat):
        return "ACTION_WEBSITE"
    return "UNKNOWN"


def _parse_directions(url: str) -> tuple[Optional[str], Optional[str]]:
    m = re.search(r"/maps/dir//([^/]+)/", url)
    address = unquote_plus(m.group(1)) if m else None
    f = re.search(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)", url, re.I)
    return address, (f.group(1) if f else None)


def decode_outer_html(outer_html: str) -> PayloadDecodeResult:
    """Group payloads into per-card :class:`EntityProvenance` records, in document order."""
    entities: list[EntityProvenance] = []
    chips: list[SourceChip] = []
    counts: dict[str, int] = {}
    unknown = 0
    current: Optional[EntityProvenance] = None

    def ensure_current() -> EntityProvenance:
        nonlocal current
        if current is None:
            current = EntityProvenance()
            entities.append(current)
        return current

    for p in iter_payloads(outer_html):
        counts[p.kind] = counts.get(p.kind, 0) + 1
        flat = [x for x in _flatten(p.data) if isinstance(x, str)]
        if p.kind == "NAME_IMAGE":
            lp = _find_local_place(p.data)
            name = lp[0] if lp else None
            kg = lp[1] if lp and len(lp) > 1 else None
            # A new card begins unless this NAME_IMAGE repeats the current card's KG id.
            if current is None or (kg and current.kg_id and kg != current.kg_id) or (current.name and name != current.name):
                current = EntityProvenance()
                entities.append(current)
            current.name = current.name or name
            current.kg_id = current.kg_id or kg
            current.entity_type = "LOCAL_PLACE"
            photo = next((s for s in flat if "googleusercontent.com" in s), None)
            current.photo_url = current.photo_url or photo
            current.payload_indices.append(p.index)
        elif p.kind == "ENTITY":
            head = p.data[0][0]
            ent = ensure_current()
            if ent.kg_id and ent.kg_id != head[0]:
                ent = EntityProvenance()
                entities.append(ent)
                current = ent
            ent.kg_id = head[0]
            ent.feature_id = head[1]
            ent.cid = feature_id_to_cid(head[1])
            geo = head[2] if len(head) > 2 and isinstance(head[2], list) else None
            if geo and len(geo) >= 3:
                ent.lon, ent.lat = _to_float(geo[1]), _to_float(geo[2])
            if len(head) > 5 and isinstance(head[5], str):
                ent.entity_query = head[5]
            ent.payload_indices.append(p.index)
        elif p.kind == "ACTION_CALL":
            ent = ensure_current()
            ent.tel_uri = ent.tel_uri or next(s for s in flat if s.startswith("tel:"))
            ent.payload_indices.append(p.index)
        elif p.kind == "ACTION_DIRECTIONS":
            ent = ensure_current()
            if not ent.directions_url:
                url = next(s for s in flat if "google.com/maps/dir/" in s)
                ent.directions_url = url
                ent.directions_address, ent.directions_feature_id = _parse_directions(url)
            ent.payload_indices.append(p.index)
        elif p.kind == "ACTION_WEBSITE":
            ent = ensure_current()
            if not ent.website_url:
                url = next(s for s in flat if s.startswith("http"))
                ent.website_url = url
                ent.website_utm = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()
                                   if k.startswith("utm_")}
            ent.payload_indices.append(p.index)
        elif p.kind == "SOURCE_CHIP":
            row = p.data[0]
            # Recognized nested rich-card variant from captured SOURCE_CHIP payloads.
            rich = row[1] if len(row)>1 and isinstance(row[1],list) else None
            nested = bool(rich and len(rich)>5 and isinstance(rich[0],str) and isinstance(rich[5],str) and rich[5].startswith(('https://','http://')))
            url = rich[5] if nested else row[7] if len(row)>7 and isinstance(row[7],str) else None
            chips.append(SourceChip(
                uuid=row[0],
                label=(rich[4][0] if isinstance(rich[4],list) and rich[4] else rich[0]) if nested else row[5] if len(row)>5 and isinstance(row[5],str) else None,
                url=url,
                favicon_url=rich[2] if nested else row[4] if len(row)>4 and isinstance(row[4],str) else None,
                domain=_domain(url),
                payload_index=p.index,
                title=rich[0] if nested else _chip_title(row),
                feature_type=classify_chip_feature_type(url, p.raw),
                secondary_sources=[x for x in flat if isinstance(x, str) and x.startswith("http")
                                   and x != url and "favicon" not in x.lower()],
            ))
        else:
            unknown += 1

    rating_labels = [(float(r), c) for r, c in RATED_RE.findall(outer_html or "")]
    image_srcs = IMG_SRC_RE.findall(outer_html or "")
    entities = [e for e in entities if e.name or e.kg_id]
    logger.debug("Decoded %d entities, %d chips, payload kinds=%s", len(entities), len(chips), counts)
    return PayloadDecodeResult(entities, chips, rating_labels, image_srcs, counts, unknown)


def _find_local_place(data: Any) -> Optional[list]:
    if isinstance(data, list):
        if len(data) >= 5 and data[4] == "LOCAL_PLACE":
            return data
        for item in data:
            found = _find_local_place(item)
            if found:
                return found
    return None


def _to_float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _domain(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    host = urlsplit(url).hostname or ""
    return host[4:] if host.startswith("www.") else host or None


# --------------------------------------------------------------------------- source chips (extended)
_FEATURE_DOMAINS = (
    ("maps.google.com", "Google"), ("google.com", "Google"), ("facebook.com", "Facebook"),
    ("craigslist.org", "Craigslist"), ("thumbtack.com", "Thumbtack"),
)


def classify_chip_feature_type(url: Optional[str], raw: Optional[str] = None) -> str:
    """Classify a source chip by its href domain (Google/Facebook/Craigslist/Thumbtack), else by the
    ``MAGI_FEATURE`` marker in its raw payload, else ``other``. dom_confirmed from the href/payload."""
    host = (urlsplit(url).hostname or "").lower() if url else ""
    for dom, label in _FEATURE_DOMAINS:
        if host == dom or host.endswith("." + dom):
            return label
    if raw and "MAGI_FEATURE" in raw:
        return "MAGI_FEATURE"
    return "other"


def _chip_title(row: list) -> Optional[str]:
    """Best-effort page title: first string after the URL slot that is prose (not a URL/uuid)."""
    for x in row[8:]:
        if isinstance(x, str) and len(x) > 12 and " " in x and not x.startswith("http"):
            return x
    return None


# --------------------------------------------------------------------------- image layer
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.I)
_EP_SRC_RE = re.compile(r"<[a-z]+\b[^>]*data-ep-type=\"src\"[^>]*>", re.I)
_ATTR_RE = re.compile(r'([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*"([^"]*)"')


def _attrs(tag: str) -> dict:
    return {k.lower(): html.unescape(v) for k, v in _ATTR_RE.findall(tag)}


def _int(v: Optional[str]) -> Optional[int]:
    try:
        return int(float(v)) if v not in (None, "") else None
    except ValueError:
        return None


def extract_image_layer(outer_html: str) -> list[dict]:
    """Image-layer records from ``data-ep-type="src"`` elements and ``<img>`` tags.

    Returns ``{host_url, filename, alt_text, width, height}`` dicts (inline ``data:`` sprites are skipped).
    ``host_url`` is the page/record URL when the element carries one (href / data-url / data-src-url),
    otherwise the image URL itself — log the DOM URL, never a guess from the picture.
    """
    out: list[dict] = []
    seen: set = set()
    for tag in _EP_SRC_RE.findall(outer_html or "") + _IMG_TAG_RE.findall(outer_html or ""):
        a = _attrs(tag)
        src = a.get("src") or a.get("data-src") or ""
        host = a.get("href") or a.get("data-url") or a.get("data-src-url") or a.get("data-lpage") or src
        if not host or host.startswith("data:"):
            continue
        key = (host, src)
        if key in seen:
            continue
        seen.add(key)
        path = urlsplit(src or host).path
        out.append({
            "host_url": host,
            "filename": path.rsplit("/", 1)[-1] or None,
            "alt_text": a.get("alt") or a.get("aria-label") or None,
            "width": _int(a.get("width") or a.get("data-width")),
            "height": _int(a.get("height") or a.get("data-height")),
        })
    return out
