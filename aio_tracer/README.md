# AIO Tracer 3.0 — Knowledge Studio + inspectable evidence chains

Capture a displayed AIO claim, inspect its citation control and source passage,
then compare that chain with later observations.

**New: launch Knowledge Studio with `python3 graph_studio.py --demo`, then open
http://127.0.0.1:8765.** It includes a clickable knowledge graph, service/topic
vocabulary, exact-quote extraction, owner review, page audits, a draft editor,
change history and optional Gemini/Neo4j integrations. See `KNOWLEDGE_GRAPH.md`.
The local Studio requires only Python; browser capture and providers have
optional dependencies. Start with `START_HERE.html` for launch instructions.

**The earlier evidence-chain demonstration is in `examples/demo/START_HERE.html`.** It contains two completed browser
captures and a visible change from a $49 to a $59 diagnostic visit. The fixture
is clearly labeled synthetic: it is not a live Google result. No installation
is required to open its reports after extracting the ZIP.

The original Lakeland capture remains in `samples/`. It can be replayed, but it
contains only the old card snapshot: missing drawers, surrounding summaries and
passages cannot be reconstructed as observed evidence.

## What changed

| Capability | Implementation |
|---|---|
| Complete observed AIO states | Summary region, cards, exact linked spans, external drawers, entity panels and image destinations; bounded expansion and panel scrolling |
| Citation graph | Exact text/offsets, entity identifiers, visible control, original URL, canonical comparison key, sentence/group/direct-span scope and opening action |
| Browser passage evidence | Actual visible passages, URL and redirects, UTC timestamp, DOM location, screenshot and content hash |
| Claim re-search | Exact claim and fact-oriented rewrite; results always marked tool-generated candidates |
| Context and repeatability | Google location UI text, requested/query geography, browser/device/viewport/session context; persistent and fresh storage cohorts |
| Competitor comparison | Price, response time, availability, warranty and service-area facts, cited support and coverage in supplied client pages |
| History | SQLite observations, source appearances, changed claim text/citations/passages, entity position/assets, changed page hashes and operator content-change notes |
| Reports | Portable HTML explorer, JSON, CSV and raw trace artifacts |

`attribution_engine.py` remains for v1 compatibility. Its weighted keyword
percentages are **not used in v2 evidence reports**. They are search hints,
not measured source contributions or calibrated probabilities.

## Install

Use Python 3.11 or newer. Open a terminal in the extracted `aio_tracer` folder.

```bash
python -m venv .venv
# macOS / Linux:
source .venv/bin/activate
# Windows PowerShell instead: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
```

CloakBrowser is supported explicitly through `--engine cloak` (the default).
Its browser build, platform and any required license must be available. Configure
its documented environment variables for your installation. Nothing here bypasses
a license requirement or promises CAPTCHA-free operation. For stock Chromium,
choose `--engine chromium`; the program does not silently switch engines.

CloakBrowser `geoip` can derive browser timezone/locale from a proxy. That does not
establish Google's perceived city. Review the captured location UI before
interpreting local results.

## Inspect the archived Lakeland capture

```bash
python main.py --trace-file samples/emergency_plumber_lakeland_trace.json \
  --client lakeland-replay --city "Lakeland FL" --no-research
```

Open the printed `report.html`. It shows the four saved business claims and marks
their citation chains unresolved where the original capture lacks the evidence.
To collect new browser evidence for that archive, add `--verify-offline` and
candidate URLs; newly retrieved support does not retroactively become a displayed
citation.

## Collect a live observation

```bash
python main.py \
  --query "emergency plumber near me" --city "Lakeland FL" --client client_001 \
  --engine cloak --proxy "http://user:password@your-proxy:port" \
  --client-name "Your Actual Business Name" \
  --client-url "https://your-business.example/lakeland/" \
  --client-url "https://your-business.example/pricing/" \
  --session-mode paired --captures 2
```

Replace the example URLs and proxy. `paired` runs one persistent-storage and one
fresh-storage observation per repetition. Both retain the configured fingerprint,
engine and viewport. Fresh storage is a control, not proof of independent sampling.
Research uses a separate temporary session so source browsing and re-search do not
modify the persistent observation profile's cookies/history.

Useful options:

- `--headed` shows the browser; `--engine chromium` uses stock Playwright Chromium.
- `--query-geography "Miami FL"` records explicit query geography separately from
  the requested city label. No city location is silently asserted or set.
- `--device mobile` uses mobile browser emulation and labels it accordingly. It is
  not a real-device measurement. Desktop is the default.
- `--candidate-url URL` adds a known review, service or website page. Repeat it as
  needed. Candidates are never promoted to Google citations just because they match.
- `--candidate-manifest candidates.json` reads objects with `url`, `kind`, and
  optional `entity_key`; kinds can describe review or service pages.
- `--aio-selector`, `--panel-selector`, `--location-selector` select observed
  containers if Google's layout does not match the built-in selectors.
- `--no-re-search` retrieves known citations/candidates without extra searches.
- `--no-research` saves the AIO evidence graph without opening source pages.
- `--max-interactions 24`, `--max-panel-scrolls 3`, `--max-documents 24`,
  `--max-claims 20`, `--max-queries 12`, `--results-per-query 3` bound work.
  Omitted or unavailable evidence is reported, not fabricated.
- `--reports-dir PATH` and `--history-db PATH` control output/history locations.
- `--allow-local-pages` is for trusted localhost fixtures only. Public document
  navigation rejects private addresses and non-HTTP(S) destinations by default.

CAPTCHAs, consent walls, failed navigation and unknown layouts are recorded as
capture outcomes. Call, booking, directions, save and share controls are not
clicked. The crawler opens only evidence-related UI and source documents.

## Read one chain

In **Claim chains**, choose a claim:

1. **Displayed claim:** exact text, business binding, commercial facts, offsets,
   captured container text and screenshot.
2. **Displayed citation:** its original destination, linked span, association
   scope and opening action. Inspect before/after screenshots and state JSON.
3. **Supporting passage:** retrieved text, URL, capture time, screenshot and DOM
   position. Each fact includes its source counterpart and comparison result.
4. **Candidates & re-search:** separately labeled source candidates and the exact
   queries issued by this tool.

**Competitor evidence** compares those facts against the client pages you supplied.
“Not found” means not found in that limited corpus. It does not prove the client
lacks the service or that the entire site lacks the information.

**History** compares previous successful observations in the same context cohort.
Persistent and fresh observations are kept separate. **Capture record** preserves
location display, browser context, errors, limits and all recorded states.

## Verification semantics

- `full_text_supported`: the full claim text occurs in a source passage, with the
  required business and geographic context matched. This is textual support, not
  proof that the factual assertion is true or current.
- `parts_supported`: specific commercial facts match; the entire claim is not
  certified. Values, units, qualifiers and negation are compared.
- `contradicted`: a matching fact value is expressed with opposite polarity.
- `different_value_or_scope`: comparable fact type but different value, units or
  qualifier. It requires review; the values could describe different services.
- `unresolved`: no sufficient supporting comparison was established.

Word overlap ranks candidate passages; it never by itself verifies a claim.
The current verifier is deterministic and conservative, with English/USD-oriented
fact rules. Unrecognized paraphrases, complex negation, ambiguous subjects and
other languages can remain unresolved. Page retrieval time is not a fact's
validity date. Linked support never proves which document generated the wording.

Exact DOM claim offsets use **UTF-16 code units** into the stored `container_text`.
Python fact offsets use **Unicode code points** and are separately labeled.
Original URLs are kept; canonical URL keys are only for comparison/deduplication.

## Record a client content change

```bash
python main.py --client client_001 \
  --record-change-url "https://your-business.example/lakeland/" \
  --change-note "Published verified dispatch terms and the date they took effect."
```

Use the same `--history-db`/`--reports-dir` as your observations. Subsequent reports
show the annotation next to observed changes. A content edit followed by a
citation change is not, by itself, a causal result.

Run the same collection command on later days to populate the history. This
package does not install a recurring task. All recorded timestamps use UTC.

## Run the browser demonstration again

```bash
python examples/run_demo.py
```

This starts a temporary local fixture server, makes two captures, retrieves their
source/client pages, records an edit, and generates a before/after history. If
needed, pass `--executable-path /absolute/path/to/chromium`.

The server stops after completion. Screenshots, snapshots, quotes and history
remain inspectable. Links to the temporary fixture website itself then stop
working, as expected; source screenshots remain in the package.

## Validation

```bash
python -m pytest -q tests
```

For a custom browser binary, set `AIO_TEST_CHROMIUM` to its absolute path. The
browser suite fails on capture or assertion errors; only the v1 test can explicitly
skip when the Playwright browser binary is absent. See `VALIDATION.md` for the
actual run and the live Google limitation in this environment.

The browser integration covers expanded drawers, nested additional sources,
entity panels, image destinations, claim offsets, branch identity, passage
verification, a mocked browser SERP for re-search, and session storage separation.
Tests also cover wrong business/location, different prices/time units, negation,
legacy replay and changes across dated history cohorts.

## Source references

- [Google generative summaries patent, FIG. 3](https://patents.google.com/patent/US11769017B1/en)
- [Google's published AI features guidance](https://developers.google.com/search/docs/appearance/ai-features)
- [Playwright browser contexts](https://playwright.dev/python/docs/api/class-browsercontext)
- [CloakBrowser documentation](https://github.com/CloakHQ/cloakbrowser)

Patents motivate the experiment. Captured UI and source passages supply the
observations. Course findings remain scoped to their reported examples.
