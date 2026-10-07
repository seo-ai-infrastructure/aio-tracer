# Knowledge Studio — AIO Tracer 3.0

A local, single-operator app for inspecting business facts, reviewing evidence,
editing page drafts, and connecting those facts to observed AIO citations.

## Open it

Install Python 3.11 or newer. Extract the entire ZIP. Open a terminal in the
`aio_tracer` directory and run:

```bash
python3 graph_studio.py --demo
```

On Windows, use `python` in place of `python3` (or run `Start_Knowledge_Studio.bat`).
Open **http://127.0.0.1:8765**. The graph, local extraction, evidence review,
audit and editor work without API keys or third-party Python packages.
`Start_Knowledge_Studio.command` is a macOS launcher; use the terminal command
if the OS prevents opening an unsigned downloaded script.

The demo is explicitly synthetic. Its two AIO captures came from the existing
browser fixture, not live Google. It includes changed claim text, displayed
citations, supporting and mismatching passages, and a deliberately incomplete
client page. Adding `--demo` again does not duplicate an existing demo project.

For an empty workspace:

```bash
python3 graph_studio.py --workspace my-client-data
```

The SQLite database lives in that workspace. Stop the server before copying the
whole workspace as a backup. `--port 8766` selects another local port. The server
binds to 127.0.0.1 only; this is not an authenticated multi-user SaaS deployment.

## Complete workflow

1. **New project:** enter a business name, locality, service vocabulary and optional
   topics. Put one service/topic on each line; separate aliases using `|`.
   These describe audit targets, not verified business capabilities.
2. **Add evidence:** enter the page URL and paste text or import HTML. Alternatively,
   capture the public page through a browser (setup below). Choose Client,
   Competitor or Research candidate. Each snapshot retains its provenance.
3. **Extract assertions:** select a page and use local rules or Gemini. Local rules
   find English/USD-oriented commercial facts and configured term occurrences.
   Gemini extracts broader assertions but does not approve them. Add a fact
   manually when neither extractor recognizes it.
4. **Facts & evidence:** inspect the exact quote. Resolve business, service,
   geography, value, conditions, polarity and effective dates. Owner confirmation
   requires the reviewer, a note, and explicit confirmation of **every assertion
   in the entire quoted passage and its structured scope**. This is a human
   attestation, not an independent proof of truth. Rejected facts remain recorded.
5. **Page audit & editor:** choose the target page, service and optionally a topic.
   Inspect missing identity/geography, unclear relationships, ambiguous prices or
   availability, warranty terms, expired facts and scoped conflicts. Checks cover
   the supplied snapshot and configured vocabulary; findings need editorial review.
6. Select current owner-confirmed facts whose locality matches the project and **Build from selected quotes**. Edit
   the draft, save it and export Markdown with evidence references. Exact quotes
   have `source_quote_draft` status. Changed wording has `edited_requires_review`
   status. No website is published by this action.
7. Publish the reviewed content through your normal CMS, then **Record published
   change**. This records your report of a change; it does not confirm recrawling.
8. Import later tracer `report.json` files. The graph preserves observed citations
   separately from passage comparisons. History shows the tracer's comparable
   observation changes plus the workspace's content/review events.

Graph nodes include Business, Service, Topic, QueryTerm, Location, Page, Passage,
Fact, AIOClaim, CitationURL and Observation. An entity's original KGID/CID remains
available. Use **Link this entity to the client** in the business inspector only
when its identity/branch has been checked; it records an explicit equivalence
relationship with reviewer evidence rather than merging identifiers by name.

The graph view shows up to eight neighbors to stay readable. Search reaches all
nodes. Select nodes with a mouse or keyboard; inspect the full relationship
records, citation scope, offsets and checks in the inspector.

## Browser page capture

```bash
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
python3 graph_studio.py --engine chromium
```

Use `--engine cloak` if you have a working CloakBrowser installation. An explicit
`--executable-path /absolute/path/to/chromium` is supported. Browser captures use
fresh storage and the existing navigation guards; blocked/error outcomes are
reported. HTML imports are parsed as text and never executed. Browser page
capture stores screenshots/HTML/JSON under the workspace's `artifacts` directory.
This release does not guarantee live Google access or a particular perceived city.

## Gemini extraction (optional)

```bash
python3 -m pip install -r requirements-graph.txt
```

Configure these in the environment of the terminal launching the server:

- `GEMINI_API_KEY`: your Gemini Developer API key.
- `GEMINI_MODEL`: a text model available to your account supporting JSON structured
  output. Model selection is explicit; the application does not silently substitute.

For example, macOS/Linux:

```bash
export GEMINI_API_KEY="YOUR_KEY"
export GEMINI_MODEL="YOUR_AVAILABLE_MODEL_ID"
python3 graph_studio.py
```

Windows PowerShell uses `$env:GEMINI_API_KEY="YOUR_KEY"` and
`$env:GEMINI_MODEL="YOUR_AVAILABLE_MODEL_ID"` before `python graph_studio.py`.

For Google Cloud with Application Default Credentials instead of an API key:

```bash
gcloud auth application-default login
export GOOGLE_GENAI_USE_VERTEXAI=true
export GOOGLE_CLOUD_PROJECT="YOUR_PROJECT_ID"
export GOOGLE_CLOUD_LOCATION="global"
export GEMINI_MODEL="YOUR_AVAILABLE_MODEL_ID"
python3 graph_studio.py
```

Enable the model service and billing in your own project. The installed SDK also
supports `GOOGLE_GENAI_USE_ENTERPRISE`; use the mode provisioned for your account.
Remove conflicting API-key/provider variables when switching modes.

A Gemini extraction sends only the selected page's passages: at most 80 passages,
60,000 source characters and 8,000 output tokens per request. Omitted passage
counts and rejected facts appear in the extraction event in Change history.
This is a paid provider call under your account. The adapter supplies no browsing
or execution tools to the model. Source instructions are untrusted data; every
returned quote must occur exactly in the selected passage. Semantic interpretation
still needs human review. API errors never echo credentials or raw provider errors.

## Neo4j (optional)

Use an existing Neo4j instance or create one using Neo4j's documented setup.
Set these server environment variables:

```bash
export NEO4J_URI="neo4j+s://YOUR_INSTANCE.databases.neo4j.io"
export NEO4J_USERNAME="neo4j"
export NEO4J_PASSWORD="YOUR_PASSWORD"
export NEO4J_DATABASE="neo4j"
python3 graph_studio.py
```

Open **Connections & exports → Sync to Neo4j**. The adapter uses the official
Python driver, parameterized Cypher, a workspace/id constraint and one transaction
for graph writes. It replaces the mirrored snapshot of the selected workspace;
other workspaces are untouched. SQLite remains the authoritative local project
and history store. Sync is explicit, not automatic.

All nodes use `:EvidenceNode` with `kind`, `label`, `workspace`, `id`, and a full
`data_json` record. Edges use `:RELATES` with `relation`, `workspace`, `id`, and
`data_json`. For example:

```cypher
MATCH (c:EvidenceNode)-[r:RELATES]->(u:EvidenceNode)
WHERE c.workspace = $workspace AND r.relation = 'DISPLAYED_CITATION'
RETURN c.label AS claim, u.label AS citation, r.data_json AS evidence;
```

Export Neo4j parameters without a connection if you want to inspect the payload
first. No model-generated Cypher is executed. The provider code lives in
`knowledge/providers.py`. Neo4j GraphRAG can be layered on this explicit graph later;
this release uses a constrained custom extractor and the official Neo4j driver.

## Connect the tracer automatically

Find the project ID in **Connections & exports → Export project evidence**.
Add the following to your existing `main.py` command:

```bash
--knowledge-workspace knowledge_data --knowledge-project YOUR_PROJECT_ID
```

The workspace path must match the Studio workspace. Each finished report is
imported after the JSON/HTML/CSV outputs have been written. An identical
observation ID does not count as an additional sample. Re-analysis of an already
imported observation is ignored; import the original complete research report.

Alternatively, use **Add evidence → Import observation** to import a report
manually. Raw v1 traces must first be replayed with `main.py --trace-file ...`.
Missing historical drawers or citations remain missing; the upgrade cannot
reconstruct observations that were never captured.

## Interpretation and limits

- Target services/topics, keyword aliases and a project city are configuration.
- A lexical mention is not proof of an offer or an accurate relationship.
- A model extraction is a source assertion, initially `needs_review`.
- Owner confirmation records a named human's attestation of the whole quote.
- A displayed citation requires an imported captured UI relationship.
- Passage support describes a comparison with retrieved text, not generation origin.
- Topic coverage, salience and token counts are not Google ranking probabilities.
- The local graph is not submitted to Google's Knowledge Graph.
- Drafts are saved locally and exported; CMS publishing is not connected.
- Client/competitor documents and old/new snapshots remain separate.
- Automatic audits use bounded English rules; implicit relations and paraphrases
  can remain unresolved. Exact quote absence does not establish semantic absence.
- Private source quotes are sent to Gemini only when that extraction is selected.
- Credentials and production provider calls were not available during this build.
  Adapter contracts were tested with fakes; live integration remains to be validated
  with your accounts. Live Google AIO capture also remains unverified here.

## What was preserved

This package includes the prior v2 capture/citation/history pipeline and its
browser demonstration. The separately uploaded v1.1 package is preserved intact
under `legacy_v1_1/`, including its original CLI and audit modules. Run its own
`main.py` from that directory if you need its old flags. Its heuristic percentages
remain legacy output and are not used as evidence or ranking probabilities here.

## References

- https://googleapis.github.io/python-genai/
- https://ai.google.dev/gemini-api/docs/structured-output
- https://neo4j.com/docs/python-manual/current/transactions/
- https://neo4j.com/docs/neo4j-graphrag-python/current/
- https://developers.google.com/search/docs/appearance/ai-features
