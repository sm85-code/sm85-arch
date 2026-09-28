# SIABUMDES ideal follow-ups (server)

Short tracking notes for residual ideal work after B0–B4. Not a migration guide.

## Done (recent)

| Item | Notes |
|------|--------|
| B0 security | Madrasah seed gate + GDrive OAuth hardening |
| B1 | UU05 stock atomicity + JWT tenant aud/iss |
| B2 | Dynamic COA import groups + clone-on-create |
| B3 | `GET /transactions` limit/offset + `meta=true` envelope |
| B4 | Pusat sentinel: `unit_usaha_id=` / `__null__` → `IS NULL` |
| Money JSON | Tx/report JSON money fields as Decimal→str; export/internal float untouched |

## Deferred / next

### Money JSON (`amount` as string) — DONE (this PR)

- `_tx_out` emits `"amount": money_str(...)` (Decimal→`"1500.50"`).
- Report/dashboard/public/ledger/period-close JSON responses run through `stringify_money_fields` (allowlisted rupiah keys).
- Internal `ReportingService` math + PDF/Excel/Word export still use float/Decimal (unchanged).
- Write path: Pydantic `Decimal` still accepts JSON number or string; FE continues to POST numbers.
- Prerequisite: `frontend-siabumdes-ts` `parseMoney` / `fmtRp` accept `number | string` (FE PR money dual-accept).
- No SIABUMDES mobile client found; old JS FE not serving production.

### Schema / Alembic clarity — DONE (this PR)

- `alembic/README.md` documents dual path: boot `ensure_schema()` vs manual Alembic.
- `tests/test_ensure_schema_lists.py` hardens `_WIDEN` / `_ADD_COLUMNS` uniqueness +
  full share_* / identity coverage, identifier + SQL-template shape, `_safe_exec` contract
  (no live Postgres required).

### Postgres in CI — **BLOCKED** (oauth lacks `workflow` scope)

- Free CI keeps `DATABASE_URL` unset; PG integration tests **skip** (see `tests/test_postgresql_integration.py`).
- `psycopg` is installed and URL rewrite → `asyncpg` works when a URL is present (#152).
- Free CI intentionally leaves `DATABASE_URL` unset (see comment on the Unit tests step in `.github/workflows/ci.yml`).
- **Blocker (2026-09-29):** authenticated `gh` / OAuth App scopes are `gist, read:org, repo` — **no `workflow`**. Contents API PUT on `.github/workflows/ci.yml` returns 404; a push that touches Actions YAML is refused. `doctl` is not available in this environment. Cannot land a `services: postgres` job until a PAT/App with `workflow` scope edits the YAML (or a human adds it in the GitHub UI).
- **Local alternative (this PR):** `docker-compose.pg-ci.yml` — Postgres 16 on `127.0.0.1:55432` for manual `DATABASE_URL=… pytest` of integration + B1 concurrent stock.
- **Max non-Actions hardening (this PR):** expanded `tests/test_ensure_schema_lists.py` (all share_* columns, identifier safety, SQL template shape, `_safe_exec` savepoint contract) — no live Postgres required.
- When unblocked: optional job `integration-pg` (manual/`workflow_dispatch` or path filter) running smoke + B1 concurrent stock — not on every push.

### Other

- Inventory multi-unit (beyond UU05-centric paths)
- UU05 list pagination with dual-mode `meta` (same B3 pattern)
