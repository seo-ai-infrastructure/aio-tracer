import html
import json
import sys
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aio_tracer import load_trace_file  # noqa: E402
from attribution_engine import GBP_ENTITY, GBP_REVIEWS, GBP_SERVICES, WEBSITE_COPY, AttributionEngine  # noqa: E402
from patent_references import FINDINGS, PATENTS, cite  # noqa: E402
from profile_manager import ProfileManager, fingerprint_seed, redact_proxy  # noqa: E402
from signal_extractor import BusinessSignal, SignalExtractor  # noqa: E402

SAMPLE = ROOT / "samples" / "emergency_plumber_lakeland_trace.json"


@pytest.fixture(scope="module")
def extraction():
    return SignalExtractor().extract(load_trace_file(SAMPLE))


# ---------------------------------------------------------------- profiles
def test_seed_is_deterministic_and_in_range():
    a, b = fingerprint_seed("client_001", "Lakeland FL"), fingerprint_seed("client_001", "Lakeland FL")
    assert a == b and 10000 <= a < 100000
    assert fingerprint_seed("client_001", "Tampa FL") != a


def test_profile_crud(tmp_path):
    pm = ProfileManager(tmp_path)
    p = pm.get_or_create("client_001", "Lakeland FL", "http://user:secret@proxy.example:8000")
    assert Path(p.user_data_dir).is_dir()
    assert "secret" not in json.dumps(p.to_public_dict())
    assert pm.get_or_create("client_001", "Lakeland FL").fingerprint_seed == p.fingerprint_seed
    assert [x.profile_id for x in pm.list_profiles()] == [p.profile_id]
    assert pm.delete(p.profile_id) and pm.list_profiles() == []


def test_redact_proxy():
    assert "secret" not in redact_proxy("socks5://u:secret@h:1080")


# ---------------------------------------------------------------- extraction
def test_extracts_four_businesses_with_provenance(extraction):
    names = [s.name for s in extraction.signals]
    assert names == ["Stuart's Plumbing", "AP Drain Service LLC Plumbing And Septic",
                     "Benjamin Franklin Plumbing", "Roto-Rooter Plumbing & Drain Services"]
    s = extraction.signals[0]
    assert (s.rating, s.review_count, s.category, s.is_open) == (4.9, 1682, "Plumber", True)
    assert s.provenance["kg_id"] == "/g/1tl4dvfz"
    assert s.provenance["cid"] == "8997965405532322092"
    assert s.photo_url and "googleusercontent.com" in s.photo_url
    assert extraction.signals[3].provenance["cid"] == "6700087979359954248"
    assert not extraction.warnings


def test_no_aio_trace_degrades():
    r = SignalExtractor().extract({"root": {}, "query": "x"})
    assert r.signals == [] and r.warnings


# ---------------------------------------------------------------- attribution
def _sig(details, **kw):
    base = dict(name="Acme Plumbing", category="Plumber", hours="Open 24/7", details_text=details)
    base.update(kw)
    return BusinessSignal(**base)


@pytest.mark.parametrize("details,layer,token", [
    ("Specializes in main line busts and sump pump repair.", GBP_SERVICES, "main line busts"),
    ("Known for arriving within 20–30 minutes on late-night calls.", GBP_REVIEWS, "20–30 minutes"),
    ("Licensed and insured with upfront estimates.", WEBSITE_COPY, "Licensed and insured"),
    ("Acme Plumbing is open 24/7.", GBP_ENTITY, "Acme Plumbing"),
])
def test_token_layers(details, layer, token):
    rep = AttributionEngine().attribute(_sig(details))
    tok = next(t for t in rep.tokens if t.text == token)
    assert tok.layer == layer and tok.references and tok.grade == "heuristic"
    assert 0 < rep.confidence <= 1 and abs(sum(rep.layer_shares.values()) - 1) < 0.05


def test_empty_details():
    rep = AttributionEngine().attribute(_sig(None))
    assert rep.tokens == [] and rep.confidence == 0


def test_phone_mismatch_flagged(extraction):
    reports = AttributionEngine().attribute_all(extraction.signals)
    status = {r.business_name: next(e.status for e in r.evidence if e.check == "phone_card_vs_payload_tel")
              for r in reports}
    assert status["Benjamin Franklin Plumbing"] == "mismatch"
    assert status["Stuart's Plumbing"] == "match"
    assert all(cite(ref) for r in reports for ref in r.references)


def test_patent_registry():
    assert PATENTS["US11769017B1"].assignee == "Google LLC"
    assert "linkifying engine" in cite("US11769017B1#linkifying_engine")["passage"]
    assert {f.grade for f in FINDINGS.values()} <= {"confirmed", "hypothesis"}


# ---------------------------------------------------------------- JS (real Chromium)
def test_aiotrace_js_in_browser(tmp_path):
    sync_api = pytest.importorskip("playwright.sync_api")
    trace = load_trace_file(SAMPLE)
    fixture = tmp_path / "aio.html"
    fixture.write_text(f"<html><head><title>{html.escape(trace['query'])}</title></head><body>"
                       f"<div data-container-id='main-col'>{trace['root']['outerHTML']}</div></body></html>")
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True, executable_path=os.environ.get('AIO_TEST_CHROMIUM'))
        except sync_api.Error as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip('Install Chromium or set AIO_TEST_CHROMIUM to run browser tests.')
            raise
        try:
            page = browser.new_page()
            page.goto(fixture.as_uri())
            page.evaluate((ROOT / "js" / "aiotrace.js").read_text())
            cap = json.loads(page.evaluate(
                "() => { window.__AIO_ROOT = AIOTrace.findRoot();"
                " return JSON.stringify(AIOTrace.capture(__AIO_ROOT, 'expanded-aio'), null, 2); }"))
        finally:
            browser.close()
    assert cap["label"] == "expanded-aio"
    r = SignalExtractor().extract(cap)
    assert len(r.signals) == 4 and r.signals[2].provenance["tel_uri"] == "tel:+18633220012"
