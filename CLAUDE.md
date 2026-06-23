# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

**QuantDinger** — a self-hosted, local-first quantitative trading platform. This repository ships the **Python Flask backend**, **Docker Compose stack**, **Postgres schema**, **MCP server** (separate `mcp_server/` package), **strategy guides**, and all docs. The web UI source lives in a separate, source-available **QuantDinger-Vue** repo and is consumed as a prebuilt `ghcr.io/brokermr810/quantdinger-frontend` image — no frontend source/Node.js is checked in here. The companion **`mcp_server/`** directory is published to PyPI as `quantdinger-mcp`.

Read the root `README.md` for the user-facing product story and install paths; treat the rest of this file as the developer/agent contract.

## Layered agent workflow (read first for non-trivial work)

For any non-trivial backend / strategy / docs change, follow `.cursor/skills/quantdinger-agent-workflow/SKILL.md`. Key red lines:

- **Never commit secrets, production `.env`, or real API keys.** Use the `env.example` pattern with placeholders.
- **Do not bypass live-trading safety.** Agent tokens are paper-only by default; live execution requires `paper_only=false` on the token **and** `AGENT_LIVE_TRADING_ENABLED=true` on the server. Do not weaken this without an explicit ask.
- Keep all **`docs/agent/*` English-only** so the same material works across locales and tools.

## Repository layout (high signal only)

```
backend_api_python/          Flask + Gunicorn API (Python 3.10+; Dockerfile uses 3.12-slim)
  app/__init__.py            create_app() factory, SafeJSONProvider, background-worker bootstrap
  app/routes/                HTTP blueprints — thin handlers only; exchange logic does NOT belong here
    agent_v1/                Agent Gateway (scoped qd_agent_*** tokens, audit-logged) at /api/agent/v1
    *.py                     Legacy Flask Blueprints; flask-smorest migration is incremental (see openapi.register)
  app/openapi/                flask-smorest wiring + exported spec helper; new public routes go here first
  app/services/              Business logic
    live_trading/            Exchange clients (binance/okx/bitget/bybit/gate/htx/coinbase/kraken), contracts, capability matrix
    grid/                    Grid bot runtime, resting orders, fill sync, ledger reconciliation
    pending_orders/          Reusable live-order phases; live_order_phases.py holds venue-specific quirks
    pending_order_worker.py  QUEUE CONSUMER — legacy hot spot, see Refactoring rules below
    trading_executor.py      Realtime strategy loop — legacy hot spot, see Refactoring rules below
    backtest.py              Historical simulation — legacy hot spot
    strategy.py + strategy_compiler.py / strategy_script_runtime.py
                            Two strategy runtimes: IndicatorStrategy (dataframe buy/sell) + ScriptStrategy (on_bar + ctx.buy/sell)
    llm.py                   Multi-provider LLM (OpenAI/OpenRouter/AtlasCloud via litellm)
  app/data_sources/          Market data adapters (factory.py registers them)
  app/data_providers/        Aggregated providers (heatmap, sentiment, opportunities, economic calendar)
  app/utils/                 Infrastructure helpers: db_postgres (pool), safe_exec (sandbox for user indicator code), agent_auth, agent_jobs
  app/config/                Env-driven settings (MetaConfig), api_keys, data_sources config
  migrations/init.sql        Postgres schema; auto-applied idempotently on backend startup
  scripts/                   One-off ops scripts + quality baselines (NOT auto-loaded)
  tests/                     Pytest suite (~130 files). conftest.py sets minimal env + SKIP_STARTUP_HOOKS=1
  gunicorn_config.py         Production WSGI: 1 worker × 4 threads gthread; do NOT preload_app

mcp_server/                  Published as PyPI package `quantdinger-mcp`. Thin MCP wrapper over Agent Gateway R/W/B endpoints
  src/quantdinger_mcp/       Stdio + SSE + streamable-http transports
  pyproject.toml             uvx / pipx installable; depends on the Agent Gateway, not the backend directly

docs/                        Product / strategy / agent / deployment docs. Strategy guides live here.
  agent/                     Agent Gateway + MCP docs (English-only). agent-openapi.json is the SSOT machine contract.
  api/                       Human Web API OpenAPI yaml + ReDoc viewer
  STRATEGY_DEV_GUIDE*.md     EN/CN/TW/JA/KO strategy authoring guide
  examples/                  Working indicator / strategy examples

scripts/                     Repo-level utility scripts (i18n tooling for QuantDinger-Vue-src, version bump, secret-key generator)
docker-compose.yml           Frontend via GHCR image; backend built from ./backend_api_python
docker-compose.ghcr.yml      Both services pulled from GHCR (zero-clone deploy — used by install.sh)
docker-compose.build.yml     Override file: builds frontend from ./QuantDinger-Vue/ source (gitignored)
install.sh                   One-line curl|bash installer for end users (uses docker-compose.ghcr.yml)
.github/workflows/
  basic-ci.yml               Python syntax + import + docker compose validate + version consistency (no live tests)
  openapi-ci.yml             Exports spec, Spectral-lints both specs, oasdiff breaking-change check
  docker-publish.yml         On `v*` tag: builds/pushes multi-arch backend to ghcr.io (frontend releases from Vue repo)
```

## Common commands

All commands assume the working directory shown. `Docker Compose v2` (`docker compose`, not `docker-compose`) is required.

### One-time setup

```bash
# Copy env template (run.py auto-loads it)
cp backend_api_python/env.example backend_api_python/.env

# Generate a real SECRET_KEY before first boot (refuses to start otherwise)
./scripts/generate-secret-key.sh     # Linux/macOS
# or PowerShell: scripts/generate-secret-key.ps1
# or: python -c "import secrets; print(secrets.token_hex(32))"
```

### Run the full stack

```bash
docker compose pull && docker compose up -d            # production-ish
docker compose up -d --build backend                  # after editing backend code
docker compose logs -f backend
docker compose restart backend
```

Web UI: `http://localhost:8888`. API: `http://localhost:5000` (bound to `127.0.0.1` by default — override with `BACKEND_PORT` in repo-root `.env`). Default admin: `quantdinger / 123456` (change immediately).

### Run backend locally (no Docker)

```bash
cd backend_api_python
python -m venv .venv && source .venv/bin/activate   # .venv\Scripts\activate on Windows
pip install -r requirements.txt
cp env.example .env  # set SECRET_KEY, DATABASE_URL (point at a Postgres you run locally)
python run.py        # serves on :5000 with auto-reload
```

Notes: `run.py` early-loads `.env`, sets `TQDM_DISABLE=1`, and applies `PROXY_URL` (with a Chinese-domestic-domain NO_PROXY list). It refuses to boot in production (`DEBUG=False`) while `SECRET_KEY` still equals the placeholder — in dev it generates a random key for the session and prints a tip.

### Tests

```bash
cd backend_api_python
python -m pytest tests/ -q                          # whole suite (uses conftest env defaults)
python -m pytest tests/test_agent_v1.py -q          # Agent Gateway auth/scope/audit
python -m pytest tests/test_openapi.py -q           # smoke checks against the exported spec
python -m pytest tests/test_grid_engine.py -q       # a heavy one (~39 KB); safe to skip in tight loops
```

`conftest.py` sets `SKIP_STARTUP_HOOKS=1`, `CACHE_ENABLED=false`, dummy `SECRET_KEY`/`ADMIN_*` so tests never touch Postgres/Redis/strategy workers. `pytest.ini` defines one marker: `integration` (live exchange API smoke tests — opt-in, requires your own testnet keys).

### OpenAPI / spec maintenance

```bash
cd backend_api_python
python scripts/export_openapi.py                   # writes ../docs/api/openapi.yaml (SSOT)
python scripts/export_openapi.py --format json -o /tmp/openapi.json
# CI in .github/workflows/openapi-ci.yml:
#   - diffs the generated spec vs committed
#   - spectral lint (human spec + agent spec)
#   - oasdiff breaking-change check
#   - pytest tests/test_openapi.py
```

If you change a route in `app/routes/` (legacy blueprint) or `app/openapi/routes/`, re-export and commit the updated `docs/api/openapi.yaml`. Agent Gateway changes also require editing `docs/agent/agent-openapi.json` by hand.

### Backend quality / exchange smoke tests

```bash
cd backend_api_python
python scripts/backend_quality_check.py                                    # structural regression guard vs scripts/backend_quality_baseline.json
python scripts/exchange_smoke_test.py --offline-contracts                  # API-key-free contract tests (use tests/fixtures/exchanges/*.json)
# Live API tests require --allow-orders AND EXCHANGE_SMOKE_ALLOW_ORDERS=1
```

### Version bumping

`scripts/check_version.py` (run by CI) verifies the repo-root `VERSION` file matches every tracked version constant. `scripts/bump_version.py` walks them — run it with the full `QuantDinger-Vue-src/` checkout present so it can sync the frontend constants too.

### MCP server (separate package)

```bash
cd mcp_server
pip install -e .
# stdio for desktop IDEs:
QUANTDINGER_BASE_URL=http://localhost:8888 QUANTDINGER_AGENT_TOKEN=qd_agent_xxx quantdinger-mcp
# remote:
QUANTDINGER_MCP_TRANSPORT=streamable-http QUANTDINGER_MCP_HOST=0.0.0.0 QUANTDINGER_MCP_PORT=7800 quantdinger-mcp
```

`mcp_server/` wraps Agent Gateway R/W/B endpoints only (no `quick-trade/*`). New MCP tools must be backed by a REST endpoint first.

### i18n tooling (for QuantDinger-Vue-src)

`scripts/i18n-*.js` operate on the gitignored `QuantDinger-Vue-src/` clone. Anthropic is the default translation provider (`ANTHROPIC_API_KEY`); see `scripts/README.md` for `--provider` options and dry-run flags. These scripts do not affect the backend.

## Architecture (the big picture)

Two API surfaces, registered from a single Flask app (`app/routes/__init__.py` → `init_openapi` + `register_agent_v1`):

- **Human Web API** — `/api/...` and friends, JWT-authenticated, mounted via flask-smorest. Old modules use plain Flask Blueprints under `app/routes/*.py`; the migration to flask-smorest (driven by `app/openapi/`) is incremental. The exported spec (`docs/api/openapi.yaml`) is the SSOT — update it whenever you add or change a route.
- **Agent Gateway** — `/api/agent/v1/...`, agent-token (`qd_agent_***`) authenticated, **never** JWT, hand-maintained spec at `docs/agent/agent-openapi.json`. Tokens are hashed at rest in `qd_agent_tokens`; every call (success or denial) is appended to `qd_agent_audit`. Scope classes: `R` (read), `W` (workspace write), `B` (backtest/experiment), `N`, `C`, `T` (trading — paper-only by default). Async jobs are persisted in `qd_agent_jobs`; clients poll `/jobs/{id}` or subscribe to `GET /jobs/{id}/stream` (SSE: `snapshot`/`progress`/`ping`/`result`). Auth helper: `app/utils/agent_auth.py` (`@agent_required(scope=...)`). Secret redaction lives in `app/routes/agent_v1/_security.py` (512 KiB cap on indicator source; known credential keys masked in JSON).

Strategy and execution pipeline:

```
data_sources/* (factory.py)  →  data_providers/* (aggregators)
       ↓
IndicatorStrategy (dataframe buy/sell)  /  ScriptStrategy (event-driven on_bar + ctx.buy/sell)
       ↓
   backtest.py (historical)  OR  trading_executor.py (realtime loop, legacy hot spot)
       ↓
   pending_order_worker.py (queue consumer, legacy hot spot)
       ↓
   services/pending_orders/live_order_phases.py (venue-specific quirks)
       ↓
   services/live_trading/*.py (per-exchange clients) + services/broker_* (IBKR/MT5/Alpaca)
       ↓
   records + ledger + notification (Telegram/email/SMS/webhook) + audit
```

User-authored indicator/strategy Python is sandboxed by `app/utils/safe_exec.py` (strict builtin whitelist, `SAFE_IMPORT_MODULES` only, `_DANGEROUS_METHOD_NAMES` blocked on any receiver — includes pandas `read_*`/`to_*`, numpy IO, `eval`/`query`, frame introspection).

User data isolation is by `user_id` column on tenant tables; roles are `admin > manager > user > viewer`. The `admin` role can issue and audit agent tokens for any tenant.

## Adding or modifying things — where they go

- **New exchange for live trading:** create `app/services/live_trading/<name>.py` inheriting `BaseLiveTrading`; register in `live_trading/factory.py`; add a capability row to `live_trading/capabilities.py`; add fixtures in `tests/fixtures/exchanges/order_fill_contracts.json` (+ `position_contracts.json` if derivatives); add tests in `test_exchange_order_param_contracts.py`; run the smoke + quality checks above.
- **New data source:** `app/data_sources/<name>.py` with `get_ticker`/`get_kline`; register in `data_sources/factory.py`. If it should appear on the global dashboard, add a fetcher in `data_providers/` and wire it into the fallback chain.
- **New public HTTP route:** prefer flask-smorest under `app/openapi/routes/` + a schema under `app/openapi/schemas/`; register it in `app/openapi/register.py` and update `docs/api/openapi.yaml` via `scripts/export_openapi.py`. Read `docs/API_CONVENTIONS.md` first (envelope shape `{"code":1,"msg":"success","data":{...}}` for human endpoints; agent endpoints use `{"code":0,"message":...}` with `details`/`retriable`). Visibility tiers: Public (default), Internal (`x-visibility: internal`), Private (`x-visibility: private`).
- **New agent route:** put it in `app/routes/agent_v1/`, gate with `@agent_required(scope=...)`, update `docs/agent/agent-openapi.json` by hand, and decide whether the MCP server should also expose it (`mcp_server/src/quantdinger_mcp/`).
- **New broker adapter (stocks/forex):** `app/services/<ibkr_trading|mt5_trading|alpaca_trading>/client.py` (each subpackage has its own README).
- **New strategy example:** drop it into `docs/examples/` and link from the relevant `STRATEGY_DEV_GUIDE*.md` rather than duplicating guide text.

## Refactoring rules (from `backend_api_python/docs/backend_architecture.md`)

- Do not add new large `isinstance(client, ExchangeClient)` blocks in routes.
- Do not add new exchange support by only patching `pending_order_worker.py`.
- Do not swallow trading-core exceptions with `except Exception: pass`; mark the order failed, log enough context, or document why the exception is safe.
- Keep fixtures updated before touching live trading paths.
- Prefer small pure parsers for response normalization — they are easy to test without API keys.

Known legacy hot spots (tracked by `backend_quality_baseline.json`; do not let them grow): `trading_executor.py`, `pending_order_worker.py`, `services/backtest.py`, `routes/quick_trade.py`, `routes/strategy.py`.

## Troubleshooting quick refs

- **Backend exits immediately:** `SECRET_KEY` is still the example placeholder, or `.env` has invalid syntax. `docker compose logs backend`.
- **Blank UI / API errors from browser:** `FRONTEND_URL` mismatch or API not reachable on the host the browser opened. Default API bind is `127.0.0.1:5000` — override via repo-root `.env` `BACKEND_PORT`.
- **Slow `docker pull`:** add `IMAGE_PREFIX=docker.m.daocloud.io/library/` to repo-root `.env` or configure Docker Desktop proxies.
- **"apikey parameter is incorrect" from Twelve Data:** `TWELVE_DATA_API_KEY` missing in `.env`. Chinese stock data requires a paid plan.
- **Heatmap "暂无数据":** usually NaN/Inf leaking from yfinance. The global `SafeJSONProvider` already sanitizes NaN/Inf to `null`; check the source.
- **Redis refused / many live strategies "start denied":** `docker compose up -d redis`; raise `STRATEGY_MAX_THREADS` in `backend_api_python/.env` and restart the backend.
- **Disable auto-restore of running strategies on boot:** `DISABLE_RESTORE_RUNNING_STRATEGIES=true`. Disable pending-order worker: `ENABLE_PENDING_ORDER_WORKER=false`. Disable portfolio monitor: `ENABLE_PORTFOLIO_MONITOR=false`.
- **Disable live trading by agent tokens:** never set `AGENT_LIVE_TRADING_ENABLED=true` on a server where you don't want agents placing live orders.

## License

Backend is **Apache 2.0** (`LICENSE`). The web frontend source (QuantDinger-Vue) is under a separate source-available license; the prebuilt image distributed here is for integrated use. Trademarks/branding are governed separately (`TRADEMARKS.md`) and may not be altered without permission.