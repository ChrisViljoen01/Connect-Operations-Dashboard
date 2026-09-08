# Connect Logistics Operations Dashboard — OPUS

Standalone NiceGUI extraction and operational analytics application for OPUS
Transport Allocation jobs and their complete ORDBULK-linked workflow data.

This project has its own Python environment, PostgreSQL database, application
port, assets, and configuration. It does not import or write into the Connect
Logistics YMS or Electricity applications.

## Local environment

The project targets Python 3.13 and uses `.venv`:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### Canonical PyCharm workspace

Open the exact Git worktree that contains the changes under review; do not run the
application from another checkout of this repository. The committed run
configurations resolve the interpreter and working directory from `$PROJECT_DIR$`,
so every run uses that worktree's `.venv` and source files.

Before reviewing or running changes, use:

```powershell
.\scripts\workspace_status.ps1
```

The reported `ProjectRoot` and `GitRoot` must match the folder open in PyCharm.
Keep changes uncommitted while reviewing them, then commit from that same worktree.
If another Copilot chat is needed, coordinate its changes back into the canonical
session instead of running its separate worktree.

The application reads the local PostgreSQL password from Windows Credential
Manager target `ConnectLogisticsOps/PostgreSQL/connect_ops_app`. An injected
`OPUS_DB_APP_PASSWORD` environment variable can be used in deployment.

The OPUS source login is configured from **Extraction** in the app.
After OPUS verifies the login, it is stored separately in Windows Credential
Manager at `ConnectLogisticsOps/OPUS/app.opus4business.com`. The password is
never stored in PostgreSQL, `.env`, or Git.

## Run

```powershell
.\.venv\Scripts\python.exe .\main.py
```

Open <http://127.0.0.1:8091>.

The extraction baseline starts on **29 June 2026** and advances through the
current date. A job reference qualifies only when OPUS returns a Transport
Allocation checklist created inside that inclusive window. The selected
Transport Allocation is the workflow root; every later linked checklist is
loaded chronologically, including newly discovered checklist types. Bulk Import
for Minerals Transport Allocation is always excluded.

Current-day scans start every two minutes. A separate full-baseline active-status
sweep runs every four minutes, while historical answer audits run hourly. Changed
jobs are prioritized in batches of at most 10 detail bundles, and a seven-day
persistent backlog keeps deferred work detectable without making every live cycle
perform a historical scan. The start date, intervals, lookback, detail and audit
batch sizes, request timeout, retry count, and retry backoff can be changed
through the variables in [.env.example](.env.example).

Each checklist endpoint is expanded through section detail so question labels,
question help text, submitted answers, comments, images, selected items, child
checklists, and table payloads are retained separately. Raw versions remain in
`ingest.raw_records`; normalized checklist
instances and answers are stored in `ops.checklist_instances` and
`ops.checklist_answers`, linked to `ops.jobs` and `ops.allocations` by the
`ORDBULK-*` job reference.

Detail extraction uses bounded parallelism both across jobs and within a
checklist's section requests. Tune `OPUS_DETAIL_WORKERS` and
`OPUS_SECTION_WORKERS` in `.env.example` for the source system's rate limits;
the defaults favor throughput without creating an unbounded request storm.

The application has five operational surfaces:

- **Extraction** retains credentials, phase-aware sync telemetry, run history,
  source errors, checklist audit tables, and full answer drill-down.
- **Data** provides checklist/status/root-date/reference filters, clickable
  status KPIs, bounded checklist-job pages, chronological workflow attempts,
  full detail drill-down, and filtered or complete XLSX exports.
- **Ops Dashboard** derives trucks in transit from strict latest-attempt workflow
  rules and shows explicit origin-to-destination routes, fallback flags, and a
  detailed truck register.
- **Stock on Hand** keeps origin point, destination point, and slab/bay as
  separate dimensions; reports origin and destination balances independently;
  and reconciles route, order, truck, and delivery variance.
- **SOH by Order** provides a searchable OPUS order-number filter and rebuilds
  the selected order's stock, routes, from/to bays, daily activity, movements,
  and exceptions from linked Loading and Exit and Offloading and Exit facts.
  Each order's actual checklist points define source, intermediate, and
  destination locations. Source-only dispatches remain visible but are excluded
  from SOH; destination and intermediate balances are counted once. Missing
  intermediate openings or receipts are shown as unresolved instead of being
  reported as valid stock. Every selected order has a detailed XLSX export;
  governed KFTS studies also retain their existing management PDF.

OPUS currently stores Nett Weight as whole-number kilograms (for example,
`36750`). Analytics retain that source answer and report `36.750` metric tonnes.
Unexpected or missing values remain visible as exceptions.

Existing databases must apply migrations through 009 in order. New databases
receive all migrations through `scripts/setup_database.ps1`. Baseline rebuilds
use the separate, explicit `scripts/reset_database.ps1 -ConfirmReset` command;
normal migrations do not silently clear current operational data.

## Test

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Database setup and the storage model are documented in [db/README.md](db/README.md).

## Deployment (Docker / a public link)

The Windows workflow above (`.venv` + Windows Credential Manager) remains the
primary supported setup. For running the dashboard somewhere other than a
Windows workstation — a shared server, a VM, or a GitHub Codespace so it is
reachable by link — a self-contained Docker Compose stack is included:
`Dockerfile`, `docker-compose.yml`, and `scripts/setup_database.sh` (a Linux
port of `setup_database.ps1`). It provisions PostgreSQL, applies every
migration automatically, and starts the app.

```bash
cp .env.example .env   # fill in OPUS_DB_APP_PASSWORD and OPUS_PG_ADMIN_PASSWORD
docker compose up -d --build
```
Open `http://<host>:8091`. Two settings matter specifically for non-Windows
hosting:

- **`OPUS_SOURCE_EMAIL` / `OPUS_SOURCE_PASSWORD`** — Windows Credential
  Manager does not exist in a container, so the OPUS source login falls back
  to these environment variables when set. Entering the login through the
  Extraction screen still works and is tried first wherever Credential
  Manager is actually available.
- **`OPUS_APP_ACCESS_PASSWORD`** — the app has no login screen by default,
  which is fine on a trusted internal network but not once it is reachable
  by a public link. Setting this enables a `/login` gate (shared password,
  timing-safe check) in front of every page. Leave it unset only for
  internal-network deployments. `OPUS_APP_STORAGE_SECRET` signs session
  cookies and is derived automatically from the access password if not set
  explicitly; set it explicitly for a stable, long-lived deployment.

### Running a published image (for testers)

Every push to `main` publishes a ready-to-run image to GitHub Container
Registry, so testers do not need to clone or build anything:

```
ghcr.io/chrisviljoen01/connect-operations-dashboard:latest
```

Pull it, or use the Compose stack with the published image instead of a local
build by overriding the `app` service:

```bash
docker compose pull
docker compose up -d
```

Tagged releases (`v1.2.3`) also publish `1.2.3`, `1.2` and short-SHA tags, so
a specific build can be pinned for testing. The package is listed under the
repository's **Packages** section on GitHub.

### Getting an actual shareable link

Building the container image does not, by itself, put a URL on the internet —
that requires a host. Two practical options:

1. **GitHub Codespaces** (`.devcontainer/devcontainer.json` is included) —
   open this repository as a Codespace, let the Compose stack start, then use
   the **Ports** panel to set port `8091`'s visibility to *Public* to get a
   shareable `https://…app.github.dev` URL. Set `OPUS_APP_ACCESS_PASSWORD`
   first if you do this, since the port is then open to anyone with the link.
2. **Your own server or cloud VM** — run the `docker compose up -d --build`
   command above on any Docker-capable host you control, then point a domain
   or reverse proxy (for TLS) at port 8091.

Neither this repository nor an automated session can provision cloud hosting
or a tunnel on your behalf without credentials for that provider; the steps
above are what turns the container into a link once you choose where it runs.
