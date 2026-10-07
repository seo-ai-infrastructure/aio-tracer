# Handoff verification — October 7, 2026

## Passed

- **36 automated tests**, including browser fixtures; no skipped tests in the handoff run.
- Vite production build for the React dashboard.
- Browser workflow: load original historical trace, inspect claims, switch evidence, open comparison form, save experiment, queue capture, create daily tracking schedule, pause schedule.
- Desktop 1536px and mobile 390px checks: no page exceptions and no document-level horizontal overflow. Table/navigation areas scroll within their containers.
- Offline CLI with phone audit, check protocol, JSON/CSV output and persistent SQLite history.
- Browser fixture separates ordinary local results from a verified AIO, opens a citation drawer, maps its URL to the triggering claim, and records before/after screenshots.
- Denominator tests keep blocked and unverified legacy captures out of visibility rates.
- Evidence tests preserve the distinction between literal passage matches and unverified lexical candidates, including a negated claim case.
- Queue persistence, atomic claims, retries, schedules, source-input restrictions, auth, support reviews, and before/after experiment rates.

## Not verified against a live service

- Real Google production AIO layouts, locale/device variations, CAPTCHA behavior and proxy geography.
- CloakBrowser binary/options compatibility.
- Live external citation-page retrieval and robots behavior; the retrieval parser and workflow were tested with controlled snapshots.
- Neon Postgres connectivity and Railway/Vercel deployment. SQLite path and deployment source/config were verified locally.
- Windows startup script execution (the worker lock implementation is portable, but Windows was not available here).

No live ranking lift, model attention measurement, provenance recovery, or statistical significance is claimed.

The original sample remains a partial historical capture: four business cards, one decoded source chip and no complete claim-to-citation mapping. It is useful for inspecting extraction and phone mismatches, not for proving current visibility.

`requirements-tested.txt` records validation package versions. A FastAPI/Starlette test-client deprecation warning appeared; the suite passed. Browser fixtures used an available Chromium 153 executable via `AIO_TEST_BROWSER`.
