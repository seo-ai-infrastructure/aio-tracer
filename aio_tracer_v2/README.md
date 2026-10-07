# AIO Tracer 2.0

A local-business AI Overview evidence tracker: capture Google's displayed answer, inspect citation relationships, retrieve cited pages, compare factual coverage, and measure changes over time.

## Start here

Python 3.11+ is required. The dashboard is already built in `dashboard/dist`, so Node is optional unless editing the frontend.

macOS / Linux:

```bash
./start.sh
```

Windows PowerShell:

```powershell
.\start.ps1
```

Open **http://127.0.0.1:8080**. Choose **Load historical sample** to inspect the original four-business Lakeland trace. It is labeled partial and excluded from verified visibility rates. No invented citation relationships are added to that sample.

Manual start:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
python -m uvicorn server:app --host 127.0.0.1 --port 8080
```

The default local server runs its capture worker in a background thread. Database, profiles, browser data and screenshots are written under `data/`. Keep that folder to retain history. `DATABASE_URL` enables Postgres instead of SQLite; screenshots and browser profiles still need a persistent filesystem.

## Main workflow

1. **New capture:** enter client, requested city, query, device and optional coordinates/named proxy. Run once or start daily/weekly tracking.
2. **Capture jobs:** see persistent queued work, failures and retries. Pause/resume schedules here. A schedule runs only while the worker is active; missed intervals are not backfilled.
3. **Visibility:** choose a matching client, query, market and device. Enter the exact target business name and bare website domain to measure mentions, entity links and citations separately.
4. **Evidence:** inspect exact claim text, offsets, displayed citation associations and before/after interaction screenshots. Click **Retrieve cited pages** to save source snapshots and passage candidates. Record a support review with a note.
5. **Page comparison:** enter your page URL, optionally its business name, and select URLs observed as citations in the chosen capture. Review indexing/snippet directives, robots rules, extraction, canonical, structured facts, brand consistency and claim coverage gaps.
6. **Experiments:** record a page change, timestamp, target business/domain and unchanged control query. Capture matching queries before and after, over multiple days. Results show rates and descriptive control-adjusted changes, without claiming statistical significance or causation.

Source page fetching is explicit. It is limited to public HTTP(S) HTML/text, ports 80/443, 2 MB/page and bounded redirects. Private-network URLs are blocked. PDFs, authentication walls and client-rendered content may require independent review. The source HTTP fetch is separate from the Google browser capture.

## What was fixed

- AIO identification requires an observed AIO boundary; rating lists and ordinary local results are insufficient.
- The complete recognized answer is captured, including prose without business cards.
- Expansion controls are scoped to the recognized AIO. Source/entity/image interactions are attempted sequentially, with bounded waits, panel records and before/after screenshots. Incomplete or unsupported states stay explicit.
- Captured extended source, image and card layers reach the extraction pipeline.
- Payloads are joined by matching business names; uncertain position-only joins no longer assign another business's data.
- Failed/blocked/unrecognized captures and unverified legacy snapshots are excluded from visibility denominators.
- Cross-capture phone stability and cross-field phone agreement are separate fields.
- Requested market and observed Google location are separate. A city label or supplied coordinates do not prove where Google localized the result.
- Generic name buttons no longer imply entity resolution or visual underlining.
- Output uses `source_hypotheses`, `rule_score`, `rule_share` and `matched_text_coverage`. Scores are not probabilities, real model tokens, attention weights, or source provenance.
- Offer presence in the captured answer is labeled `observed_in_aio`; it does not establish independent grounding or OCR origin.
- sameAs strengths are unset unless explicitly configured. Licensing/directory labels have no asserted causal strength.
- Browser tests skip only when a browser prerequisite is missing. JavaScript and capture failures fail the test.
- Unique timestamps avoid report collisions; profile IDs resist slug collisions and path traversal.

## Evidence semantics

| Output | Meaning |
| --- | --- |
| `verified_aio` | A supported AIO boundary was observed. It does not establish complete citation inventory or verified geography. |
| `no_aio` | Search rendered without a recognized AIO or ambiguous AIO heading after the bounded wait. An unrecognized layout can still require manual inspection. |
| `legacy_partial_or_unverified` | Inspectable historical snapshot; excluded from verified metrics. |
| `same_claim_block` | Citation control observed within the captured claim block. It does not prove that every sentence is supported by every listed URL. |
| `opened_from_control` | Source URL observed in a newly opened panel from a recorded control. |
| `unmapped` | No claim-specific citation association recorded. A page-level chip alone is insufficient. |
| `exact_text_match` | Claim text found in a retrieved passage after whitespace/case normalization. It is a textual observation, not proof of model provenance. |
| `lexical_candidate` | Shared terms suggest a passage to review. Negations, numbers, scope and subject can disagree. |
| `human_review` | Operator assessment with a note. |
| `interactive_unverified` | Named control observed without sufficient entity-specific destination/payload evidence. |

Claim offsets use JavaScript UTF-16 code units into `root.visibleText`. Citation labels are excluded from claim text. If text is not a contiguous span in the captured root, offsets are null; `raw_block_text` and raw-block offsets preserve the surrounding display. Drawer screenshots require working browser capture and persistent screenshot storage.

Rates use **valid captures including AIO absence**. Failed and legacy captures do not count as absence. Website citation rate measures observed URLs for the exact bare domain; it can undercount unopened drawers. Business names in prose are literal mention matches; stable entity identifiers are preferred for card history. Choose consistent filters; pooled queries/device mixes are not controlled experiments. Five rapid repeats are not a statistical reliability threshold.

Citation loss alerts require complete captured source-control inventories on both snapshots. Partial captures report **previously observed, not seen**, rather than claiming loss. This completeness is limited to controls the selector adapter recognized, not all internal Google sources.

The bundled `patent_references.py` preserves the original reference registry for context. Course PDFs are not bundled or independently verified. Disclosed patent mechanisms and legacy course hypotheses do not establish Google's production internals.

## CLI

Existing offline extraction remains supported:

```bash
python main.py --trace-file samples/emergency_plumber_lakeland_trace.json \
  --client client_001 --city "Lakeland FL" --phone-audit --check-protocol \
  --database data/aio.sqlite
```

Live capture (screenshots recommended):

```bash
python main.py --query "emergency plumber in Lakeland FL" \
  --city "Lakeland FL" --client client_001 --device mobile --screenshots \
  --latitude 28.0395 --longitude -81.9498 --database data/aio.sqlite
```

Add `--retrieve-sources` to retrieve observed citation URLs and build passage candidates. Add `--client-url https://your-business.com/service` to compare against retrieved pages. `--captures N` repeats observations; `--capture-interval` controls spacing. Prefer separate runs across days.

CloakBrowser remains optional:

```bash
pip install -r requirements-cloak.txt
python main.py --browser cloak --query "plumber Lakeland FL" --city "Lakeland FL" --client client_001
```

CloakBrowser's installed version and binary availability must support the launch options. It does not guarantee CAPTCHA-free Google access. Challenges are recorded as failures. No CAPTCHA-solving or endless block retries are implemented.

## Browser adapter / manual capture

`js/aiotrace.js` recognizes explicit `data-aio-root` fixtures and supported layouts anchored to a visible "AI Overview" heading. It reads source-control attributes and recognizable SOURCE_CHIP payload comments, rather than treating every outbound link as a citation. Google changes layouts; missing controls or unrecognized roots require selector updates or a manual snapshot.

In DevTools select the full AIO container and run the script, then:

```js
copy(JSON.stringify(AIOTrace.capture($0, 'manual-aio'), null, 2))
```

Manual/legacy imports remain unverified unless their capture metadata establishes a supported verified boundary. This prevents silently promoting a manually selected local pack to a confirmed AIO.

## Deployment

See **DEPLOYMENT.md** for Vercel frontend + Railway API/worker + Neon Postgres, environment variables and persistent volumes. This is a single-operator application, not a multi-tenant SaaS. Hosted access requires an operator token. Deployments are supplied, not automatically published.

## Validation

```bash
python -m pytest -q tests
cd dashboard
npm ci
npm run build
```

Set `AIO_TEST_BROWSER` to an existing Chromium executable when the default Playwright binary is unavailable. Tests cover capture scoping and drawer states in browser fixtures, legacy extraction, denominators, phone consistency, source retrieval parsing, evidence chains, comparisons, persistent jobs, schedules, authentication and experiments.

See **VERIFICATION.md** for what was actually verified and the live-environment limits.
