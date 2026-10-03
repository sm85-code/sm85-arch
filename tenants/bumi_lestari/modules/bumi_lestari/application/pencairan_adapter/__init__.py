"""Adapter kode cadangan (spesifikasi 2.5b, 10.7): untuk file yang tidak bisa dipetakan dengan format penghasilan.

Satu adapter = fungsi `(isi_file: bytes, nama_file: str) -> HasilFormat` yang menghasilkan baris standar yang sama
dengan mesin format. Adapter tidak boleh memuat logika bisnis (AB-MP-5). Didaftarkan per nama saluran, mis.:

    ADAPTER["Blibli"] = baca_blibli

Belum ada adapter yang dibutuhkan; semua saluran memakai format penghasilan di Data master.
"""
from __future__ import annotations

from typing import Callable

from tenants.bumi_lestari.modules.bumi_lestari.application.pencairan_format import HasilFormat

Adapter = Callable[[bytes, str], HasilFormat]

ADAPTER: dict[str, Adapter] = {}


def adapter_untuk(nama_saluran: str) -> Adapter | None:
    return ADAPTER.get(nama_saluran)
