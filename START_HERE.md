# AIO Tracer + Knowledge Studio

This package preserves the current aio_tracer_v2 project unchanged and adds
Knowledge Studio in aio_tracer/.

## Knowledge Studio
Open aio_tracer/START_HERE.html, or from that directory run:

    python3 graph_studio.py --demo

Then open http://127.0.0.1:8765.

Knowledge Studio contains the evidence graph, page audit/editor, Gemini and
Neo4j adapters, and the claim/citation/passage report pipeline documented in
its KNOWLEDGE_GRAPH.md. Its 41 tests passed.

## Existing dashboard
Your intervening dashboard and deployment work remains under aio_tracer_v2/,
with its own README.md and DEPLOYMENT.md. All its files are preserved exactly.

The two applications are separate. Knowledge Studio's automatic report import
is integrated with the tracer inside aio_tracer/, not with the separate
server/worker/dashboard in aio_tracer_v2/. Do not assume their databases or
report formats are interchangeable. No deployment settings were merged.

Credentialed Gemini/Neo4j calls and live Google capture remain unverified for
Knowledge Studio. Included demo observations are labeled synthetic.
