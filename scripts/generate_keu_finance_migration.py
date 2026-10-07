"""python scripts/generate_keu_finance_migration.py > keu_complete_finance.sql"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.keu_finance_migration import sql  # noqa: E402

if __name__ == "__main__":
    print(sql(), end="")
