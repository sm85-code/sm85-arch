# AGENTS.md — sm85-arch

## Aturan untuk agen (Hermes / Codex)
- Kerja di worktree sendiri: `git worktree add ../wt/<id> -b hermes/<id>-<topik> origin/main`. Satu tugas per repo dalam satu waktu.
- Jangan `git reset --hard`, jangan force-push, jangan hapus data/migrasi, jangan commit secret.
- Selalu lewat PR; merge hanya setelah owner bilang "ya" dan CI hijau.
- Jalankan tes terkait + build sebelum PR. Area kritis (keuangan, pesanan, DB) wajib tes penuh di CI sebelum merge.
- Pesan commit/PR singkat; jelaskan apa yang diubah dan cara mengujinya.

## Perintah
- Lint: `ruff check .`
- Tes: `python -m pytest tests/ -n 0` (di server: `/opt/data/venv-test/bin/python -m pytest ...`)
- Tes Postgres: set `TEST_DATABASE_URL`, lihat `.github/workflows/ci.yml`.
