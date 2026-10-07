# Vercel + Railway + Neon

## Local setup first

Run `start.sh` / `start.ps1`, load the historical sample and inspect it. Queue one real query in the target market. Check the observed Google location, AIO scope, drawer screenshots and citation associations before starting a wider tracking schedule.

## Neon

Create a Postgres database and put its SSL connection URL into `DATABASE_URL` on both Railway services. The application creates its tables on startup. Use a dedicated database or schema for this project. Never put the database URL in Vercel browser variables.

## Railway API

Deploy this folder using its Dockerfile. Set:

```dotenv
DATABASE_URL=your-Neon-Postgres-connection-URL
AIO_API_TOKEN=your-random-operator-token
AIO_ALLOWED_ORIGINS=https://your-frontend.vercel.app
AIO_DATA_DIR=/data
AIO_EMBEDDED_WORKER=0
```

Generate the operator token locally with `python -c "import secrets; print(secrets.token_urlsafe(32))"` and retain it privately. The default Docker command serves the API on Railway's `PORT`. Health endpoint: `/health`.

## Railway worker

Deploy the same image as a second service; override the start command with:

```bash
python worker.py
```

Set the same `DATABASE_URL` and `AIO_DATA_DIR`. Browser captures need persistent storage for profiles, cookies and screenshots. Mount a Railway volume at `/data` on the worker.

**Screenshot serving:** the API must have access to the same screenshot files. Separate Railway service volumes are not automatically shared. For the simplest deployment, run API and embedded worker in one Railway service with a single `/data` volume and `AIO_EMBEDDED_WORKER=1`. Separate services can share database evidence but require shared screenshot storage or a later object-storage adapter to serve images across services. Do not claim screenshot serving is configured merely because both services use `/data`.

The queue uses database claims to prevent two workers taking the same job. A browser profile uses a local file lock. Keep one capture worker per profile-volume deployment; file locks are not distributed across independent volumes. Expired 30-minute job leases become errors and require manual retry.

## Proxies / devices

If needed, set a city proxy on the worker as `AIO_PROXY_LAKELAND`. Enter `LAKELAND` as the named proxy profile in the capture form. Credentials are never sent by the frontend. Desktop/mobile profiles are separate. Supply browser coordinates if useful, but inspect Google's displayed location rather than assuming coordinates or proxy label control it exactly.

CloakBrowser is optional. Install `requirements-cloak.txt` and its compatible browser binary if selecting it. Default Docker installs Playwright Chromium. Optional Cloak launch behavior was not verified in this handoff.

## Vercel frontend

Set root directory to `dashboard`:

- Install: `npm ci`
- Build: `npm run build`
- Output: `dist`
- Build environment: `VITE_API_BASE_URL=https://your-api.up.railway.app`

Set `AIO_ALLOWED_ORIGINS` on Railway to the exact frontend origin. Rebuild Vercel after changing its API URL. Open **Operator access** in the dashboard and enter the token. Tokens remain in tab session storage. This grants full operator access; client labels are filters, not separate tenant permissions.

## Tracking and experiments

Create daily/weekly schedules only after one live capture validates the layout and geography. Keep a matching device/query/market series and at least one unchanged control query. Archive content changes in Experiments with real timestamps. The tool reports descriptive changes; it does not establish statistical independence or causal lift.

## Operational limits

- Browser CAPTCHA/consent/layout failures remain explicit and excluded from rates.
- Source fetch retrieves public raw HTML/text, not authenticated or fully rendered web pages.
- HTTP/robots audits do not prove indexing; use Search Console URL inspection independently.
- Snapshot/page text is stored in the database; screenshots/profiles need disk persistence.
- This handoff did not deploy Railway/Vercel/Neon or verify real Google production layouts.
