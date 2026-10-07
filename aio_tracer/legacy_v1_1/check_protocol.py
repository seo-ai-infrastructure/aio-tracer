"""Six-point repeatable check protocol (apex_aio_module Part 12) run against one captured trace.

1. Query, location chip, device            ("near me" writes the location chip; a city in the query overrides it)
2. The sentence — underlined vs highlighted (name button ≠ phone pill; screenshot circles ≠ Google highlight)
3. Chip label and href, after it is opened ("+1" is unidentified until the full source list is opened)
4. GBP listing name/phone/hours/address    (compare to the sentence before assuming the website was the source)
5. Same strings on the business's own page (a second URL on another domain can hold the number)
6. The image record's page URL from the DOM (never a guess from the picture)

Each point carries ``status`` (pass | flag | manual | missing), ``observed`` values, an evidence grade and a
note. Points that need data outside the DOM (Maps listing, the website) are ``manual`` unless supplied.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Optional
from urllib.parse import parse_qs, urlsplit

from entity_payloads import extract_image_layer
from phone_analyzer import PhoneAnalyzer
from signal_extractor import SignalExtractor, _norm


@dataclass
class CheckProtocolResult:
    query_device_location: dict
    underlined_vs_highlighted: dict
    chip_label_href: dict
    gbp_nap: dict
    site_strings_match: dict
    image_url_from_dom: dict
    overall_confidence: str  # high | medium | low
    business_name: Optional[str] = None
    references: list[str] = field(default_factory=lambda: ["finding:check_protocol", "finding:volatility_5_captures",
                                                            "US11769017B1#linkifying_engine"])

    def to_dict(self) -> dict:
        return asdict(self)


def _point(status: str, observed: dict, grade: str, note: str) -> dict:
    return {"status": status, "observed": observed, "evidence_grade": grade, "note": note}


class CheckProtocol:
    def __init__(self, extractor: Optional[SignalExtractor] = None):
        self.extractor = extractor or SignalExtractor()
        self.phones = PhoneAnalyzer()

    def run(self, trace: dict, business_name: str, supplied_website_phone: Optional[str] = None) -> CheckProtocolResult:
        extraction = self.extractor.extract(trace)
        sig = next((s for s in extraction.signals if _norm(s.name) == _norm(business_name)), None)
        outer = (trace.get("root") or {}).get("outerHTML") or ""
        points = [
            self._p1_query(trace),
            self._p2_sentence(sig, business_name),
            self._p3_chips(extraction),
            self._p4_nap(sig, business_name),
            self._p5_site(sig, business_name, supplied_website_phone),
            self._p6_image(outer),
        ]
        passed = sum(p["status"] == "pass" for p in points)
        flagged = sum(p["status"] in ("flag", "missing") for p in points)
        overall = "high" if passed >= 5 else ("medium" if passed >= 3 and flagged <= 1 else "low")
        return CheckProtocolResult(*points, overall_confidence=overall, business_name=business_name)

    # ------------------------------------------------------------------ points
    @staticmethod
    def _p1_query(trace: dict) -> dict:
        url = trace.get("pageURL") or ""
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        query = trace.get("query") or q.get("q")
        ua = trace.get("userAgent") or ""
        device = ("mobile" if re.search(r"Mobile|Android|iPhone", ua) else "desktop") if ua else None
        cap = trace.get("capture") or {}
        location = (cap.get("profile") or {}).get("city") if isinstance(cap.get("profile"), dict) else None
        location = location or cap.get("city") or q.get("near") or ("uule" in q and "uule param") or None
        near_me = bool(query and re.search(r"\bnear me\b", query, re.I))
        obs = {"query": query, "device": device, "location_chip": location, "timezone": trace.get("timezone"),
               "query_has_near_me": near_me}
        ok = bool(query and device)
        note = ("'near me' — the location chip (IP/profile) decides the market." if near_me else
                "A city in the query overrides the location chip.")
        if not location:
            note += " Location chip not recorded in the trace — log it manually."
        note += " Desktop and mobile runs of the same query were not the same answer."
        return _point("pass" if ok and location else ("flag" if ok else "missing"), obs, "dom_confirmed", note)

    @staticmethod
    def _p2_sentence(sig, name: str) -> dict:
        if sig is None:
            return _point("missing", {"business": name}, "dom_confirmed", "Business not found in this capture.")
        obs = {"binding_status": getattr(sig, "binding_status", "unknown"), "phone_in_text": sig.phone,
               "details_text": sig.details_text}
        note = ("Name button (entity binding) and phone pill are different things; only DOM role=button counts as "
                "underlined. Circles drawn on a screenshot are not Google's highlight, and a highlight does not mean "
                "one page verified the whole sentence (US11900068B1: portions verified by different documents).")
        status = "pass" if obs["binding_status"] == "entity_linked" else (
            "flag" if obs["binding_status"] == "plain_text" else "manual")
        return _point(status, obs, "dom_confirmed", note)

    @staticmethod
    def _p3_chips(extraction) -> dict:
        chips = [{"label": c.get("label"), "href": c.get("url"), "feature_type": c.get("feature_type")}
                 for c in extraction.source_chips]
        unresolved = [r for r in extraction.related_results if r.get("more")]
        obs = {"chips": chips, "unopened_plus_n": unresolved}
        note = ("The label can be the domain or the word 'Google'; '+N' sources stay unidentified until the full "
                "source list is opened (e.g. Roto-Rooter '+1' resolved to a Google searchviewer → 1-Tom record).")
        if not chips:
            return _point("missing", obs, "dom_confirmed", note)
        return _point("flag" if unresolved else "pass", obs, "dom_confirmed", note)

    @staticmethod
    def _p4_nap(sig, name: str) -> dict:
        if sig is None:
            return _point("missing", {"business": name}, "dom_confirmed", "Business not found in this capture.")
        p = sig.provenance or {}
        obs = {"card": {"name": sig.name, "phone": sig.phone, "hours": sig.hours, "address": sig.address},
               "entity_payload": {"name": p.get("name"), "tel_uri": p.get("tel_uri"),
                                  "directions_address": p.get("directions_address"), "kg_id": p.get("kg_id"),
                                  "maps_cid_url": p.get("maps_cid_url")}}
        if not p:
            return _point("missing", obs, "dom_confirmed", "No entity payload joined; open the listing manually.")
        tel_ok = not (sig.phone and p.get("tel_uri")) or \
            PhoneAnalyzer.normalize_phone(sig.phone) == PhoneAnalyzer.normalize_phone(p.get("tel_uri"))
        note = (f"Open {p.get('maps_cid_url') or 'the Maps listing'} and compare its name/phone/hours/address with "
                "the sentence before assuming the website was the source.")
        return _point("pass" if tel_ok else "flag", obs, "dom_confirmed", note if tel_ok else
                      "Card phone ≠ Call-button tel: (fact). " + note)

    def _p5_site(self, sig, name: str, website_phone: Optional[str]) -> dict:
        if not website_phone:
            return _point("manual", {"website_url": getattr(sig, "website_url", None)}, "dom_confirmed",
                          "Supply the website phone (--phone-audit/--website-phone). Check more than the chip URL — "
                          "a second URL on another domain can hold the number (Ben Franklin 797-4507).")
        rep = self.phones.analyze(name, getattr(sig, "phone", None),
                                  (getattr(sig, "provenance", None) or {}).get("tel_uri"), website_phone)
        status = "pass" if rep.classification == "consistent" else "flag"
        return _point(status, rep.to_dict(), rep.evidence_grade, " ".join(rep.notes))

    @staticmethod
    def _p6_image(outer: str) -> dict:
        imgs = [i for i in extract_image_layer(outer) if "googleusercontent.com" not in i["host_url"]]
        thumbs = [i for i in extract_image_layer(outer) if "googleusercontent.com" in i["host_url"]]
        obs = {"image_slot_records": imgs[:10], "entity_thumbnails": len(thumbs)}
        note = ("Log the image record's page URL from the DOM, not a guess from the picture. The image slot is "
                "resolved separately from the entity layer; a geographic mismatch in one capture is a hypothesis "
                "to re-test, not a rule.")
        return _point("pass" if imgs else "missing", obs, "dom_confirmed", note)
