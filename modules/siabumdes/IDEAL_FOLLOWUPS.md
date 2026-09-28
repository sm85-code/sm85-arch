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

### Postgres in CI

- Free CI keeps `DATABASE_URL` unset; PG integration tests **skip** (see `tests/test_postgresql_integration.py`).
- `psycopg` is installed and URL rewrite → `asyncpg` works when a URL is present (#152).
- Free CI intentionally leaves `DATABASE_URL` unset (see comment on the Unit tests step in `.github/workflows/ci.yml`).
- Adding a real `services: postgres` job is feasible but out of scope for a drive-by: needs stable secrets, seed/`ensure_schema` time budget, and clarity that alembic drift is pre-existing (app relies on `ensure_schema()` in prod).
- Next dedicated PR: optional job `integration-pg` (manual/`workflow_dispatch` or path filter) running smoke + B1 concurrent stock test — not on every push. (Requires a token/App with `workflow` scope to edit the Actions YAML.)

### Other

- Inventory multi-unit (beyond UU05-centric paths)
- UU05 list pagination with dual-mode `meta` (same B3 pattern)
