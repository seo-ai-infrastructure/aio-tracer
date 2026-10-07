import html
import json
import sys
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
    try:
        with sync_api.sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(fixture.as_uri())
            page.evaluate((ROOT / "js" / "aiotrace.js").read_text())
            cap = json.loads(page.evaluate(
                "() => { window.__AIOTRACE_TEST__ = true; window.__AIO_ROOT = AIOTrace.findRoot();"
                " return JSON.stringify(AIOTrace.capture(__AIO_ROOT, 'expanded-aio'), null, 2); }"))
            browser.close()
    except Exception as exc:  # browser binary missing etc.
        pytest.skip(f"Chromium unavailable: {exc}")
    assert cap["label"] == "expanded-aio"
    r = SignalExtractor().extract(cap)
    assert len(r.signals) == 4 and r.signals[2].provenance["tel_uri"] == "tel:+18633220012"
    assert [c["business_name"] for c in cap["cardLayer"]] == [s.name for s in r.signals]
    assert isinstance(cap["imageLayer"], list) and isinstance(cap["sourceChipsExtended"], list)


# ---------------------------------------------------------------- v1.1 layers
from check_protocol import CheckProtocol, CheckProtocolResult  # noqa: E402
from entity_payloads import classify_chip_feature_type  # noqa: E402
from offer_extractor import OfferExtractor  # noqa: E402
from phone_analyzer import PhoneAnalyzer  # noqa: E402
from signal_extractor import classify_entity_binding  # noqa: E402


def test_source_chip_feature_type_classification(extraction):
    assert classify_chip_feature_type("https://maps.google.com/?cid=1") == "Google"
    assert classify_chip_feature_type("https://www.google.com/searchviewer/10?svid=x") == "Google"
    assert classify_chip_feature_type("https://www.facebook.com/groups/lakeland/posts/1") == "Facebook"
    assert classify_chip_feature_type("https://lakeland.craigslist.org/search/bbb") == "Craigslist"
    assert classify_chip_feature_type("https://www.thumbtack.com/fl/lakeland/plumbers") == "Thumbtack"
    assert classify_chip_feature_type("https://www.rotorooter.com/schedule-service/?zipCode=33805",
                                      '[["uuid",null,"MAGI_FEATURE"]]') == "MAGI_FEATURE"
    assert classify_chip_feature_type("https://www.rotorooter.com/lakeland/", "[]") == "other"
    chip = extraction.source_chips[0]
    assert chip["domain"] == "rotorooter.com" and "feature_type" in chip and "secondary_sources" in chip


def test_phone_anomaly_detection_consistent():
    pa = PhoneAnalyzer()
    assert pa.normalize_phone("tel:+1 (863) 940-4270") == "8639404270"
    rep = pa.analyze("Roto-Rooter", "(863) 940-4270", "tel:+18639404270")
    assert rep.classification == "consistent" and rep.evidence_grade == "dom_confirmed"
    assert pa.analyze("X", None, "tel:+18635551234").classification == "payload_only"


def test_phone_anomaly_detection_mismatch():
    pa = PhoneAnalyzer()
    rep = pa.analyze("Benjamin Franklin Plumbing", "863-797-4507", "tel:+18633220012")
    assert rep.classification in ("tracking_number_in_payload", "website_mismatch")
    assert "hypothesis" in rep.evidence_grade
    tom = pa.analyze("1-Tom-Plumber", "863-216-2171", "tel:+18632251411", website_phone="863-216-2171")
    assert tom.classification == "directory_number" and any("directory" in n for n in tom.notes)
    roto = pa.analyze("Roto-Rooter", "(863) 940-4270", "tel:+18639404270", website_phone="863-646-4370")
    assert roto.classification == "website_mismatch"
    assert not any("Business Profile number" in n for n in roto.notes)


def test_entity_binding_classifier(extraction):
    linked = '<div><span role="button" tabindex="0"><span>Plumbing Panda</span></span> is open 24/7.</div>'
    aria = '<div><a role="button" aria-label="Plumbing Panda, plumber">x</a></div>'
    plain = '<div><span>Amanda Demanda Plumbing</span> offers <span role="button">Call</span></div>'
    assert classify_entity_binding("Plumbing Panda", linked)[0] == "entity_linked"
    assert classify_entity_binding("Plumbing Panda", aria)[0] == "entity_linked"
    assert classify_entity_binding("Amanda Demanda Plumbing", plain)[0] == "plain_text"
    assert all(s.binding_status in ("entity_linked", "plain_text") for s in extraction.signals)
    assert extraction.four_layer_summary["order_independence_note"].startswith("sentence_order")
    sig = BusinessSignal(name="Amanda Demanda Plumbing", binding_status="plain_text",
                         provenance={"name": "Amanda Demanda Plumbing", "kg_id": "/g/x"})
    ev = next(e for e in AttributionEngine().evidence_checks(sig) if e.check == "entity_binding_evidence")
    assert ev.status == "mismatch" and ev.grade == "dom_confirmed" and "not permanent" in ev.interpretation.lower()


def test_offer_extractor_dollar_value():
    offers = OfferExtractor().extract({"Current Offer": "$179 drain cleaning special, valid through October 2026"},
                                      "Drain cleaning special: $179 — valid through October 2026", "")
    assert len(offers) == 1
    o = offers[0]
    assert o.dollar_value == 179.0 and o.service_name and "drain cleaning" in o.service_name.lower()
    assert o.expiration_date == "October 2026" and o.evidence_grade == "hypothesis"
    assert o.extraction_mechanism == "gbp_offer_post" and not o.multimodal_risk
    schema = ('<script type="application/ld+json">{"@type":"Offer","name":"$100 Off Pipe Camera Inspection",'
              '"priceValidUntil":"2026-10-31","url":"https://example.com/pipe-camera-inspection/"}</script>')
    s = OfferExtractor().extract({"Current Offer": "$100 off pipe camera inspection"}, None, schema)[0]
    assert s.extraction_mechanism == "offer_schema" and s.cta_url.endswith("/pipe-camera-inspection/")
    assert s.expiration_date == "2026-10-31"
    assert OfferExtractor().extract({"Pricing": "$24.99/month membership"}) == []


def test_offer_extractor_multimodal_risk():
    html_ = '<div><img src="https://example.com/banner-100-off.jpg" alt="$100 OFF pipe camera inspection"></div>'
    o = OfferExtractor().extract({"Current Offer": "$100 off pipe camera inspection, valid through October"},
                                 "Welcome to Stuart's Plumbing. Call us today.", html_)[0]
    assert o.multimodal_risk is True and o.extraction_mechanism == "ocr_extraction"
    assert o.grounding_status in ("partial", "ungrounded") and "temporal" in (o.notes or "").lower()


def test_check_protocol_output_structure():
    trace = load_trace_file(SAMPLE)
    res = CheckProtocol().run(trace, "Benjamin Franklin Plumbing", supplied_website_phone="863-797-4507")
    assert isinstance(res, CheckProtocolResult)
    d = res.to_dict()
    points = ["query_device_location", "underlined_vs_highlighted", "chip_label_href", "gbp_nap",
              "site_strings_match", "image_url_from_dom"]
    for k in points:
        assert {"status", "observed", "evidence_grade", "note"} <= set(d[k])
    assert d["query_device_location"]["observed"]["query"] == "emergency plumber near me"
    assert d["gbp_nap"]["status"] == "flag"  # card 797-4507 vs tel 322-0012
    assert d["site_strings_match"]["observed"]["classification"] != "consistent"
    assert res.overall_confidence in ("high", "medium", "low")
