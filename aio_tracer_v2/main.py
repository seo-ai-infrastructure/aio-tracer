#!/usr/bin/env python3
"""AIO Tracer CLI: profile -> trace -> extract -> attribute -> report.

Examples
--------
Live capture (CloakBrowser):
    python main.py --query "emergency plumber near me" --city "Lakeland FL" --client client_001 \
        --proxy "http://user:pass@proxy.example:8000"

Offline (parse an existing trace JSON or a DevTools console dump):
    python main.py --trace-file samples/trace.json --client client_001 --city "Lakeland FL"

Profiles:
    python main.py --list-profiles
    python main.py --delete-profile client_001--lakeland-fl
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from slugify import slugify

from attribution_engine import LAYERS, AttributionEngine, AttributionReport
from check_protocol import CheckProtocol
from multi_capture import MIN_CAPTURES, MultiCaptureAggregator
from offer_extractor import OfferExtractor
from phone_analyzer import PhoneAnalyzer
from patent_references import bibliography, cite
from profile_manager import DEFAULT_PROFILES_DIR, BrowserProfile, ProfileManager, redact_proxy
from signal_extractor import ExtractionResult, SignalExtractor, write_csv

TOOL_VERSION = "2.1.0"
BASE_DIR = Path(__file__).resolve().parent
logger = logging.getLogger("aio_tracer.cli")


def setup_logging(verbose: bool, quiet: bool) -> None:
    level = logging.DEBUG if verbose else (logging.WARNING if quiet else logging.INFO)
    try:
        from rich.logging import RichHandler
        handler: logging.Handler = RichHandler(rich_tracebacks=True, show_path=False)
        fmt = "%(message)s"
    except ImportError:  # pragma: no cover
        handler, fmt = logging.StreamHandler(), "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(level=level, format=fmt, datefmt="[%X]", handlers=[handler], force=True)
    for noisy in ("httpx", "httpcore", "asyncio", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Capture Google AI Overviews and trace every data point to its source.")
    p.add_argument("--query", help="Search query, e.g. 'emergency plumber near me'")
    p.add_argument("--city", help="City label for the profile, e.g. 'Lakeland FL'")
    p.add_argument("--client", dest="client_id", help="Client identifier, e.g. client_001")
    p.add_argument("--proxy", help="Proxy URL (http://user:pass@host:port or socks5://...). Never logged in clear.")
    p.add_argument("--trace-file", type=Path, nargs="+",
                   help="Offline mode: parse existing trace JSON(s); several files = a multi-capture stability run")
    p.add_argument("--check-protocol", action="store_true",
                   help="Run the six-point check protocol (apex Part 12) for every business")
    p.add_argument("--phone-audit", action="store_true",
                   help="Phone anomaly audit: card/sentence phone vs Call-button tel: vs website phone")
    p.add_argument("--website-phone", action="append", default=[], metavar="NAME=PHONE",
                   help="Website phone for a business (repeatable), used by --phone-audit/--check-protocol")
    p.add_argument("--captures", type=int, default=1, help="Repeated captures to run (not statistically independent)")
    p.add_argument("--capture-interval", type=float, default=20.0, help="Seconds between repeated captures")
    p.add_argument("--headed", action="store_true", help="Show the browser window (default headless)")
    p.add_argument("--no-geoip", action="store_true", help="Don't derive timezone/locale from the proxy IP")
    p.add_argument("--no-expand", action="store_true", help="Don't click 'Show more' in the AIO")
    p.add_argument("--hl", default="en", help="Google interface language (hl=)")
    p.add_argument("--gl", default="us", help="Google country (gl=); empty string to omit")
    p.add_argument("--locale", help="Browser locale override, e.g. en-US")
    p.add_argument("--timezone", help="Browser timezone override, e.g. America/New_York")
    p.add_argument("--screenshots", action="store_true", help="Save a full-page screenshot per capture")
    p.add_argument("--save-trace", action="store_true", default=True, help="Save the raw trace next to the report (default)")
    p.add_argument("--no-save-trace", dest="save_trace", action="store_false")
    p.add_argument("--reports-dir", type=Path, default=BASE_DIR / "reports")
    p.add_argument("--profiles-dir", type=Path, default=DEFAULT_PROFILES_DIR)
    p.add_argument("--list-profiles", action="store_true", help="List stored profiles and exit")
    p.add_argument("--delete-profile", metavar="PROFILE_ID", help="Delete a profile (and its browser data) and exit")
    p.add_argument("--keep-user-data", action="store_true", help="With --delete-profile: keep the browser data dir")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("-q", "--quiet", action="store_true")
    p.add_argument("--device", choices=["desktop", "mobile"], default="desktop")
    p.add_argument("--latitude", type=float)
    p.add_argument("--longitude", type=float)
    p.add_argument("--browser", choices=["playwright", "cloak", "cloak_manager"], default="playwright")
    p.add_argument("--no-network", action="store_true", help="Disable passive CDP observation")
    p.add_argument("--disable-cache", action="store_true", help="Disable browser HTTP cache for a diagnostic cohort")
    p.add_argument("--query-archetype", choices=['informational','comparative','local_entity','ymyl','shopping'],default='local_entity')
    p.add_argument("--database", help="Save history to SQLite path or Postgres URL (otherwise reports only)")
    p.add_argument("--retrieve-sources", action="store_true", help="Retrieve up to 10 observed citation pages and save passage candidates")
    p.add_argument("--client-url", help="Audit and compare this business page against retrieved citation pages")
    args = p.parse_args(argv)
    if not (args.list_profiles or args.delete_profile):
        missing = [n for n, v in (("--client", args.client_id), ("--city", args.city)) if not v]
        if not args.trace_file and not args.query:
            missing.append("--query")
        if missing:
            p.error("missing required argument(s): " + ", ".join(missing))
    if args.captures < 1:
        p.error("--captures must be >= 1")
    args.trace_files = list(args.trace_file or [])
    args.trace_file = args.trace_files[0] if args.trace_files else None
    args.website_phones = {}
    for item in args.website_phone:
        name, sep, phone = item.partition("=")
        if not sep:
            p.error(f"--website-phone expects NAME=PHONE, got {item!r}")
        args.website_phones[name.strip().lower()] = phone.strip()
    return args


# --------------------------------------------------------------------------- reporting
def attribution_columns(reports: list[AttributionReport]) -> dict[int, dict]:
    cols: dict[int, dict] = {}
    for i, r in enumerate(reports):
        row = {"attr_source_summary": r.source_summary, "rule_score": r.confidence, "matched_text_coverage": r.coverage}
        for layer in LAYERS:
            row[f"rule_share_{layer}"] = r.layer_shares.get(layer, 0)
            row[f"matched_spans_{layer}"] = "; ".join(dict.fromkeys(t.text for t in r.tokens if t.layer == layer))
        for e in r.evidence:
            if e.check in ("phone_card_vs_payload_tel", "address_card_vs_directions", "name_card_vs_entity_payload"):
                row[f"check_{e.check}"] = e.status
        row["payload_tel"] = next((e.observed.get("payload_tel") for e in r.evidence
                                   if e.check == "phone_card_vs_payload_tel"), None)
        cols[i] = row
    return cols


def build_report(*, args, profile: Optional[BrowserProfile], trace: dict, extraction: ExtractionResult,
                 attributions: list[AttributionReport], run_id: str, trace_path: Optional[Path]) -> dict:
    refs = sorted({r for a in attributions for r in a.references})
    capture = trace.get("capture") or {"status": "offline", "method": "trace-file", "source": str(args.trace_file)}
    return {
        "meta": {
            "tool": "aio_tracer", "version": TOOL_VERSION, "run_id": run_id,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "mode": "offline" if args.trace_file else "live",
            "evidence_policy": ("evidence[] checks compare DOM-observed values and are 'dom_confirmed'; token "
                                "attributions are keyword heuristics ('heuristic') mapped onto the patent-described "
                                "linkify/verify model and must be verified against the cited documents."),
        },
        "inputs": {"query": extraction.query or args.query, "city": args.city, "client_id": args.client_id,
                   "proxy": redact_proxy(args.proxy)},
        "profile": profile.to_public_dict() if profile else None,
        "capture": capture,
        "trace_source": {"page_url": extraction.page_url, "captured_at_utc": extraction.captured_at,
                         "schema_version": trace.get("schemaVersion"), "label": trace.get("label"),
                         "user_agent": trace.get("userAgent"), "timezone": trace.get("timezone"),
                         "raw_trace_file": str(trace_path) if trace_path else None,
                         "limitations": trace.get("limitations")},
        "extraction": {
            "signals": [s.to_dict() for s in extraction.signals],
            "related_results": extraction.related_results,
            "source_chips": extraction.source_chips,
            "payload_summary": extraction.payload_summary,
            "warnings": extraction.warnings,
        },
        "four_layer_summary": extraction.four_layer_summary,
        "attributions": [a.to_dict() for a in attributions],
        "references": {"cited": {r: cite(r) for r in refs}, "bibliography": bibliography()},
    }


def layer_audits(args, trace: dict, extraction: ExtractionResult) -> dict:
    """Optional --phone-audit / --check-protocol sections plus offer records (always)."""
    out: dict = {"offers": []}
    outer = (trace.get("root") or {}).get("outerHTML") or ""
    visible = (trace.get("root") or {}).get("visibleText") or ""
    for s in extraction.signals:
        for o in OfferExtractor().extract(s.synthesized_fields, visible, outer):
            out["offers"].append({"business_name": s.name, **o.to_dict()})
    phones = getattr(args, "website_phones", {}) or {}
    if getattr(args, "phone_audit", False):
        pa = PhoneAnalyzer()
        out["phone_audit"] = [pa.analyze(s.name, s.phone, (s.provenance or {}).get("tel_uri"),
                                         phones.get(s.name.lower())).to_dict() for s in extraction.signals]
    if getattr(args, "check_protocol", False):
        cp = CheckProtocol()
        out["check_protocol"] = [cp.run(trace, s.name, phones.get(s.name.lower())).to_dict()
                                 for s in extraction.signals]
    return out


def write_outputs(report: dict, extraction: ExtractionResult, attributions: list[AttributionReport],
                  out_dir: Path, stem: str) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path, csv_path = out_dir / f"{stem}.json", out_dir / f"{stem}.csv"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    write_csv(extraction.signals, csv_path, extra_columns=attribution_columns(attributions))
    return json_path, csv_path


def print_summary(report: dict, attributions: list[AttributionReport], json_path: Path, csv_path: Path) -> None:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    cap = report["capture"]
    console.rule(f"[bold]AIO trace · {report['inputs']['query']!r} · {report['inputs']['city']} · {cap.get('status')}")
    signals = report["extraction"]["signals"]
    if not signals:
        console.print(f"[yellow]No local-place AIO captured (status={cap.get('status')}). "
                      f"Errors: {cap.get('errors') or '-'}[/yellow]")
    table = Table(show_lines=True, expand=True)
    for col, kw in (("#", {"justify": "right", "width": 2}), ("Business / KG id", {}), ("★ / reviews", {"width": 10}),
                    ("Phone (card | tel:)", {}), ("Details → layers", {"ratio": 3}), ("Rule", {"width": 5}),
                    ("DOM checks", {})):
        table.add_column(col, **kw)
    for sig, att in zip(signals, attributions):
        prov = sig.get("provenance") or {}
        checks = {e.check: e.status for e in att.evidence}
        phone_status = checks.get("phone_card_vs_payload_tel", "-")
        phone_cell = f"{sig.get('phone') or '-'} | {prov.get('tel_uri') or '-'}"
        if phone_status == "mismatch":
            phone_cell = f"[red]{phone_cell}\nMISMATCH[/red]"
        layers = " · ".join(f"{round(v * 100)}% {k}" for k, v in sorted(att.layer_shares.items(), key=lambda x: -x[1]) if v)
        check_cell = "\n".join(f"{'✓' if v == 'match' else ('✗' if v == 'mismatch' else '·')} {k}"
                               for k, v in checks.items() if v in ("match", "mismatch"))
        table.add_row(str(sig.get("position")), f"[bold]{sig['name']}[/bold]\n{prov.get('kg_id') or '-'}"
                      f"\ncid {prov.get('cid') or '-'}",
                      f"{sig.get('rating')} / {sig.get('review_count_display') or sig.get('review_count')}",
                      phone_cell, f"{sig.get('details_text') or '-'}\n[cyan]{layers}[/cyan]",
                      f"{att.confidence:.2f}", check_cell or "-")
    if signals:
        console.print(table)
    chips = report["extraction"]["source_chips"]
    if chips:
        console.print("Source chips: " + ", ".join(f"{c.get('label')} → {c.get('url')}" for c in chips))
    four = report.get("four_layer_summary") or {}
    if four:
        ent, cards = four.get("entity_layer", {}), four.get("card_layer", {})
        console.print(f"Four layers: entity {ent.get('count', 0)} (plain-text: {len(ent.get('plain_text', []))}) · "
                      f"chips {four.get('source_chip_layer', {}).get('count', 0)} · "
                      f"images {four.get('image_layer', {}).get('count', 0)} · cards {cards.get('count', 0)}  "
                      f"[dim]{four.get('order_independence_note')}[/dim]")
        for name in ent.get("plain_text", []):
            console.print(f"[yellow]entity layer:[/yellow] {name!r} has no proven entity-specific control — unverified "
                          "binding; re-capture and inspect.")
    stab = report.get("stability")
    if stab:
        for name, n in (stab.get("entity_set_stability", {}).get("unstable") or {}).items():
            console.print(f"[yellow]entity layer unstable:[/yellow] {name} in {n}/{stab['n_captures']} captures")
    elif (report.get("meta") or {}).get("captures_in_run", 1) < MIN_CAPTURES:
        console.print(f"[yellow]entity layer stability unknown:[/yellow] single capture — run ≥{MIN_CAPTURES} "
                      "captures before acting (1 of 4 captures showed Ben Franklin in course data).")
    for row in report.get("phone_audit") or []:
        if row["classification"] != "consistent":
            console.print(f"[magenta]phone audit:[/magenta] {row['business_name']}: {row['classification']} "
                          f"({row['evidence_grade']})")
    for res in report.get("check_protocol") or []:
        console.print(f"check protocol: {res['business_name']}: overall {res['overall_confidence']} · "
                      + " ".join(f"{k}={res[k]['status']}" for k in ("query_device_location", "underlined_vs_highlighted",
                                                                      "chip_label_href", "gbp_nap", "site_strings_match",
                                                                      "image_url_from_dom")))
    for o in report.get("offers") or []:
        console.print(f"offer: {o['business_name']}: {o['raw_text']!r} → {o['extraction_mechanism']} "
                      f"(multimodal_risk={o['multimodal_risk']}, hypothesis)")
    for w in report["extraction"]["warnings"]:
        console.print(f"[yellow]warning:[/yellow] {w}")
    console.print(f"[dim]Source hypotheses are uncalibrated regex rules; DOM checks are confirmed from the captured DOM. "
                  f"Single captures are volatile — run across days per query/location.[/dim]")
    console.print(f"Report: [green]{json_path}[/green]\nCSV:    [green]{csv_path}[/green]")


# --------------------------------------------------------------------------- commands
def cmd_list_profiles(pm: ProfileManager) -> int:
    from rich.console import Console
    from rich.table import Table

    profiles = pm.list_profiles()
    table = Table(title=f"Profiles ({len(profiles)})")
    for col in ("profile_id", "client", "city", "seed", "proxy", "geoip", "updated"):
        table.add_column(col)
    for pr in profiles:
        d = pr.to_public_dict()
        table.add_row(d["profile_id"], d["client_id"], d["city"], str(d["fingerprint_seed"]),
                      str(d.get("proxy_url") or "-"), str(d["geoip"]), d["updated_at"])
    Console().print(table)
    return 0


def run_once(args, pm: ProfileManager, capture_no: int, run_id: str) -> tuple[int, dict]:
    from aio_tracer import AIOTracer, TracerConfig, load_trace_file

    profile: Optional[BrowserProfile] = None
    if args.trace_file:
        logger.info("Offline mode: loading %s", args.trace_file)
        trace = load_trace_file(args.trace_file)
    else:
        profile = pm.get_or_create(args.client_id+'::'+args.device, args.city, args.proxy, geoip=not args.no_geoip,
                                   locale=args.locale, timezone_name=args.timezone)
        cfg = TracerConfig(headless=False if args.browser=="cloak_manager" else not args.headed, expand_aio=not args.no_expand, hl=args.hl, gl=args.gl or None,
                           screenshot_dir=(args.reports_dir / "screenshots") if args.screenshots else None,
                           device=args.device, latitude=args.latitude, longitude=args.longitude, browser=args.browser,
                           observe_network=not args.no_network,disable_cache=args.disable_cache,query_archetype=args.query_archetype)
        logger.info("Capture %d/%d with profile %s (seed %s, proxy %s)", capture_no, args.captures,
                    profile.profile_id, profile.fingerprint_seed, redact_proxy(profile.proxy_url))
        trace = AIOTracer(cfg).trace(profile, args.query)

    extraction = SignalExtractor().extract(trace)
    attributions = AttributionEngine().attribute_all(extraction.signals)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    stem = f"{slugify(args.client_id, regex_pattern=r'[^-a-z0-9_]+')}_{slugify(args.city)}_{ts}" + (f"_c{capture_no}" if args.captures > 1 else "")
    trace_path = None
    if args.save_trace and not args.trace_file:
        args.reports_dir.mkdir(parents=True, exist_ok=True)
        trace_path = args.reports_dir / f"{stem}_trace.json"
        trace_path.write_text(json.dumps(trace, indent=2, ensure_ascii=False), encoding="utf-8")
    elif args.trace_file:
        trace_path = args.trace_file

    report = build_report(args=args, profile=profile, trace=trace, extraction=extraction,
                          attributions=attributions, run_id=run_id, trace_path=trace_path)
    report["meta"]["captures_in_run"] = len(args.trace_files) if args.trace_files else args.captures
    report.update(layer_audits(args, trace, extraction))
    from pipeline import analyze_trace, persist_capture
    from source_audit import audit_page, evidence_chains, compare_pages
    from measurement import canonical_url
    analysis=analyze_trace(trace)
    pages={}
    if args.retrieve_sources:
        for c in extraction.source_chips[:10]:
            url=canonical_url(c.get("url"))
            if not url or url in pages:continue
            try:pages[url]=audit_page(url)
            except Exception as exc:pages[url]={"requested_url":url,"error":str(exc)[:300]}
        analysis["chains"]=evidence_chains(trace,pages)
        report["source_snapshots"]=pages
    report["analysis"]=analysis
    if args.client_url:
        own=audit_page(args.client_url)
        report["comparison"]=compare_pages(own,[p for p in pages.values() if not p.get("error")],
                                           [c["claim"] for c in analysis["chains"]],extraction.query)
    if args.database:
        from history_store import Store
        store=Store(args.database)
        record=persist_capture(store,trace,args.client_id,args.city,args.device)
        payload=record["payload"];payload["analysis"]=analysis;store.put("capture",payload,record["id"])
        report["history_capture_id"]=record["id"]
        for page in pages.values():store.put("page",{**page,"capture_id":record["id"]})
    json_path, csv_path = write_outputs(report, extraction, attributions, args.reports_dir, stem)
    logger.info("Wrote %s and %s", json_path, csv_path)
    print_summary(report, attributions, json_path, csv_path)
    status = report["capture"].get("status")
    return (0 if status in ("ok", "offline", "no_aio", "aio_without_local_list") else 2), trace


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose, args.quiet)
    pm = ProfileManager(args.profiles_dir)
    if args.list_profiles:
        return cmd_list_profiles(pm)
    if args.delete_profile:
        ok = pm.delete(args.delete_profile, remove_user_data=not args.keep_user_data)
        logger.info("Profile %s %s", args.delete_profile, "deleted" if ok else "not found")
        return 0 if ok else 1

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    files = list(args.trace_files)
    captures = len(files) if files else args.captures
    if not files:
        args.captures = captures
    if captures < MIN_CAPTURES:
        logger.info("Note: AIO results are volatile; >=%d captures per query/location are recommended.", MIN_CAPTURES)
    rc, traces = 0, []
    for n in range(1, captures + 1):
        if files:
            args.trace_file = files[n - 1]
            args.captures = captures
        code, trace = run_once(args, pm, n, run_id)
        rc, _ = max(rc, code), traces.append(trace)
        if n < captures and not files:
            time.sleep(args.capture_interval)
    if captures > 1:
        write_stability(args, traces, run_id)
    return rc


def write_stability(args, traces: list[dict], run_id: str) -> Path:
    report = MultiCaptureAggregator().aggregate(traces, location=args.city)
    args.reports_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{slugify(args.client_id, regex_pattern=r'[^-a-z0-9_]+')}_{slugify(args.city)}_{run_id}"
    path = args.reports_dir / f"{stem}_stability.json"
    path.write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    logger.info("Stability report (%d captures): %s — %s", report.n_captures, path, report.conclusion)
    from rich.console import Console

    console = Console()
    for name, n in report.entity_set_stability.get("unstable", {}).items():
        console.print(f"[yellow]entity layer unstable:[/yellow] {name} in {n}/{report.n_captures} captures")
    if report.n_captures < MIN_CAPTURES:
        console.print(f"[yellow]Only {report.n_captures} captures — below the {MIN_CAPTURES}-capture minimum.[/yellow]")
    console.print(f"Stability: [green]{path}[/green]")
    return path


if __name__ == "__main__":
    sys.exit(main())
