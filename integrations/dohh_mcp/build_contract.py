"""Run from repo root with the ERP test venv. No database or HTTP calls."""

import hashlib
import json
from pathlib import Path

from tenants.marketplace_erp.modules.marketplace_erp.application import (
    agen_aksi,
    agen_guardrail,
    agen_jadwal,
    agen_pemasaran,
)

ROOT = Path(__file__).resolve().parents[2]
APP = "tenants/marketplace_erp/modules/marketplace_erp/application/"
API = "tenants/marketplace_erp/adapters/api/v1/"
FILES = [
    APP + n + ".py" for n in ["agen_aksi", "agen_guardrail", "agen_jadwal", "agen_pemasaran", "agen_pengguna", "akses"]
]
FILES += [API + n + ".py" for n in ["agen_router", "agen_guardrail_router", "marketplace_erp_router"]]
FILES += ["tenants/marketplace_erp/modules/marketplace_erp/infrastructure/auth.py"]


def build():
    return {
        "sources": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in FILES},
        "models": {
            n: model.model_json_schema()
            for n, model in {
                "aksi": agen_aksi.AksiIn,
                "pemasaran": agen_pemasaran.TulisIn,
                "batas": agen_guardrail.BatasIn,
                "jadwal": agen_jadwal.JadwalIn,
            }.items()
        },
        "baca": sorted(agen_pemasaran.BACA),
        "tulis": sorted(agen_pemasaran.TULIS),
        "jenis": sorted(set(agen_aksi.JENIS) | agen_aksi.JENIS_KATA),
    }


if __name__ == "__main__":
    print(json.dumps(build(), indent=2, ensure_ascii=False))
