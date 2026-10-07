# Knowledge Studio 3.0 validation

Validated 2026-10-07 with Python 3.12 and headless Chromium 153.0.8010.0.

## Results

- 41 automated tests passed across the original tracer and new knowledge workflow.
- Real Chromium UI test: graph search and selection, audit, source-based draft,
  persisted save, owner review, imported page extraction, change history, source
  screenshot retrieval and 390px mobile layout.
- A separate desktop/mobile walkthrough captured the screenshots under `docs/`.
  No JavaScript page errors or horizontal document overflow were observed.
- Offline CLI-to-Studio integration: the archived Lakeland sample imported four
  claims into the persistent knowledge workspace. Missing citations stayed missing.
- SDK interface validation used google-genai 2.28.0 and neo4j 6.4.0.
- Gemini adapter tested with a fake client: structured output contract, passage
  budget, untrusted-source instruction and rejection of nonmatching source quotes.
- Neo4j adapter tested with a fake driver: parameterized writes, project-scoped
  replacement and preservation of unrelated workspace identities.
- Tests cover approval requirements, expired/wrong-locality draft facts, source
  quote offsets, term boundaries, schema identity mismatch, distinct price
  conditions, report deduplication, transactional rollback and stale-edit rejection.
- Browser tests cover hostile imported text and cross-origin request rejection.

## Visual check

The app extends the existing Stakeout evidence report's design: navy background,
lime actions, pale-blue links, system typography and bordered evidence panels.
The graph itself is code-native SVG. Desktop 1440x1050 and mobile 390x844 were
inspected using actual screenshots. Local Chromium/Playwright was used because
the app runs inside the execution workspace, not an existing user browser tab.

Checked navigation/copy, spacing, palette, typography, graph density, inspector
readability, editor controls and mobile behavior. Fixed crowded graph neighbors,
repeated visual edges, button wrapping and mobile graph legibility. Mobile uses
readable node buttons with the same inspector. No image-generated artwork is
required by this extension of the existing report design.

## Material limitations

No live credentialed Gemini or Neo4j operation was performed. Their adapters are
implemented and their interfaces tested, but account permissions, available
models and production connectivity require user configuration.

Live Google AIO capture remains unverified in this environment. The included
observations are explicitly synthetic browser fixtures or the original saved
Lakeland capture. No measured SEO uplift is claimed.

Owner confirmation is a recorded human attestation of the entire quote and its
scope. Automatic rules and model extraction may miss context or extract an
incorrect interpretation. There is no automatic publication, Google Knowledge
Graph submission, calibrated citation probability or access to model weights.

The UI server is local-only and single-operator. Deploying it publicly requires
an authentication, authorization and network-isolation design.
