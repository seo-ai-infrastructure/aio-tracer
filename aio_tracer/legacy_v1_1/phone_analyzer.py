"""Phone anomaly analysis: sentence/card phone vs. Call-button ``tel:`` payload vs. website phone.

Facts (which numbers differ) are ``dom_confirmed``; causes (tracking number, directory number) are
``hypothesis`` until checked in the GBP dashboard / on the site. Course cases:

* Roto-Rooter Lakeland — website 863-646-4370; desktop AIO sentence used (863) 940-4270, mobile used
  646-4370. Which GBP field holds 940-4270 is NOT established (do not call it the Business Profile number).
* 1-Tom-Plumber — website 863-216-2171; Call payload ``tel:+18632251411``. The 225 exchange is consistent
  with a directory/call-routing number (hypothesis).

Standalone: must not import ``attribution_engine``.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Optional

CLASSIFICATIONS = ("consistent", "tracking_number_in_payload", "directory_number", "website_mismatch",
                   "payload_only", "unknown")
# Exchanges observed on directory / call-routing numbers in course captures (1-Tom-Plumber: 863-225-xxxx).
DIRECTORY_EXCHANGES = {"225"}


@dataclass
class PhoneAnomalyReport:
    business_name: str
    sentence_phone: Optional[str]
    payload_tel: Optional[str]
    website_phone: Optional[str]
    classification: str
    evidence_grade: str
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class PhoneAnalyzer:
    @staticmethod
    def normalize_phone(value: Optional[str]) -> Optional[str]:
        """Strip to the 10-digit NANP number (drops ``tel:``, ``+1``, punctuation). None if not 10 digits."""
        d = re.sub(r"\D", "", value or "")
        if len(d) == 11 and d.startswith("1"):
            d = d[1:]
        return d if len(d) == 10 else None

    def analyze(self, business_name: str, sentence_phone: Optional[str], payload_tel: Optional[str],
                website_phone: Optional[str] = None) -> PhoneAnomalyReport:
        s, p, w = (self.normalize_phone(x) for x in (sentence_phone, payload_tel, website_phone))
        classification, grade, notes = self._classify(s, p, w)
        return PhoneAnomalyReport(business_name, sentence_phone, payload_tel, website_phone,
                                  classification, grade, notes)

    @staticmethod
    def _fmt(d: Optional[str]) -> str:
        return f"{d[:3]}-{d[3:6]}-{d[6:]}" if d else "-"

    def _classify(self, s: Optional[str], p: Optional[str], w: Optional[str]) -> tuple[str, str, list[str]]:
        notes: list[str] = []
        directory = [d for d in (p, s) if d and d[3:6] in DIRECTORY_EXCHANGES]
        for d in dict.fromkeys(directory):
            notes.append(f"{self._fmt(d)} uses a {d[3:6]} exchange — possible directory/call-routing number "
                         "(hypothesis; 1-Tom-Plumber pattern: payload 863-225-1411 vs website 863-216-2171).")

        if s and p and s == p:
            if w and w != s:
                notes.append(f"Sentence and payload agree ({self._fmt(s)}) but the website shows {self._fmt(w)} "
                             "— NAP inconsistency between GBP and site (cause: hypothesis).")
                return "website_mismatch", "dom_confirmed_fact/hypothesis_cause", notes
            notes.append("Sentence phone equals the Call-button tel: from the entity payload.")
            return "consistent", "dom_confirmed", notes

        if s and p:  # both present, different
            notes.append(f"Sentence {self._fmt(s)} ≠ payload tel {self._fmt(p)} (dom_confirmed fact).")
            if w and w not in (s, p):
                notes.append(f"Website {self._fmt(w)} differs from both — check which GBP field holds each number.")
                return "website_mismatch", "dom_confirmed_fact/hypothesis_cause", notes
            if p in directory:
                return "directory_number", "dom_confirmed_fact/hypothesis_cause", notes
            if w and w == s:
                notes.append("Website matches the sentence; the payload tel: may be a tracking number in a GBP "
                             "phone field (hypothesis — confirm primary vs additional phone in GBP).")
                return "tracking_number_in_payload", "dom_confirmed_fact/hypothesis_cause", notes
            if w and w == p:
                notes.append("Website matches the payload; the sentence number came from another source "
                             "(check other URLs/directories; cause is a hypothesis).")
                return "website_mismatch", "dom_confirmed_fact/hypothesis_cause", notes
            notes.append("No website phone supplied — cause cannot be narrowed (tracking number or NAP drift).")
            return "tracking_number_in_payload", "dom_confirmed_fact/hypothesis_cause", notes

        if p and not s:
            if w and w != p:
                notes.append(f"Payload tel {self._fmt(p)} ≠ website {self._fmt(w)}; no phone in the sentence.")
            if p in directory:
                return "directory_number", "dom_confirmed_fact/hypothesis_cause", notes
            notes.append("Phone exists only in the Call-button payload (not in the visible sentence/card).")
            return "payload_only", "dom_confirmed", notes

        if s and not p:
            if w and w != s:
                notes.append(f"Sentence {self._fmt(s)} ≠ website {self._fmt(w)}; no payload tel to compare.")
                return "website_mismatch", "dom_confirmed_fact/hypothesis_cause", notes
            notes.append("No Call-button payload tel: — sentence phone cannot be tied to the entity record.")
            return "unknown", "dom_confirmed", notes

        notes.append("No comparable phone numbers.")
        return "unknown", "dom_confirmed", notes
