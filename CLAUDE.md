# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

**QuantDinger** — a self-hosted, local-first quantitative trading platform. This repository ships the **Python Flask backend**, **Docker Compose stack**, **Postgres schema**, the **`mcp_server/`** Python package (also published to PyPI as `quantdinger-mcp`), **strategy guides**, and all docs. The web UI source lives in a separate, source-available **QuantDinger-Vue** repo and is consumed as a prebuilt `ghcr.io/brokermr810/quantdinger-frontend` image — no frontend source/Node.js is checked in here.

Read the root `README.md` for the user-facing product story and install paths; treat this file as the developer/agent contract. The repository is currently mid-refactor on the v4.0.x line (see `docs/REFACTOR_PLAN_4_0.md`).

## Layered agent workflow (read first for non-trivial work)

For any non-trivial backend / strategy / docs change, follow `.cursor/skills/quantdinger-agent-workflow/SKILL.md`. Key red lines:

- **Never commit secrets, production `.env`, or real API keys.** Use the `env.example` pattern with placeholders.
- **Do not bypass live-trading safety.** Agent tokens are paper-only by default; live execution requires `paper_only=false` on the token **and** `AGENT_LIVE_TRADING_ENABLED=true` on the server. Do not weaken this without an explicit ask.
- Keep all **`docs/agent/*` English-only**.
- Code comments, docstrings, log messages, internal error details, module names, function names, variables, and engineering docs should be **English by default** (Chinese only for user-facing localized text and prompts). See `docs/MODULE_BOUNDARIES.md → Language Boundary`.
- Before changing a high-risk module, consult `docs/MODULE_BOUNDARIES.md`, `docs/CONCURRENCY_MODEL.md`, and `docs/API_INVENTORY.md`. If the change crosses a module boundary, document the contract and add a regression test.

## Repository layout (high signal only)

```
backend_api_python/                    Flask + Gunicorn API (Python 3.10+; Dockerfile uses 3.12-slim-bookworm)
  app/
    __init__.py                        create_app() factory + SafeJSONProvider + CORS + DB bootstrap + JWT/IBKR setup
    startup.py                         NEW (v4.0): worker boot, strategy restore, process-local singletons — owns the "process boundary"
    config/                            Env-driven settings (MetaConfig)
    markets/                           NEW (v4.0): canonical market-module registry (Crypto / USStock / CNStock / HKStock / Forex / Futures / MOEX) — single source for what counts as a market
    routes/
      agent_v1/                        Agent Gateway (scoped qd_agent_*** tokens, audit-logged) at /api/agent/v1
      strategy_*.py                    NEW (v4.0): strategy routes split by subdomain
                                        (account/backtest/deviation/grid/ledger/logs/notifications/positions/review/services/blueprint)
      ai_chat.py                       Memory + skills + tools + chat + streaming (refactor target)
      market_modules.py                NEW (v4.0): route module for the markets registry
      script_source_routes.py          NEW (v4.0)
      *.py                             Legacy Flask Blueprints; flask-smorest migration is incremental
    openapi/                           flask-smorest wiring + exported spec helper; new public routes go here first
    services/
      ai_skill_registry.py             NEW (v4.0): AI planner skill catalog (read-only metadata for system workflows)
      ai_tool_registry.py              NEW (v4.0): AI planner tool catalog (read-only metadata; routes still enforce their own auth/safety)
      mfa_service.py                   NEW (v4.0): TOTP MFA — opt-in per user, gated by MFA_ENABLED env
      script_source.py                 NEW (v4.0)
      live_trading/                    Exchange clients (binance/okx/bitget/bybit/gate/htx/coinbase/kraken); contracts; capability matrix; spot sizing; fill recovery
      grid/                            Grid bot runtime, resting orders, fill sync, ledger reconciliation
      pending_orders/                  Reusable live-order phases; live_order_phases.py holds venue-specific quirks
      pending_order_worker.py          QUEUE CONSUMER — legacy hot spot (target split: worker shell + dispatcher + reconciliation)
      trading_executor.py              Realtime strategy loop — legacy hot spot (target split: executor core + order intents + locks + recorders)
      backtest.py                      Historical simulation — legacy hot spot (target split: pipeline components)
      strategy.py + strategy_compiler.py / strategy_script_runtime.py
                                      Two strategy runtimes: IndicatorStrategy (dataframe buy/sell) + ScriptStrategy (on_bar + ctx.buy/sell)
      llm.py                           Multi-provider LLM (OpenAI/OpenRouter/AtlasCloud via litellm)
    data_sources/                      Market data adapters (factory.py registers them)
    data_providers/                    Aggregated providers (heatmap, sentiment, opportunities, economic calendar, macro_series NEW v4.0)
    utils/                             Infrastructure: db_postgres (pool), safe_exec (sandbox for user indicator code), agent_auth, agent_jobs
  migrations/init.sql                  Postgres schema; auto-applied idempotently on backend startup. Schema is SSOT — no separate v3_*.sql files.
  scripts/                             Operational scripts (NOT auto-loaded): backend_quality_check, exchange_smoke_test, export_openapi, bump_version, check_version, check_mojibake (NEW v4.0), generate-secret-key
  tests/                               Pytest suite. conftest.py sets SKIP_STARTUP_HOOKS=1 so tests skip workers/DB/Redis.
  gunicorn_config.py                   Production WSGI: 1 worker × 4 threads gthread; do NOT preload_app (worker threads depend on fork).

mcp_server/                            Published as PyPI package `quantdinger-mcp`. Thin MCP wrapper over Agent Gateway R/W/B endpoints
  src/quantdinger_mcp/                 Stdio + SSE + streamable-http transports
  pyproject.toml                       uvx / pipx installable

docs/
  API_CONVENTIONS.md                   Envelope shapes, auth, visibility tiers (Public/Internal/Private), naming
  API_INVENTORY.md                     NEW (v4.0): static snapshot of all 260 routes with risk classification (refactor guide, not a replacement for generated OpenAPI)
  MODULE_BOUNDARIES.md                 NEW (v4.0): layer ownership + allowed/forbidden dependencies + target decomposition
  CONCURRENCY_MODEL.md                 NEW (v4.0): per-domain serialization keys, idempotency posture, claim-before-work, state machines
  REFACTOR_PLAN_4_0.md                 NEW (v4.0): phased plan (Phase 0–5) for the v4.0 codebase stabilization
  agent/                               Agent Gateway + MCP docs (English-only). agent-openapi.json is the SSOT machine contract.
  api/                                 Human Web API OpenAPI yaml + ReDoc viewer
  STRATEGY_DEV_GUIDE*.md               EN/CN/TW/JA/KO strategy authoring guide
  CROSS_SECTIONAL_STRATEGY_GUIDE*.md
  examples/                            Working indicator / strategy examples
  SIGNAL_EXECUTION_STANDARD*.md        Backtest ↔ live signal/close-reason contract

scripts/                               Repo-level utility scripts: bump_version, check_version, generate-secret-key, check_mojibake (NEW v4.0)
install.sh / install.ps1               Interactive one-click installers (Bash on Linux/macOS, PowerShell on Windows) — both ask for admin user/password and write a real SECRET_KEY before first start
docker-compose.yml                     Frontend via GHCR image; backend built from ./backend_api_python
docker-compose.ghcr.yml                Both services pulled from GHCR (zero-clone deploy)
docker-compose.build.yml               Override file: builds frontend from ./QuantDinger-Vue/ source (gitignored)
.github/workflows/
  basic-ci.yml                         Python syntax + import + docker compose validate + version consistency (no live tests)
  openapi-ci.yml                       Exports spec, Spectral-lints both specs, oasdiff breaking-change check
  docker-publish.yml                   On `v*` tag: builds/pushes multi-arch backend to ghcr.io (frontend releases from Vue repo)
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

The interactive installers (`curl … install.sh | bash` / `irm … install.ps1 | iex`) will prompt for `ADMIN_USER` + `ADMIN_PASSWORD` and write a real `SECRET_KEY` automatically — preferred over the manual path.

### Run the full stack

```bash
docker compose pull && docker compose up -d            # production-ish
docker compose up -d --build backend                  # after editing backend code
docker compose logs -f backend
docker compose restart backend
```

Web UI: `http://localhost:8888`. API: `http://localhost:5000` (bound to `127.0.0.1` by default — override with `BACKEND_PORT` in repo-root `.env`).

### Run backend locally (no Docker)

```bash
cd backend_api_python
python -m venv .venv && source .venv/bin/activate   # .venv\Scripts\activate on Windows
pip install -r requirements.txt
cp env.example .env  # set SECRET_KEY, DATABASE_URL (point at a Postgres you run locally)
python run.py        # serves on :5000 with auto-reload
```

Notes: `run.py` early-loads `.env`, sets `TQDM_DISABLE=1`, applies `PROXY_URL` (with a Chinese-domestic NO_PROXY list). It refuses to boot in production (`DEBUG=False`) while `SECRET_KEY` still equals the placeholder — in dev it generates a random key for the session and prints a tip. `app/startup.py` runs background workers (pending-order, grid-fill poller, portfolio monitor, USDT order worker, AI calibration, reflection, strategy restore) unless `SKIP_STARTUP_HOOKS=1` (which `conftest.py` and `export_openapi.py` both set).

### Tests

```bash
cd backend_api_python
python -m pytest tests/ -q                          # whole suite
python -m pytest tests/test_agent_v1.py -q          # Agent Gateway auth/scope/audit
python -m pytest tests/test_openapi.py -q           # smoke checks against the exported spec
python -m pytest tests/test_grid_engine.py -q       # heavy; safe to skip in tight loops
```

`conftest.py` sets `SKIP_STARTUP_HOOKS=1`, `CACHE_ENABLED=false`, dummy `SECRET_KEY`/`ADMIN_*` so tests never touch Postgres/Redis/strategy workers. `pytest.ini` defines one marker: `integration` (live exchange API smoke tests — opt-in, requires your own testnet keys).

### OpenAPI / spec maintenance

```bash
cd backend_api_python
python scripts/export_openapi.py                              # writes ../docs/api/openapi.yaml (SSOT)
python scripts/export_openapi.py --format json -o /tmp/openapi.json
# CI in .github/workflows/openapi-ci.yml:
#   - diffs the generated spec vs committed
#   - spectral lint (human spec + agent spec)
#   - oasdiff breaking-change check
#   - pytest tests/test_openapi.py
```

If you change a route in `app/routes/` (legacy blueprint) or `app/openapi/routes/`, re-export and commit the updated `docs/api/openapi.yaml`. Agent Gateway changes also require editing `docs/agent/agent-openapi.json` by hand. Per `docs/API_INVENTORY.md → API Refactor Rules`: **do not rename or remove existing route paths during internal decomposition** — keep the old route as a compatibility wrapper.

### Backend quality / exchange smoke tests

```bash
cd backend_api_python
python scripts/backend_quality_check.py                                    # structural regression guard vs scripts/backend_quality_baseline.json
python scripts/exchange_smoke_test.py --offline-contracts                  # API-key-free contract tests (use tests/fixtures/exchanges/*.json)
# Live API tests require --allow-orders AND EXCHANGE_SMOKE_ALLOW_ORDERS=1
python scripts/check_mojibake.py                                           # scan for mojibake comments; v4.0 hygiene gate
```

### Version bumping

```bash
python scripts/check_version.py                       # verify VERSION matches every tracked version constant (run by CI)
python scripts/bump_version.py X.Y.Z                  # walks all version constants — run from repo root with the full QuantDinger-Vue-src/ checkout present so frontend constants get synced too
```

### MCP server (separate package)

```bash
cd mcp_server
pip install -e .
# stdio for desktop IDEs:
QUANTDINGER_BASE_URL=http://localhost:8888 QUANTDINGER_AGENT_TOKEN=qd_agent_xxx quantdinger-mcp
# remote:
QUANTDINGER_MCP_TRANSPORT=streamable-http QUANTDINGER_MCP_HOST=0.0.0.0 QUANTDINGER_MCP_PORT=7800 quantdinger-mcp
```

`mcp_server/` wraps Agent Gateway R/W/B endpoints only (no `quick-trade/*`). New MCP tools must be backed by a REST endpoint first. MCP package version is decoupled from the backend (current line: `quantdinger-mcp 0.2.0`).

## Architecture (the big picture)

Two API surfaces, registered from a single Flask app (`app/routes/__init__.py` → `init_openapi` + `register_agent_v1`):

- **Human Web API** — `/api/...`, JWT-authenticated, mounted via flask-smorest. Old modules use plain Flask Blueprints under `app/routes/*.py`; the migration to flask-smorest is incremental. The exported spec (`docs/api/openapi.yaml`) is the SSOT. As of v4.0, ~260 routes are inventoried in `docs/API_INVENTORY.md`, classified by stability (Public / Private / Admin / Agent / Internal).
- **Agent Gateway** — `/api/agent/v1/...`, agent-token (`qd_agent_***`) authenticated, **never** JWT, hand-maintained spec at `docs/agent/agent-openapi.json`. Tokens are hashed at rest in `qd_agent_tokens`; every call (success or denial) is appended to `qd_agent_audit`. Scope classes: `R` (read), `W` (workspace write), `B` (backtest/experiment), `N`, `C`, `T` (trading — paper-only by default). Async jobs persisted in `qd_agent_jobs` with `job_id` + `idempotency_key` (unique index) — replays return the same job. Clients poll `/jobs/{id}` or subscribe to `GET /jobs/{id}/stream` (SSE: `snapshot`/`progress`/`ping`/`result`). Auth helper: `app/utils/agent_auth.py` (`@agent_required(scope=...)`). Secret redaction lives in `app/routes/agent_v1/_security.py` (512 KiB cap on indicator source; known credential keys masked in JSON).

Strategy and execution pipeline:

```
data_sources/* (factory.py)  →  data_providers/* (aggregators + macro_series)
       ↓
markets/registry.py          (canonical market modules: Crypto/USStock/CN/HK/Forex/Futures/MOEX)
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

### v4.0 design contracts (read these before touching a high-risk module)

- **`docs/MODULE_BOUNDARIES.md`** — which layer owns what; route/service/adapter/startup rules; target decomposition for legacy hotspots (`routes/strategy.py`, `routes/quick_trade.py`, `services/trading_executor.py`, `services/pending_order_worker.py`).
- **`docs/CONCURRENCY_MODEL.md`** — per-domain serialization keys (`strategy_id`, `strategy_id:symbol:side`, `user_id:credential_id:market:symbol:side`, `pending_order_id`, `strategy_id:symbol:cell_index`, …), idempotency key shape `actor_id:operation:target:client_request_id`, claim-before-work pattern, state machine (`pending → processing → submitted → filled / partially_filled / failed / cancelled / expired`).
- **`docs/API_INVENTORY.md`** — every route family with risk classification; rules for not renaming/removing existing paths.

## Adding or modifying things — where they go

- **New exchange for live trading:** create `app/services/live_trading/<name>.py` inheriting `BaseLiveTrading`; register in `live_trading/factory.py`; add a capability row to `live_trading/capabilities.py`; add fixtures in `tests/fixtures/exchanges/order_fill_contracts.json` (+ `position_contracts.json` if derivatives); add tests in `test_exchange_order_param_contracts.py`; run the smoke + quality checks above.
- **New data source:** `app/data_sources/<name>.py` with `get_ticker`/`get_kline`; register in `data_sources/factory.py`. If it should appear on the global dashboard, add a fetcher in `data_providers/` and wire it into the fallback chain.
- **New market module:** add a `MarketModule` entry in `app/markets/registry.py` and `app/markets/models.py`, then expose it via `app/routes/market_modules.py`. UI visibility still comes from `ENABLED_MARKETS` / legacy `SHOW_*` flags via `app/utils/market_visibility.py`.
- **New public HTTP route:** prefer flask-smorest under `app/openapi/routes/` + a schema under `app/openapi/schemas/`; register it in `app/openapi/register.py` and update `docs/api/openapi.yaml` via `scripts/export_openapi.py`. Read `docs/API_CONVENTIONS.md` first (envelope shape `{"code":1,"msg":"success","data":{...}}` for human endpoints; agent endpoints use `{"code":0,"message":...}` with `details`/`retriable`). Visibility tiers: Public (default), Internal (`x-visibility: internal`), Private (`x-visibility: private`). Do not rename existing route paths; keep old ones as compatibility wrappers.
- **New agent route:** put it in `app/routes/agent_v1/`, gate with `@agent_required(scope=...)`, follow the idempotency posture from `docs/CONCURRENCY_MODEL.md`, update `docs/agent/agent-openapi.json` by hand, and decide whether the MCP server should also expose it (`mcp_server/src/quantdinger_mcp/`).
- **New broker adapter (stocks/forex):** `app/services/<ibkr_trading|mt5_trading|alpaca_trading>/client.py` (each subpackage has its own README).
- **New AI planner skill/tool:** add a definition to `app/services/ai_skill_registry.py` / `app/services/ai_tool_registry.py`. These registries are **read-only metadata** — they describe capabilities to the AI planner but never bypass route-level auth/safety.
- **MFA (TOTP) wiring:** per-user opt-in via `app/services/mfa_service.py`. Enable the feature surface with `MFA_ENABLED=true` in `.env`; it does NOT force every account to bind an authenticator.
- **New strategy example:** drop it into `docs/examples/` and link from the relevant `STRATEGY_DEV_GUIDE*.md` rather than duplicating guide text.

## Refactoring rules (from `docs/MODULE_BOUNDARIES.md` + `docs/REFACTOR_PLAN_4_0.md`)

Route layer:
- Routes may validate input, call services, and shape HTTP responses.
- Routes **must not** start background threads, contain exchange-specific order sizing logic, or perform multi-step DB transactions inline (unless being migrated + covered by tests).
- Streaming routes must define timeout, heartbeat, and cancellation behavior.

Service layer:
- Accept plain Python values, not Flask request objects.
- Return plain dicts/dataclasses or typed result objects.
- Define idempotency behavior when mutating state.

Adapter layer (exchanges / brokers):
- Normalize external APIs into internal contracts.
- Don't know about Flask, users, or frontend response shapes.
- Accept explicit `client_order_id` when the venue supports it.
- Isolate rate limits and retry rules from business logic.

Anti-patterns (must avoid):
- New large `isinstance(client, ExchangeClient)` blocks in routes.
- New exchange support by only patching `pending_order_worker.py`.
- Swallowing trading-core exceptions with `except Exception: pass` — mark the order failed, log context, or document why the exception is safe.
- Renaming or removing existing route paths during decomposition.

Known legacy hot spots (tracked by `backend_quality_baseline.json`; do not let them grow, refactor per `REFACTOR_PLAN_4_0.md`):
- `app/services/trading_executor.py`
- `app/services/pending_order_worker.py`
- `app/services/backtest.py`
- `app/routes/quick_trade.py`
- `app/routes/strategy.py` (being decomposed into `routes/strategy_*.py`)

## Troubleshooting quick refs

- **Backend exits immediately:** `SECRET_KEY` is still the example placeholder, or `.env` has invalid syntax. `docker compose logs backend`.
- **Blank UI / API errors from browser:** `FRONTEND_URL` mismatch or API not reachable on the host the browser opened. Default API bind is `127.0.0.1:5000` — override via repo-root `.env` `BACKEND_PORT`.
- **Slow `docker pull`:** add `IMAGE_PREFIX=docker.m.daocloud.io/library/` to repo-root `.env` or configure Docker Desktop proxies.
- **"apikey parameter is incorrect" from Twelve Data:** `TWELVE_DATA_API_KEY` missing in `.env`. Chinese stock data requires a paid plan.
- **Heatmap "暂无数据":** usually NaN/Inf leaking from yfinance. The global `SafeJSONProvider` already sanitizes NaN/Inf to `null`; check the source.
- **Redis refused / many live strategies "start denied":** `docker compose up -d redis`; raise `STRATEGY_MAX_THREADS` in `backend_api_python/.env` and restart the backend.
- **Disable auto-restore of running strategies on boot:** `DISABLE_RESTORE_RUNNING_STRATEGIES=true`. Disable pending-order worker: `ENABLE_PENDING_ORDER_WORKER=false`. Disable portfolio monitor: `ENABLE_PORTFOLIO_MONITOR=false`. Disable grid fill poller: `ENABLE_GRID_FILL_POLLER=false`.
- **Disable live trading by agent tokens:** never set `AGENT_LIVE_TRADING_ENABLED=true` on a server where you don't want agents placing live orders.
- **Multi-worker Gunicorn + workers:** in-process singletons in `app/startup.py` (trading executor, pending-order worker, USDT worker) are **process-local** — under multi-worker Gunicorn they will exist in each worker. Per `docs/CONCURRENCY_MODEL.md → Gaps To Audit`, each external-side-effect worker needs either a single owner or a distributed lock before production multi-worker is safe.

## License

Backend is **Apache 2.0** (`LICENSE`). The web frontend source (QuantDinger-Vue) is under a separate source-available license; the prebuilt image distributed here is for integrated use. Trademarks/branding are governed separately (`TRADEMARKS.md`) and may not be altered without permission.