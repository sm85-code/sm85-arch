"""Schemas -- tutup buku bulanan (spesifikasi 8.10) & bagi hasil dari bulan tertutup (8.11)."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, computed_field

from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_laporan import BarisKategoriOut


class ButirKesiapanOut(BaseModel):
    kode: str
    label: str
    siap: bool
    penghalang: bool  # True = wajib beres sebelum tutup buku; False = hanya catatan
    keterangan: str = ""


class PratinjauLabaOut(BaseModel):
    pemasukan: list[BarisKategoriOut]
    biaya: list[BarisKategoriOut]
    di_luar_laba: list[BarisKategoriOut]
    total_pemasukan: Decimal
    total_biaya: Decimal
    laba_bersih: Decimal


class KesiapanOut(BaseModel):
    periode: str
    status: str  # terbuka / ditutup / dibuka
    boleh_tutup: bool
    butir: list[ButirKesiapanOut]
    pratinjau: PratinjauLabaOut
    belum_cair: Decimal


class TutupBukuOut(BaseModel):
    id: str
    periode: str
    status: str
    ditutup_oleh: str
    ditutup_pada: datetime
    dibuka_oleh: Optional[str] = None
    dibuka_pada: Optional[datetime] = None
    alasan_buka: Optional[str] = None
    snapshot: dict[str, Any] = {}

    model_config = ConfigDict(from_attributes=True)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def laba_bersih(self) -> Optional[Decimal]:
        nilai = (self.snapshot.get("laba_rugi") or {}).get("laba_bersih")
        return Decimal(str(nilai)) if nilai is not None else None
