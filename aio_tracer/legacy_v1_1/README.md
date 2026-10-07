# AIO Tracer — Google AI Overview capture & source attribution

AIO Tracer captures Google AI Overviews (AIOs) for a local query and records every field on each local business card: name, rating, review count, category, open status, hours, address, phone, the synthesised "Details:" sentence and the photo. For each value it records where in the DOM it came from:

```
profile (per client+city fingerprint, proxy) → CloakBrowser trace (AIOTrace JS) → SignalExtractor
   → entity payload decode (KG id, CID, tel:, directions, website, photo) → AttributionEngine → JSON + CSV report
```

## Main data reference: Google's patents

The attribution model follows the mechanism Google disclosed in its generative-search patents. The passages are quoted verbatim in `patent_references.py`, and every token and check in a report cites them.

| Patent | Title | Used for |
|---|---|---|
| **US 11,769,017 B1** (Google LLC, filed 2023-03-20) | Generative summaries for search results | The *response linkifying engine*. Method 300: select a portion → find a candidate document → verify by comparison or embedding distance → linkify. The *response confidence engine* (per-portion confidence). The locality measure for SRD selection. |
| **US 11,900,068 B1** (Google LLC) | Generative summaries for search results | Claim 1: different portions of a summary can be verified by different documents, and one portion by several documents. |
| **US 11,886,828 B1** (Google LLC) | Generative summaries for search results | Claim 1: the prompt contains content from each selected SRD plus additional content. |
| **US 2024/0289407 A1** (Google LLC, application) | Search with stateful chat | Synthetic queries (query fan-out) and contextual state, which explain volatility between captures. |

**Read this before using the attributions.** A patent describes what Google *may* do, not what production does. **None of these patents mentions Google Business Profile, reviews or the Knowledge Graph by name.** The four layers below are a keyword heuristic laid over the patents' generic "search result document" model. That is why every token is graded `heuristic`.

The DOM checks are graded `dom_confirmed`, because they compare values that appear verbatim in the captured page. Examples: card phone vs. the entity-payload `tel:`, card address vs. the Directions link, and the GBP UTM tag on the Website link.

The Apex Course DOM findings (`aio_linkification.pdf`, `apex_aio_module.pdf`) are cited with their own confirmed/hypothesis grade.

## Attribution layers

| Layer | What it means | Examples |
|---|---|---|
| `GBP_ENTITY` | The entity record itself: listing name, primary category, hours | "Roto-Rooter", "plumber", "24/7" (when the hours field agrees) |
| `GBP_REVIEWS` | Language typical of reviews: response windows, time of day, holidays, sentiment | "20–30 minutes", "late-night", "around the clock" |
| `WEBSITE_COPY` | Site or blog marketing copy: credentials, warranty, pricing process, price tokens | "licensed and insured", "upfront estimates", "$24.99/month" |
| `GBP_SERVICES` | The services menu: concrete job types | "main line busts", "sump pump repair", "septic issues" |

Each token in a report includes:
- its character span;
- the rule that matched it;
- a confidence score;
- competing alternatives from other layers;
- corroboration from the card's own fields;
- the candidate source document;
- a concrete verification step (GBP Services or Reviews via the CID link, or a website URL search);
- citations.

## Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt            # includes cloakbrowser[geoip] (geoip2) for --geoip
playwright install chromium                 # only used by tests / plain-Playwright fallback
python -m cloakbrowser install              # optional: pre-download the stealth Chromium binary
```

CloakBrowser downloads its patched Chromium the first time it launches. Environment variables:
- `CLOAKBROWSER_BINARY_PATH` points at a local binary.
- `CLOAKBROWSER_VERSION` pins a version. Leave it unset unless that exact free build exists. A Pro build needs `CLOAKBROWSER_LICENSE_KEY`.
- `CLOAKBROWSER_SUPPRESS_FONT_WARNING=1` silences the Windows-font notice.

## Proxies

`--proxy` accepts `http://user:pass@host:port` or `socks5://user:pass@host:port`. The proxy is stored in the profile JSON (chmod 600) and is **always redacted** in logs and reports.

With geoip enabled (the default), CloakBrowser derives the timezone and locale from the proxy's exit IP. Use a proxy that exits in the target city so the AIO's locality signals match the market. The patent's *locality measure* uses the origin of the query. Pass `--no-geoip` to disable this.

## Profiles

Each client+city pair gets its own persistent profile.
- **Fingerprint seed:** `sha256("client_id:city")` mapped into CloakBrowser's seed range and passed as `--fingerprint=SEED`. The same client and city always produce the same device.
- **Storage:** profile JSON in `profiles/<id>.json`; cookies and storage in `profiles/user_data/<id>/`.

```bash
python main.py --list-profiles
python main.py --delete-profile client-001-lakeland-fl            # also removes browser data
python main.py --delete-profile client-001-lakeland-fl --keep-user-data
```

## Run

```bash
# Live capture (headless). Repeat captures: the Apex findings recommend >=5 per query/location.
python main.py --query "emergency plumber near me" --city "Lakeland FL" --client client_001 \
    --proxy "http://user:pass@lakeland-exit.example:8000" --captures 5 --screenshots

# Watch the browser
python main.py --query "emergency plumber near me" --city "Lakeland FL" --client client_001 --headed

# Offline: re-analyse a saved trace or a DevTools console dump
python main.py --trace-file samples/emergency_plumber_lakeland_trace.json --client client_001 --city "Lakeland FL"
```

Outputs:
- `reports/{client}_{city}_{timestamp}.json` contains the inputs, the public profile, the capture block (status, method, retries, redacted proxy, errors), every signal with full provenance, the attributions with evidence checks and a verification plan, and the cited patent passages with a bibliography.
- `reports/{client}_{city}_{timestamp}.csv` has one row per business: KG id, CID, `tel:`, layer shares and tokens, and check statuses.
- `reports/..._trace.json` is the raw AIOTrace capture, so every number can be re-derived.

Capture `status` values:
- `ok`
- `no_aio`
- `aio_without_local_list`
- `blocked` (the `/sorry/` page, which is never retried)
- `error`

The pipeline never crashes on a missing AIO. It writes a report with empty signals.

## Manual DevTools capture (same JS)

Paste `js/aiotrace.js` into the console on a results page, select the AIO local-place list in Elements, then run:

```js
window.__AIO_ROOT = $0; copy(JSON.stringify(AIOTrace.capture(__AIO_ROOT, 'expanded-aio'), null, 2))
```

`AIOTrace.findRoot()` finds the list automatically. The tracer does the same thing through `page.evaluate`.

## How each value is traced

| Field | Source in the DOM |
|---|---|
| name, rating, review count, category, open status | `root.visibleText`, card block, plus the `aria-label="Rated X out of 5, N reviews"` |
| address, phone, hours, Details | the `Address:` / `Phone:` / `Hours:` / `Details:` rows of each card |
| KG id, feature id, CID, lat/lon | the `<!--TgQPHd|||…-->` ENTITY payload in `root.outerHTML` |
| `tel:` (Call button) | the ACTION_CALL payload, taken from the entity record and not from the sentence |
| directions address / feature id | the ACTION_DIRECTIONS payload (Maps deep link) |
| website URL + UTM | the ACTION_WEBSITE payload. A `utm_source=GBP` or `utm_campaign=gmb` tag means the link is the GBP website field |
| photo | the NAME_IMAGE (`LOCAL_PLACE`) payload |
| source chips | SOURCE_CHIP payloads (the UUIDs are session-specific) |

## Tests

```bash
python -m pytest -q tests
```

The suite covers:
- seed determinism and profile create/list/delete;
- extraction of the 4-business sample, including KG id, CID and photo;
- graceful handling of a trace with no AIO;
- the per-layer token rules;
- the Benjamin Franklin phone mismatch (card `863-797-4507` vs. payload `tel:+18633220012`);
- the patent registry;
- an AIOTrace JS capture in real headless Chromium against the sample DOM, including the v1.1 layers;
- source-chip feature-type classification, entity binding (`entity_linked` / `plain_text`);
- phone anomaly classification (consistent, mismatch, 225 directory-number note);
- offer extraction (dollar value, service, expiry, Offer schema, multimodal/OCR risk);
- the six-point check protocol's output structure.

## v1.1 layer audits

| Module | What it adds |
|---|---|
| `layer_capture.py` | Four independent layers (entity · source chip · image · card) per capture; `ExtractionResult.four_layer_summary` |
| `phone_analyzer.py` | Card/sentence phone vs `tel:` payload vs website phone (`--phone-audit`, `--website-phone NAME=PHONE`) |
| `offer_extractor.py` | Offer lines → GBP Offer post / Offer schema / declarative HTML / OCR, with multimodal (temporal-drift) risk |
| `triangulation.py` | GBP name vs page title/H1 vs schema name; sameAs strength (license board > BBB > Yelp/HomeAdvisor) |
| `check_protocol.py` | Six-point protocol per business (`--check-protocol`) |
| `multi_capture.py` | Stability across captures; `reports/*_stability.json` when `--captures N` > 1 or several `--trace-file`s |

Facts read from the DOM are `dom_confirmed`; causes (tracking number, Offer-post origin, OCR origin) are `hypothesis`.

## Limitations

- **A DOM snapshot is not server-side provenance.** Layer attribution is an inference to verify, not a fact.
- **Results are volatile.** A single capture is anecdotal.
- **Google may show a consent page or CAPTCHA** depending on the proxy or IP. These are reported as `blocked` and not retried.
