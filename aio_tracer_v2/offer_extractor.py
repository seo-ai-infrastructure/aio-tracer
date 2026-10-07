"""Offer linkification: detect offer lines in AIO synthesized fields and how they were likely grounded.

Three candidate mechanisms (aio_linkification §3.1–3.3, all hypotheses) plus the OCR path (Part 4):

* ``gbp_offer_post``        — the AIO ``Current Offer:`` row (GBP Offer post: title + end date + CTA URL)
* ``offer_schema``          — JSON-LD ``Offer`` with ``priceValidUntil`` in the cited page
* ``declarative_proximity`` — a dollar value + named service + constraint sentence in visible HTML
* ``ocr_extraction``        — the offer exists only in an image (alt/filename/banner), not in visible text

``multimodal_risk`` is True when the offer is found only via the image path. Such offers are exposed to
generative interpolation and *temporal drift* (a missing year filled from the model's current date).
Everything here is graded ``hypothesis``. Standalone: must not import ``attribution_engine``.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Optional

DOLLAR_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)")
MONTHS = ("january|february|march|april|may|june|july|august|september|october|november|december|"
          "jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec")
EXPIRY_RE = re.compile(
    rf"(?:valid\s+(?:through|thru|until)|expires?(?:\s+on)?|ends?|through|until|by)\s+"
    rf"((?:{MONTHS})\.?(?:\s+\d{{1,2}}(?:st|nd|rd|th)?(?!\d))?(?:,?\s+\d{{4}})?|\d{{1,2}}/\d{{1,2}}(?:/\d{{2,4}})?|\d{{4}}-\d{{2}}-\d{{2}})",
    re.I)
SERVICE_RE = re.compile(
    r"(?:off|on|for)\s+(?:an?\s+|any\s+|your\s+)?([A-Za-z][A-Za-z \-]{2,60}?)"
    r"(?=\s*(?:[—–\-,.;:(]|valid|through|until|expires|ends|when|with|$))", re.I)
SERVICE_AFTER_PRICE_RE = re.compile(r"\$\s?[\d,.]+\s+([A-Za-z][A-Za-z \-]{2,60}?)(?=\s*(?:[—–\-,.;:(]|$))")
OFFER_LABELS = ("Current Offer", "Offer", "Special", "Promotion", "Deal", "Coupon")
JSONLD_RE = re.compile(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.I)
IMG_RE = re.compile(r"<img\b[^>]*>", re.I)
ALT_RE = re.compile(r'\balt="([^"]*)"', re.I)
SRC_RE = re.compile(r'\bsrc="([^"]*)"', re.I)
TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class OfferRecord:
    raw_text: str
    dollar_value: Optional[float] = None
    service_name: Optional[str] = None
    expiration_date: Optional[str] = None
    cta_url: Optional[str] = None
    extraction_mechanism: str = "declarative_proximity"  # gbp_offer_post|offer_schema|declarative_proximity|ocr_extraction
    multimodal_risk: bool = False
    grounding_status: str = "ungrounded"  # grounded | ungrounded | partial
    evidence_grade: str = "hypothesis"
    notes: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9$]+", " ", (s or "").lower()).strip()


class OfferExtractor:
    def extract(self, synthesized_fields: Optional[dict], visible_text: Optional[str] = None,
                outer_html: Optional[str] = None) -> list[OfferRecord]:
        visible = visible_text or (TAG_RE.sub(" ", outer_html) if outer_html else "")
        schema_offers = self._schema_offers(outer_html or "")
        image_texts = self._image_texts(outer_html or "")
        records: list[OfferRecord] = []
        for label, text in (synthesized_fields or {}).items():
            if not text:
                continue
            is_offer_label = any(label.lower() == l.lower() for l in OFFER_LABELS)
            dollar = self._detect_dollar_value(text)
            if not is_offer_label and dollar is None:
                continue
            if not is_offer_label and not re.search(r"\b(off|save|discount|special|coupon|deal|free)\b", text, re.I):
                continue  # plain pricing ("$24.99/month") is a pricing token, not an offer line
            records.append(self._build(text, label, visible, schema_offers, image_texts))
        return records

    # ---------------------------------------------------------------- building
    def _build(self, text: str, label: str, visible: str, schema_offers: list[dict],
               image_texts: list[str]) -> OfferRecord:
        dollar = self._detect_dollar_value(text)
        rec = OfferRecord(raw_text=text, dollar_value=dollar, service_name=self._detect_service(text),
                          expiration_date=self._detect_expiration(text))
        schema = next((o for o in schema_offers if self._offer_matches(text, dollar, o.get("name", "")
                                                                         + " " + str(o.get("price", "")))), None)
        in_visible = self._in_text(text, dollar, rec.service_name, visible)
        rec.multimodal_risk = self._flag_multimodal_risk(text, visible, image_texts)
        if schema:
            rec.extraction_mechanism = "offer_schema"
            rec.cta_url = schema.get("url")
            rec.expiration_date = rec.expiration_date or schema.get("priceValidUntil")
        elif rec.multimodal_risk:
            rec.extraction_mechanism = "image_text_hypothesis"
        elif label.lower() == "current offer":
            rec.extraction_mechanism = "gbp_offer_post_hypothesis"
        else:
            rec.extraction_mechanism = "declarative_proximity"
        layers = sum([bool(schema), in_visible, any(self._offer_matches(text, dollar, t) for t in image_texts)])
        rec.grounding_status = "observed_in_aio" if in_visible else "unverified"
        notes = []
        if rec.multimodal_risk:
            notes.append("Offer found only in the image path (alt/filename), not visible HTML text: exposed to OCR "
                         "interpolation and temporal drift (e.g. 'valid through <month>' with an invented year).")
        if rec.expiration_date and not re.search(r"\d{4}", rec.expiration_date):
            notes.append("Expiration lacks a year — temporal-drift risk.")
        if rec.extraction_mechanism == "gbp_offer_post_hypothesis":
            notes.append("Most plausible source: a GBP 'Offer' post (title + end date + CTA URL) — not confirmed "
                         "without the GBP post export.")
        rec.notes = " ".join(notes) or None
        return rec

    # ---------------------------------------------------------------- detectors
    @staticmethod
    def _detect_dollar_value(text: str) -> Optional[float]:
        m = DOLLAR_RE.search(text or "")
        return float(m.group(1).replace(",", "")) if m else None

    @staticmethod
    def _detect_expiration(text: str) -> Optional[str]:
        m = EXPIRY_RE.search(text or "")
        return m.group(1).strip() if m else None

    @staticmethod
    def _detect_service(text: str) -> Optional[str]:
        for rx in (SERVICE_RE, SERVICE_AFTER_PRICE_RE):
            m = rx.search(text or "")
            if m:
                name = re.sub(r"\s+", " ", m.group(1)).strip(" -")
                name = re.sub(r"^(?:the|a|an|any|your)\s+", "", name, flags=re.I)
                if name and not DOLLAR_RE.match(name):
                    return name
        return None

    def _flag_multimodal_risk(self, text: str, visible: str, image_texts: list[str]) -> bool:
        dollar = self._detect_dollar_value(text)
        in_image = any(self._offer_matches(text, dollar, t) for t in image_texts)
        return in_image and not self._in_text(text, dollar, self._detect_service(text), visible)

    # ---------------------------------------------------------------- helpers
    def _offer_matches(self, offer_text: str, dollar: Optional[float], candidate: str) -> bool:
        cand = _norm(candidate)
        if not cand:
            return False
        if dollar is not None and self._detect_dollar_value(candidate) == dollar:
            return True
        svc = self._detect_service(offer_text)
        return bool(svc and len(svc) > 4 and _norm(svc) in cand)

    def _in_text(self, offer_text: str, dollar: Optional[float], service: Optional[str], visible: str) -> bool:
        if not visible:
            return False
        vis = _norm(visible)
        if _norm(offer_text) in vis:
            return True
        price_ok = dollar is None or any(self._detect_dollar_value(m.group(0)) == dollar
                                         for m in DOLLAR_RE.finditer(visible))
        svc_ok = not service or _norm(service) in vis
        return bool(price_ok and svc_ok and (dollar is not None or service))

    @staticmethod
    def _schema_offers(outer_html: str) -> list[dict]:
        found: list[dict] = []

        def walk(node):
            if isinstance(node, dict):
                t = node.get("@type")
                if (t == "Offer" or (isinstance(t, list) and "Offer" in t)) or "priceValidUntil" in node:
                    found.append(node)
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        for block in JSONLD_RE.findall(outer_html):
            try:
                walk(json.loads(block.strip()))
            except (ValueError, TypeError):
                continue
        return found

    @staticmethod
    def _image_texts(outer_html: str) -> list[str]:
        out = []
        for tag in IMG_RE.findall(outer_html):
            alt = ALT_RE.search(tag)
            src = SRC_RE.search(tag)
            text = " ".join(filter(None, [alt.group(1) if alt else "",
                                          (src.group(1).rsplit("/", 1)[-1].replace("-", " ").replace("_", " ")
                                           if src and not src.group(1).startswith("data:") else "")]))
            if text.strip():
                out.append(text)
        return out
