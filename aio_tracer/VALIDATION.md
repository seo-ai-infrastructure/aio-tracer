# Validation — 2026-10-06

- **27 tests passed**, including real Chromium browser interactions and source-page retrieval against a synthetic fixture.
- Replayed the original saved Lakeland capture: four businesses retained. Missing historical source drawers remain unresolved.
- Generated two complete demonstration captures with a changed price, citation destination and source passage. The second report contains the history comparison and recorded content edit.
- Checked the HTML explorer in desktop and 390-pixel mobile viewports: claim selection, before/after screenshots, competitor evidence and History operated without JavaScript errors or horizontal page overflow.
- The browser re-search parser was exercised using a mocked SERP served to the browser. These results are test data, not observed Google results.
- **Live Google validation is incomplete.** A fresh stock-Chromium navigation to the example Google search failed with `net::ERR_FAILED`. No live Google AIO, proxy city accuracy or production Google selector coverage is claimed from that run.
- CloakBrowser support is implemented through its persistent-context interface, but its live binary/proxy path was not validated here. Use an available licensed/free build as appropriate to the provider; no silent browser fallback is performed.

The exact acceptance path is exercised in `tests/test_evidence_v2.py::test_complete_browser_chain`: observed claim and offsets → visible citation control → opened source drawer (including extra sources) → browser-retrieved passage → fact checks. `examples/demo/START_HERE.html` provides a reviewable result and second-capture history.

The verifier performs deterministic commercial-fact comparisons. It is not a general semantic entailment model; complex phrasing, unfamiliar layouts or inaccessible passages can remain unresolved. No citation or passage match is treated as proof of original generation provenance.

No source publishing, outreach, recurring task installation or deployment was performed.
