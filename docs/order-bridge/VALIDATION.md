# Development validation

No production requests or writes. Synthetic fixtures and a local disposable PostgreSQL 16 container only.

- Backend full suite: 1,631 passed, 27 skipped (PostgreSQL tests run separately); subsequent bridge tests added and verified separately. CI reruns full suite on final commit.
- Backend lint: `ruff check .` passes. Python compile check passes.
- Combined PostgreSQL schema/regression + bridge tests: 55 passed. Additional legacy-shop SQL migration test: 4/4 bridge PostgreSQL tests pass, including repeated migration with preserved row identity.
- Final bridge unit suite: 22 passed (plus the existing shop tests). Includes custom reader role write/delete rejection.
- Frontend existing full suite: 90 files / 521 tests passed; added bridge tests: 3 passed. TypeScript build, Vite build and lint pass (repository-wide lint has existing warnings).

The adapter is verified using MockTransport and synthetic source records. This does not verify native EasyBoss detail/payment/item contracts or a running upstream gateway. The PR CI results are the authoritative final-commit checks.
