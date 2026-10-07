"""Heuristic source-layer attribution for AIO-synthesised card text (e.g. ``Details:``).

Model
-----
Google's "Generative summaries for search results" patents (US11769017B1 / US11900068B1) describe
a *response linkifying engine* that splits a generated summary into portions, finds a candidate
document for each portion, and verifies the portion against that document (embedding distance).
This engine mirrors that structure offline: it splits ``details_text`` into portions (semantic
units -> tokens) and, for each token, names the **most likely candidate source document layer**:

* ``GBP_ENTITY``   – the entity record itself: listing name, primary category, hours field.
* ``GBP_REVIEWS``  – customer-review language: response windows, time-of-day, holidays, sentiment.
* ``WEBSITE_COPY`` – site marketing copy: credentials, warranties, pricing process, price tokens.
* ``GBP_SERVICES`` – the GBP services menu: concrete job types (main line, sump pump, septic...).

It is keyword/regex only (no network, no models). Each token therefore carries a *heuristic*
grade; the per-business ``evidence`` block, by contrast, compares DOM-observed values (card text
vs. entity payload) and is graded ``dom_confirmed``. Every token and check cites the patent passage
or course finding it relies on (see :mod:`patent_references`).
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Iterable, Optional
from urllib.parse import parse_qs, urlsplit

from entity_payloads import digits
from offer_extractor import OfferExtractor
from patent_references import cite
from phone_analyzer import PhoneAnalyzer

logger = logging.getLogger(__name__)

GBP_ENTITY, GBP_REVIEWS, WEBSITE_COPY, GBP_SERVICES = "GBP_ENTITY", "GBP_REVIEWS", "WEBSITE_COPY", "GBP_SERVICES"
UNATTRIBUTED = "UNATTRIBUTED"
LAYERS = (GBP_ENTITY, GBP_REVIEWS, WEBSITE_COPY, GBP_SERVICES)

# Every token is a "portion" whose candidate document is a source layer (patent method 300).
BASE_REFS = ["US11769017B1#linkify_method_300", "US11769017B1#candidate_doc_358", "US11900068B1#claim_1_verifiable_portions"]

LAYER_REFS = {
    GBP_ENTITY: ["finding:tel_from_entity_record", "finding:listing_name_underlined"],
    GBP_REVIEWS: [],
    WEBSITE_COPY: ["finding:pricing_from_blog"],
    GBP_SERVICES: ["finding:temporal_from_service_menu"],
}

LAYER_DOCUMENT = {
    GBP_ENTITY: "GBP entity record (name / primary category / hours)",
    GBP_REVIEWS: "GBP customer reviews",
    WEBSITE_COPY: "business website / blog copy",
    GBP_SERVICES: "GBP services menu / service descriptions",
}


@dataclass(frozen=True)
class Rule:
    layer: str
    name: str
    pattern: re.Pattern
    weight: float
    refs: tuple = ()


def _r(layer: str, name: str, pattern: str, weight: float, *refs: str) -> Rule:
    return Rule(layer, name, re.compile(pattern, re.I), weight, tuple(refs))


_SERVICE_TERMS = [
    r"main[- ]line(?:\s+(?:busts?|breaks?|repairs?|clogs?|backups?))?", r"sewer(?:\s+line)?(?:\s+(?:repairs?|replacement|backups?))?",
    r"sump[- ]pumps?(?:\s+(?:repairs?|installation|replacement))?", r"water[- ]heaters?(?:\s+(?:failures?|repairs?|installation|replacement))?",
    r"tankless(?:\s+water\s+heaters?)?", r"septic(?:\s+(?:issues?|tanks?|pumping|systems?|repairs?))?",
    r"drain(?:\s+(?:cleaning|clearing|repairs?))", r"drains?", r"burst(?:ing)?\s+pipes?", r"pipe\s+(?:fixes|repairs?|issues?|replacement|bursts?)",
    r"(?:slab\s+)?leaks?(?:\s+(?:detection|repairs?))?", r"(?:severe\s+)?backups?", r"clogs?", r"hydro[- ]?jetting", r"re-?piping",
    r"gas\s+lines?", r"toilets?(?:\s+repairs?)?", r"faucets?", r"garbage\s+disposals?", r"(?:pipe\s+)?camera\s+inspections?",
    r"water\s+softeners?", r"backflow(?:\s+(?:testing|prevention))?", r"(?:roof|shingle)\s+repairs?", r"tarping",
]

STATIC_RULES: list[Rule] = [
    # ---- GBP_REVIEWS: temporal / experiential language typical of review text
    _r(GBP_REVIEWS, "response_window", r"\b\d{1,3}\s*(?:[–—-]|to)\s*\d{1,3}\s*(?:minutes?|mins?|hours?|hrs?)\b", 0.85),
    _r(GBP_REVIEWS, "within_time", r"\bwithin\s+(?:an?\s+|\d+\s*)?(?:minutes?|hours?|hour)\b", 0.75),
    _r(GBP_REVIEWS, "response_speed", r"\b(?:rapid|fast|quick|prompt|speedy)\s+(?:response|arrival|service)(?:\s+times?)?\b", 0.75),
    _r(GBP_REVIEWS, "arrival_language", r"\b(?:frequently\s+)?arriv(?:ing|ed|es|al)\b|\bshowed up\b|\bsame[- ]day\b|\bon time\b", 0.65),
    _r(GBP_REVIEWS, "time_of_day", r"\blate[- ]night\b|\bovernight\b|\bmidnight\b|\bafter[- ]hours\b|\bearly[- ]morning\b|\bweekends?\b", 0.75),
    _r(GBP_REVIEWS, "around_the_clock", r"\baround[- ]the[- ]clock\b", 0.7, "finding:temporal_from_service_menu"),
    _r(GBP_REVIEWS, "holiday", r"\b(?:holidays?|christmas|thanksgiving|new year'?s?(?:\s+eve)?|easter)\b", 0.7),
    _r(GBP_REVIEWS, "urgency", r"\burgent\b", 0.6),
    _r(GBP_REVIEWS, "sentiment", r"\b(?:friendly|courteous|honest|reliable|knowledgeable|responsive|polite|trustworthy)\b", 0.55),
    _r(GBP_REVIEWS, "reputation_cue", r"\bknown for\b|\bpraised for\b|\bcustomers (?:say|praise|mention)\b", 0.55),
    # ---- WEBSITE_COPY: credentials, guarantees, pricing process, price tokens
    _r(WEBSITE_COPY, "credentials_pair", r"\blicensed(?:,)?\s+(?:bonded,?\s+)?(?:and|&)\s+insured\b", 0.85),
    _r(WEBSITE_COPY, "credential", r"\b(?:licensed|insured|bonded|certified|accredited|background[- ]checked)\b", 0.65),
    _r(WEBSITE_COPY, "warranty", r"\b(?:warrant(?:y|ies)|guarantee[ds]?|satisfaction guaranteed)\b", 0.8),
    _r(WEBSITE_COPY, "pricing_process", r"\b(?:upfront|up-front|free|written|transparent|flat[- ]rate|no[- ]obligation)\s+(?:estimates?|pricing(?:\s+models?)?|prices?|quotes?|rates?)\b", 0.85),
    _r(WEBSITE_COPY, "price_token", r"\$\s?\d[\d,]*(?:\.\d{2})?(?:\s*/\s*(?:mo(?:nth)?|yr|year|hour|hr))?", 0.75),
    _r(WEBSITE_COPY, "membership_or_fees", r"\bmembership(?:\s+(?:plans?|perks?))?\b|\bdispatch\s+fees?\b|\bfinancing\b|\bdiscounts?\b", 0.7),
    _r(WEBSITE_COPY, "offer_line", r"\b\d+%\s+off\b|\$\d+\s+off\b|\bvalid (?:through|until)\b", 0.6, "finding:offer_post_source"),
    _r(WEBSITE_COPY, "staff_claim", r"\b(?:professionals?|technicians?|experts?|specialists?)\b", 0.45),
    _r(WEBSITE_COPY, "ownership_tenure", r"\b(?:family|locally|veteran)[- ]owned\b|\bsince\s+\d{4}\b|\b\d+\+?\s+years\b", 0.7),
    _r(WEBSITE_COPY, "marketing_adjective", r"\bcomprehensive\b|\bfull[- ]service\b|\bstate[- ]of[- ]the[- ]art\b", 0.45),
    # ---- GBP_SERVICES: concrete job types
    _r(GBP_SERVICES, "service_type", r"\b(?:" + "|".join(_SERVICE_TERMS) + r")\b", 0.8),
    _r(GBP_SERVICES, "emergency_24_7", r"\b(?:24/7|24-hour)\s+emergency\b", 0.7, "finding:temporal_from_service_menu"),
    _r(GBP_REVIEWS, "always_available", r"\b24/7\b|\b24 hours\b", 0.5),
    _r(GBP_SERVICES, "emergency_service", r"\bemergency\b", 0.6, "finding:temporal_from_service_menu"),
    _r(GBP_SERVICES, "repairs_generic", r"\brepairs?\b|\bfixes\b|\binstallations?\b", 0.5),
    _r(GBP_SERVICES, "specializes_cue", r"\bspeciali[sz]es in\b|\bspeciali[sz]ing in\b", 0.5),
    # ---- GBP_ENTITY (static part; dynamic name/category/hours rules are built per business)
    _r(GBP_ENTITY, "hours_24", r"\b24/7\b|\b24 hours\b|\b24-hour\b|\bopen 24\b", 0.6, "finding:temporal_from_service_menu"),
]

BOUNDARY_RE = re.compile(r"\s*[,;()]\s*|\s+(?:and|with|for|during|including|plus|while)\s+", re.I)
STOPWORDS = set("""a an the and or of in on at to for with by from as is are be being been during
including plus while such their its it this that these those via into than then also very
offers offer provides provide available""".split())


@dataclass
class TokenAttribution:
    text: str
    start: int
    end: int
    unit_index: int
    layer: str
    rule: str
    confidence: float
    candidate_document: str
    alternatives: list[dict] = field(default_factory=list)
    corroborated_by: list[str] = field(default_factory=list)
    verification_step: str = ""
    grade: str = "heuristic"
    references: list[str] = field(default_factory=list)


@dataclass
class SemanticUnit:
    index: int
    text: str
    start: int
    end: int
    layer: str
    token_indices: list[int] = field(default_factory=list)


@dataclass
class EvidenceCheck:
    check: str
    status: str  # match | mismatch | missing | info
    observed: dict
    dom_sources: list[str]
    interpretation: str
    grade: str
    references: list[str] = field(default_factory=list)


@dataclass
class AttributionReport:
    business_name: str
    position: int
    details_text: Optional[str]
    units: list[SemanticUnit]
    tokens: list[TokenAttribution]
    layer_shares: dict
    coverage: float
    confidence: float
    source_summary: str
    evidence: list[EvidenceCheck]
    verification_plan: list[str]
    references: list[str]
    extra_fields: dict = field(default_factory=dict)

    def to_dict(self, expand_references: bool = False) -> dict:
        d = asdict(self)
        d["source_hypotheses"] = d.pop("tokens")
        d["rule_score"] = d.pop("confidence")
        d["rule_share"] = d.pop("layer_shares")
        d["matched_text_coverage"] = d.pop("coverage")
        d["score_policy"] = "Uncalibrated regex rule scores, not probabilities, model tokens, attention, or source provenance."
        for item in d["source_hypotheses"]:
            item["rule_score"] = item.pop("confidence")
            for alt in item.get("alternatives", []):
                alt["rule_score"] = alt.pop("confidence")
        for item in d["extra_fields"].values():
            item["source_hypotheses"] = item.pop("tokens")
            item["rule_score"] = item.pop("confidence")
            item["rule_share"] = item.pop("layer_shares")
            for hypothesis in item["source_hypotheses"]:
                hypothesis["rule_score"] = hypothesis.pop("confidence")
                for alt in hypothesis.get("alternatives", []):
                    alt["rule_score"] = alt.pop("confidence")
        if expand_references:
            d["references_expanded"] = {r: cite(r) for r in self.references}
        return d


def _ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


class AttributionEngine:
    """Classify AIO card text into source layers and run DOM consistency checks."""

    def __init__(self, extra_rules: Optional[Iterable[Rule]] = None):
        self.rules = list(STATIC_RULES) + list(extra_rules or [])

    # ------------------------------------------------------------------ public
    def attribute(self, signal) -> AttributionReport:
        text = signal.details_text or ""
        tokens, units, coverage = self._attribute_text(text, signal)
        shares = self._layer_shares(tokens)
        confidence = self._confidence(tokens, coverage)
        evidence = self.evidence_checks(signal)
        refs = sorted({r for t in tokens for r in t.references} | {r for e in evidence for r in e.references}
                      | {"US11769017B1#confidence_engine", "finding:independent_layers", "finding:volatility_5_captures"})
        extra = {}
        for label, value in (signal.synthesized_fields or {}).items():
            if label == "Details" or not value:
                continue
            t2, u2, c2 = self._attribute_text(value, signal)
            extra[label] = {"text": value, "tokens": [asdict(t) for t in t2], "units": [asdict(u) for u in u2],
                            "layer_shares": self._layer_shares(t2), "coverage": c2,
                            "confidence": self._confidence(t2, c2)}
            refs = sorted(set(refs) | {r for t in t2 for r in t.references})
        report = AttributionReport(
            business_name=signal.name, position=getattr(signal, "position", 0), details_text=signal.details_text,
            units=units, tokens=tokens, layer_shares=shares, coverage=coverage, confidence=confidence,
            source_summary=self._summary(signal.name, tokens, shares, coverage, confidence, evidence),
            evidence=evidence, verification_plan=self._verification_plan(signal, tokens, evidence),
            references=refs, extra_fields=extra,
        )
        logger.debug("Attributed %s: %s", signal.name, report.source_summary)
        return report

    def attribute_all(self, signals) -> list[AttributionReport]:
        return [self.attribute(s) for s in signals]

    # ------------------------------------------------------------- tokenizing
    def _dynamic_rules(self, signal) -> list[Rule]:
        rules: list[Rule] = []
        name_words = [w for w in re.findall(r"[A-Za-z][A-Za-z'&-]{2,}", signal.name or "")
                      if w.lower() not in STOPWORDS and w.lower() not in {"llc", "inc", "services", "service"}]
        if signal.name:
            rules.append(_r(GBP_ENTITY, "listing_name", re.escape(signal.name), 0.95, "finding:listing_name_underlined"))
        for w in name_words:
            rules.append(_r(GBP_ENTITY, "listing_name_word", rf"\b{re.escape(w)}\b", 0.55, "finding:listing_name_underlined"))
        if signal.category:
            stem = re.escape(signal.category.lower().rstrip("s"))
            rules.append(_r(GBP_ENTITY, "primary_category", rf"\b{stem}(?:s|ing)?\b", 0.7))
        return rules

    def _attribute_text(self, text: str, signal) -> tuple[list[TokenAttribution], list[SemanticUnit], float]:
        if not text.strip():
            return [], [], 0.0
        matches = []
        for rule in self.rules + self._dynamic_rules(signal):
            for m in rule.pattern.finditer(text):
                if m.end() > m.start():
                    score, corr = self._corroborate(rule, m.group(0), signal)
                    matches.append((score, m.end() - m.start(), m.start(), m.end(), rule, corr))
        # Greedy, highest-score then longest, non-overlapping selection.
        matches.sort(key=lambda x: (-x[0], -x[1], x[2]))
        selected = []
        for mt in matches:
            if all(mt[3] <= s[2] or mt[2] >= s[3] for s in selected):
                selected.append(mt)
        selected.sort(key=lambda x: x[2])

        units = self._segment(text, [(s[2], s[3]) for s in selected])
        tokens: list[TokenAttribution] = []
        for score, _, start, end, rule, corr in selected:
            alts = {}
            for o in matches:
                if o[4].layer != rule.layer and not (o[3] <= start or o[2] >= end):
                    if o[4].layer not in alts or o[0] > alts[o[4].layer]["confidence"]:
                        alts[o[4].layer] = {"layer": o[4].layer, "rule": o[4].name, "confidence": round(o[0], 2),
                                            "text": text[o[2]:o[3]]}
            unit_idx = next((u.index for u in units if u.start <= start < u.end), 0)
            tok_text = text[start:end]
            tokens.append(TokenAttribution(
                text=tok_text, start=start, end=end, unit_index=unit_idx, layer=rule.layer, rule=rule.name,
                confidence=round(score, 2), candidate_document=LAYER_DOCUMENT[rule.layer],
                alternatives=sorted(alts.values(), key=lambda a: -a["confidence"]), corroborated_by=corr,
                verification_step=self._verification_step(rule.layer, tok_text, signal),
                references=list(dict.fromkeys(BASE_REFS + list(rule.refs) + LAYER_REFS[rule.layer])),
            ))
        for i, t in enumerate(tokens):
            units[t.unit_index].token_indices.append(i)
        for u in units:
            layer_scores: dict[str, float] = {}
            for i in u.token_indices:
                layer_scores[tokens[i].layer] = layer_scores.get(tokens[i].layer, 0) + tokens[i].confidence
            u.layer = max(layer_scores, key=layer_scores.get) if layer_scores else UNATTRIBUTED
        return tokens, units, self._coverage(text, selected)

    @staticmethod
    def _corroborate(rule: Rule, matched: str, signal) -> tuple[float, list[str]]:
        """Boost a rule when the matched phrase agrees with a structured field on the same card."""
        score, corr = rule.weight, []
        low = matched.lower()
        if rule.layer == GBP_ENTITY and rule.name == "hours_24":
            hours = signal.hours or signal.open_status or ""
            if re.search(r"24", hours):
                score += 0.15
                corr.append(f"card hours field = {hours.strip()!r}")
        if rule.layer == GBP_REVIEWS and rule.name == "around_the_clock" and re.search(r"24", signal.hours or ""):
            corr.append(f"consistent with card hours field {signal.hours!r} (does not change layer)")
        if rule.layer == GBP_ENTITY and rule.name == "primary_category" and signal.category:
            corr.append(f"card category = {signal.category!r}")
        if rule.layer == GBP_SERVICES and signal.category and low in (signal.category or "").lower():
            score -= 0.2  # a category word is better explained by the entity record
        return min(round(score, 2), 0.99), corr

    @staticmethod
    def _segment(text: str, protected: list[tuple[int, int]]) -> list[SemanticUnit]:
        cuts = [0]
        for m in BOUNDARY_RE.finditer(text):
            if any(s < m.end() and m.start() < e for s, e in protected):
                continue  # never split inside an attributed phrase ("licensed and insured")
            cuts.extend([m.start(), m.end()])
        cuts.append(len(text))
        units, idx = [], 0
        for a, b in zip(cuts[0::2], cuts[1::2]):
            seg = text[a:b]
            lead_m = re.match(r"^[\s.]*(?:(?:and|or|with|for|during|plus)\s+)?", seg, re.I)
            lead = lead_m.end() if lead_m else 0
            stripped = seg[lead:].rstrip(" .")
            if not stripped:
                continue
            units.append(SemanticUnit(idx, stripped, a + lead, a + lead + len(stripped), UNATTRIBUTED))
            idx += 1
        return units or [SemanticUnit(0, text.strip(), 0, len(text), UNATTRIBUTED)]

    @staticmethod
    def _coverage(text: str, selected) -> float:
        words = [(m.start(), m.end()) for m in re.finditer(r"[A-Za-z0-9$/–-]+", text)
                 if m.group(0).lower() not in STOPWORDS and len(m.group(0)) > 1]
        if not words:
            return 0.0
        covered = sum(1 for a, b in words if any(s[2] <= a and b <= s[3] for s in selected))
        return round(covered / len(words), 2)

    @staticmethod
    def _layer_shares(tokens: list[TokenAttribution]) -> dict:
        total = sum(t.confidence for t in tokens)
        shares = {layer: 0.0 for layer in LAYERS}
        for t in tokens:
            shares[t.layer] += t.confidence / total if total else 0
        return {k: round(v, 2) for k, v in shares.items()}

    @staticmethod
    def _confidence(tokens: list[TokenAttribution], coverage: float) -> float:
        if not tokens:
            return 0.0
        mean = sum(t.confidence for t in tokens) / len(tokens)
        return round(mean * (0.5 + 0.5 * coverage), 2)

    # --------------------------------------------------------------- evidence
    def evidence_checks(self, signal) -> list[EvidenceCheck]:
        p = signal.provenance or {}
        checks: list[EvidenceCheck] = []
        idx = p.get("payload_indices") or []
        payload_src = f"root.outerHTML TgQPHd payloads {idx}" if idx else "root.outerHTML TgQPHd payloads"
        card_src = f"root.visibleText card #{getattr(signal, 'position', '?')}"

        if not p:
            checks.append(EvidenceCheck("entity_payload_present", "missing", {}, [payload_src],
                                        "No entity payload joined to this card; provenance cannot be traced.",
                                        "dom_confirmed", ["finding:independent_layers"]))
            return checks

        checks.append(EvidenceCheck(
            "entity_identity", "info" if p.get("kg_id") else "missing",
            {"kg_id": p.get("kg_id"), "feature_id": p.get("feature_id"), "cid": p.get("cid"),
             "lat": p.get("lat"), "lon": p.get("lon"), "kgmid_url": p.get("kgmid_url"), "maps_cid_url": p.get("maps_cid_url")},
            [payload_src + " (ENTITY)"],
            "Entity layer resolved to this Knowledge Graph record; use kgmid/cid URLs to open the exact GBP.",
            "dom_confirmed", ["finding:independent_layers", "finding:tel_from_entity_record"]))

        tel = p.get("tel_uri")
        card_phone = signal.phone
        if tel or card_phone:
            same = bool(tel and card_phone and digits(tel) == digits(card_phone))
            status = "match" if same else ("missing" if not (tel and card_phone) else "mismatch")
            interp = {
                "match": "Card phone equals the Call-button tel: baked into the entity payload (GBP primary phone).",
                "mismatch": ("Card text phone differs from the entity-payload tel:. Possible NAP inconsistency or a "
                             "tracking number in one GBP phone field (hypothesis — confirm in the GBP dashboard)."),
                "missing": "Only one of card phone / payload tel: present; cannot compare.",
            }[status]
            refs = ["finding:tel_from_entity_record"] + (["finding:tracking_number_primary"] if status != "match" else [])
            checks.append(EvidenceCheck("phone_card_vs_payload_tel", status,
                                        {"card_phone": card_phone, "payload_tel": tel, "all_card_phones": signal.phones},
                                        [card_src + " 'Phone:' row", payload_src + " (ACTION_CALL)"], interp,
                                        "dom_confirmed" if status != "mismatch" else "dom_confirmed_fact/hypothesis_cause", refs))

        dir_addr = p.get("directions_address")
        if dir_addr or signal.address:
            na, nb = _norm_addr(signal.address), _norm_addr(dir_addr)
            status = "match" if na and na == nb else ("missing" if not (na and nb) else "mismatch")
            checks.append(EvidenceCheck("address_card_vs_directions", status,
                                        {"card_address": signal.address, "directions_address": dir_addr,
                                         "directions_feature_id": p.get("directions_feature_id")},
                                        [card_src + " 'Address:' row", payload_src + " (ACTION_DIRECTIONS)"],
                                        "Card address vs. the address encoded in the Directions deep link.",
                                        "dom_confirmed", ["finding:independent_layers"]))
            if p.get("directions_feature_id") and p.get("feature_id"):
                fid_ok = p["directions_feature_id"].lower() == p["feature_id"].lower()
                checks.append(EvidenceCheck("feature_id_entity_vs_directions", "match" if fid_ok else "mismatch",
                                            {"entity_feature_id": p["feature_id"], "directions_feature_id": p["directions_feature_id"]},
                                            [payload_src + " (ENTITY, ACTION_DIRECTIONS)"],
                                            "Directions link points at the same Maps feature as the entity payload."
                                            if fid_ok else "Directions link points at a different Maps feature.",
                                            "dom_confirmed", []))

        checks.append(EvidenceCheck(
            "name_card_vs_entity_payload", "match" if _ascii(signal.name or "").lower() == _ascii(p.get("name") or "").lower() else "mismatch",
            {"card_name": signal.name, "payload_name": p.get("name"), "matched_by": p.get("matched_by")},
            [card_src + " title line", payload_src + " (NAME_IMAGE LOCAL_PLACE)"],
            "The underlined/listed name is the GBP listing name from the entity record.",
            "dom_confirmed", ["finding:listing_name_underlined"]))

        site = p.get("website_url") or signal.website_url
        if site:
            q = {k.lower(): v[0] for k, v in parse_qs(urlsplit(site).query).items()}
            tagged = any("gbp" in (q.get(k) or "").lower() or "gmb" in (q.get(k) or "").lower()
                         for k in ("utm_source", "utm_campaign", "utm_medium"))
            checks.append(EvidenceCheck(
                "website_link_gbp_tagged", "info",
                {"website_url": site, "domain": urlsplit(site).netloc, "utm": {k: v for k, v in q.items() if k.startswith("utm_")},
                 "gbp_utm_tag": tagged},
                [payload_src + " (ACTION_WEBSITE)"],
                ("Website button URL carries a GBP/GMB UTM tag — a tagging observation, not proof of field origin. Candidate "
                 "WEBSITE_COPY document for verification.") if tagged else
                "Website button URL (from the entity record) — candidate WEBSITE_COPY document for verification.",
                "dom_confirmed", ["US11769017B1#candidate_doc_358"]))

        if p.get("photo_url"):
            checks.append(EvidenceCheck("entity_photo", "info", {"photo_url": p["photo_url"]},
                                        [payload_src + " (NAME_IMAGE)"],
                                        "Card thumbnail served from the entity record (independent of the image layer).",
                                        "dom_confirmed", ["finding:independent_layers"]))
        checks.extend(self.layer_checks(signal, payload_src, card_src))
        return checks

    def layer_checks(self, signal, payload_src: str = "root.outerHTML", card_src: str = "root.visibleText") -> list[EvidenceCheck]:
        """Entity binding, phone anomaly and offer linkification checks (independent linkification events)."""
        p = signal.provenance or {}
        checks: list[EvidenceCheck] = []
        binding = getattr(signal, "binding_status", "unknown")
        if binding != "unknown":
            plain = binding == "plain_text"
            checks.append(EvidenceCheck(
                "entity_binding_evidence", "mismatch" if plain else "info",
                {"binding_status": binding, "binding_evidence": getattr(signal, "binding_evidence", None),
                 "kg_id": p.get("kg_id")},
                ["root.outerHTML / interactive[] role=button"],
                ("Business name has no observed entity-specific control. The cause is unknown; this is not permanent evidence of resolver failure.") if plain else
                ("Named control has an entity-specific destination or payload; this proves displayed linking only.") if binding == "entity_linked" else
                "Named interactive control observed; entity resolution and visual underline remain unverified.",
                "dom_confirmed",
                ["finding:entity_binding_plain_text", "finding:independent_layers", "US11769017B1#linkifying_engine"]
                + (["finding:triangulation_rule"] if plain else [])))
        tel = p.get("tel_uri")
        if tel:
            rep = PhoneAnalyzer().analyze(signal.name, signal.phone, tel, getattr(signal, "website_phone", None))
            status = "match" if rep.classification == "consistent" else (
                "info" if rep.classification == "payload_only" else "mismatch")
            checks.append(EvidenceCheck(
                "phone_anomaly_evidence", status, rep.to_dict(),
                [card_src + " 'Phone:' row", payload_src + " (ACTION_CALL)"], " ".join(rep.notes), rep.evidence_grade,
                ["finding:tel_from_entity_record"] + (["finding:tracking_number_primary"] if status == "mismatch" else [])))
        offers = OfferExtractor().extract(signal.synthesized_fields or {}, None, None)
        if offers:
            checks.append(EvidenceCheck(
                "offer_linkification_evidence", "info", {"offers": [o.to_dict() for o in offers]},
                [card_src + " synthesized fields"],
                "Offer line(s) detected; candidate mechanisms: GBP Offer post, Offer schema (priceValidUntil), "
                "declarative visible HTML, or image OCR (multimodal risk / temporal drift). Verify on the GBP post "
                "and the cited page.", "hypothesis",
                ["finding:offer_post_source", "finding:offer_multimodal_risk", "US11769017B1#linkifying_engine"]))
        return checks

    # ------------------------------------------------------- summary / plans
    @staticmethod
    def _verification_step(layer: str, token: str, signal) -> str:
        p = signal.provenance or {}
        gbp = p.get("maps_cid_url") or p.get("kgmid_url") or "the GBP listing"
        if layer == GBP_SERVICES:
            return f"Open {gbp} → Services; look for '{token}'."
        if layer == GBP_REVIEWS:
            return f"Open {gbp} → Reviews; search for '{token}'."
        if layer == WEBSITE_COPY:
            site = p.get("website_url") or signal.website_url or "the business website"
            return f"Fetch {site} (and blog/service pages); search visible text for '{token}'."
        return f"Compare '{token}' with the GBP name/category/hours on {gbp}."

    @staticmethod
    def _summary(name, tokens, shares, coverage, confidence, evidence) -> str:
        if not tokens:
            return f"{name}: no Details text to attribute."
        parts = []
        for layer in sorted(LAYERS, key=lambda l: -shares[l]):
            if shares[layer] <= 0:
                continue
            words = ", ".join(dict.fromkeys(t.text for t in tokens if t.layer == layer))
            parts.append(f"{round(shares[layer] * 100)}% {layer} ({words})")
        flags = [e.check for e in evidence if e.status == "mismatch"]
        flag_txt = f" ⚠ DOM mismatch: {', '.join(flags)}." if flags else " DOM checks consistent."
        return (f"{name}: " + " · ".join(parts)
                + f" — coverage {round(coverage * 100)}%, rule score {confidence:.2f} [heuristic]." + flag_txt)

    @staticmethod
    def _verification_plan(signal, tokens, evidence) -> list[str]:
        steps = list(dict.fromkeys(t.verification_step for t in tokens))
        for e in evidence:
            if e.status == "mismatch" and e.check == "phone_card_vs_payload_tel":
                steps.insert(0, f"Phone mismatch: compare GBP primary vs additional phone with card "
                                f"{e.observed.get('card_phone')} and tel {e.observed.get('payload_tel')}; check the website number.")
        site = (signal.provenance or {}).get("website_url") or getattr(signal, "website_url", None)
        steps.append(f"triangulation_check: compare the GBP name {signal.name!r} with the <title>/H1 and LocalBusiness "
                     f"schema 'name' of {site or 'the cited page'}; align to one canonical string and add sameAs "
                     "(state license board > BBB > Yelp/HomeAdvisor) [hypothesis].")
        for e in evidence:
            if e.check == "entity_binding_evidence" and e.status == "mismatch":
                steps.insert(0, "Name is plain text: check for a duplicate GBP with the same name and run the "
                                "triangulation check; re-capture later (binding is not permanent).")
        steps.append("Repeat the capture ≥5 times (different times of day) before acting on any single result.")
        return steps


def _norm_addr(s: Optional[str]) -> str:
    s = _ascii(s or "").lower()
    s = re.sub(r"\b(united states|usa|us)$", "", s.strip())
    return re.sub(r"[^a-z0-9]+", " ", s).strip()
