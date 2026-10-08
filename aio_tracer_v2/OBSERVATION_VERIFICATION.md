# Observation upgrade verification — October 7, 2026

- Dashboard/API: 51 tests passed, no skips. Existing behavior plus parser, redaction, lossless IDs, offsets, cohort separation, HAR import and source snapshot comparisons.
- Knowledge Studio/report pipeline: 43 tests passed, no skips. Its existing graph/editor/provider workflows plus observation integration and graph coverage.
- Total: 94 tests passed.
- Real Chromium desktop and mobile synthetic delayed-response tests verified UTF-8/HTML parsing, later source-metadata delivery, rendering events, cache headers and declared interactions.
- The sample intentionally retains unknown first-text timing where the initial prefix was buffered. Later source-metadata timing was captured; no timestamp was fabricated.
- Vite production build passed. Browser UI checks visited every observation tab and exercised new capture controls at desktop and 390px mobile widths; no page exceptions or horizontal document overflow.
- Synthetic samples are excluded from SEO metrics.
- A live Google attempt returned net::ERR_EMPTY_RESPONSE and was recorded as an error, not an AIO absence. Production Google selector coverage and framing variants remain unverified.
- CloakBrowser live compatibility, configured proxies, Google location accuracy, Neon and Railway/Vercel deployment were not verified in this upgrade.
- No model identity, backend cache status, parametric generation ratio, hidden safety thresholds or listener-binding state was inferred.
- Semantic similarity remains explicitly unset. Snapshot text/number checks are conservative and require review. The existing Knowledge Studio deterministic verifier remains available.
- One existing FastAPI/Starlette test-client deprecation warning appeared; tests passed.

See ../OBSERVATION_HARNESS.md for capture limits, instructions and interpretation.
