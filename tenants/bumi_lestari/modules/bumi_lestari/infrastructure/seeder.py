"""Idempotent schema creation + starter data for the bumi_lestari database.

seed-now creates the first owner (password from BUMI_LESTARI_SEED_OWNER_PASSWORD --
there is no default password), the default akun kas (incl. kas kecil with plafon
Rp 3.000.000) and default kategori.
"""
from __future__ import annotations

import os
from datetime import date
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.security import hash_password
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure import database as bl_database
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import BumiLestariBase
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_order import BlSaluran
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure import models_pembayaran  # noqa: F401  (register tables)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_pembayaran import BlLangganan
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure import models_talangan  # noqa: F401 (daftarkan tabel)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure import models_kolom  # noqa: F401 (daftarkan tabel)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_iklan import BlPlatformIklan
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_pencairan import (
    BlFormatPenghasilan,
    BlFormatPenghasilanKolom,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import (
    KODE_DANA_CADANGAN,
    KODE_KAS_IKLAN,
    KODE_KAS_KECIL,
    JENIS_KEWAJIBAN,
    KODE_KAS_UTAMA,
    KODE_TALANGAN,
    KODE_SALDO_BLIBLI,
    KODE_SALDO_IPAYMU,
    KODE_SALDO_LAZADA,
    KODE_SALDO_SHOPEE,
    KODE_SALDO_TIKTOK,
    PLAFON_KAS_IKLAN_DEFAULT,
    PLAFON_KAS_KECIL_DEFAULT,
    BlAkunKas,
    BlKategori,
    BlProporsiBagiHasil,
    BlTransaksi,
    BlUser,
)

DEFAULT_OWNER_EMAIL = "owner@bumi-lestari.internal"
DEFAULT_ADMIN_EMAIL = "admin@bumi-lestari.internal"

DEFAULT_AKUN = (
    (KODE_KAS_UTAMA, "Kas utama", "kas", None),
    (KODE_SALDO_SHOPEE, "Saldo Shopee", "ewallet", None),
    (KODE_SALDO_TIKTOK, "Saldo TikTok Shop", "ewallet", None),
    (KODE_SALDO_LAZADA, "Saldo Lazada", "ewallet", None),
    (KODE_SALDO_BLIBLI, "Saldo Blibli", "ewallet", None),
    (KODE_SALDO_IPAYMU, "Saldo iPaymu", "ewallet", None),
    (KODE_KAS_KECIL, "Kas kecil", "kas_kecil", PLAFON_KAS_KECIL_DEFAULT),
    (KODE_KAS_IKLAN, "Kas iklan", "kas_iklan", PLAFON_KAS_IKLAN_DEFAULT),
    (KODE_DANA_CADANGAN, "Dana cadangan (gaji & langganan)", "kas", None),
    (KODE_TALANGAN, "Talangan (utang ke perorangan)", JENIS_KEWAJIBAN, None),
)

# Saluran bawaan dan akun saldonya (spesifikasi 2.6). Saluran lain ditambah lewat POST /saluran.
DEFAULT_SALURAN = (
    ("Shopee", "marketplace", KODE_SALDO_SHOPEE),
    ("TikTok Shop", "marketplace", KODE_SALDO_TIKTOK),
    ("Lazada", "marketplace", KODE_SALDO_LAZADA),
    ("Blibli", "marketplace", KODE_SALDO_BLIBLI),
    ("Toko web", "web", KODE_SALDO_IPAYMU),
)

DEFAULT_LANGGANAN = ("Listrik", "Air", "Wifi", "Kebersihan", "Iuran BUMDES", "Langganan Komplace")

DEFAULT_KATEGORI = (
    ("Penjualan marketplace", "pemasukan"),
    ("Penjualan toko web", "pemasukan"),
    ("Penjualan reseller", "pemasukan"),
    ("Pemasukan lain", "pemasukan"),
    ("Biaya produksi / pembelian barang", "pengeluaran"),
    ("Gaji karyawan", "pengeluaran"),
    ("Bagi hasil", "pengeluaran"),
    ("Transport", "pengeluaran"),
    ("Packing", "pengeluaran"),
    ("Operasional", "pengeluaran"),
    ("Biaya iklan", "pengeluaran"),
    ("Langganan & utilitas", "pengeluaran"),
    ("Prive", "pengeluaran"),
    ("Pengeluaran lain", "pengeluaran"),
    # Fase 1 (spesifikasi 6.2)
    ("Setoran modal", "pemasukan"),
    ("Biaya marketplace", "pengeluaran"),
    ("Kerugian retur", "pengeluaran"),
)

# Format Shopee "Penghasilan Saya" SEMENTARA (spesifikasi 2.5a). Belum ada contoh file asli: nama kolom diambil dari
# ekspor Seller Center yang umum dan HARUS diuji dengan file asli (Data master > Format file penghasilan > Uji) sebelum
# diaktifkan; bila kolom berbeda, ubah pemetaan (versi draf) lalu uji lagi.
FORMAT_SHOPEE_SEMENTARA = {
    "nama": "Shopee Penghasilan Saya (SEMENTARA)",
    "jenis_file": "xlsx",
    "nama_sheet": None,
    "baris_header": 6,
    "format_tanggal": "yyyy-mm-dd",
    "pemisah_desimal": ",",
    "pemisah_ribuan": ".",
    "aturan_tanda": "mutlak",
    "satuan_baris": "per_pesanan",
    "aturan_jenis_baris": {"kolom": None, "retur": ["Pengembalian"], "penyesuaian": ["Penyesuaian"], "negatif_penyesuaian": True},
    "aturan_abaikan": {"kode_kosong": True, "berisi": ["Total"]},
    "catatan": (
        "SEMENTARA — dibuat tanpa contoh file asli. Unggah file Keuangan > Penghasilan Saya > Sudah Dilepas > Export "
        "di tombol Uji; periksa baris judul (bawaan baris 6) dan nama kolom, sesuaikan, lalu aktifkan."
    ),
    "kolom": [
        ("kode_pesanan", "No. Pesanan", "ambil", None),
        ("tanggal_cair", "Tanggal Dana Dilepaskan", "ambil", None),
        ("jumlah_cair", "Total Penghasilan", "ambil", None),
        ("harga_jual", "Harga Asli Produk", "jumlahkan", None),
        ("harga_jual", "Total Diskon Produk", "jumlahkan", None),
        ("potongan_biaya", "Biaya Administrasi", "mutlak", "Biaya administrasi"),
        ("potongan_biaya", "Biaya Layanan", "mutlak", "Biaya layanan"),
        ("potongan_biaya", "Biaya Proses Pesanan", "mutlak", "Biaya proses pesanan"),
        ("potongan_biaya", "Biaya Komisi AMS", "mutlak", "Komisi AMS"),
        ("potongan_biaya", "Voucher disponsor oleh Penjual", "mutlak", "Voucher penjual"),
        ("potongan_biaya", "Ongkos Kirim Pengembalian Barang", "mutlak", "Ongkir retur"),
    ],
}

# Platform iklan bawaan (AB-KI-3): internal = iklan di marketplace (dihubungkan ke salurannya bila ada).
DEFAULT_PLATFORM_IKLAN = (
    ("Shopee", "internal", "Shopee"),
    ("TikTok Shop", "internal", "TikTok Shop"),
    ("Lazada", "internal", "Lazada"),
    ("Blibli", "internal", "Blibli"),
    ("Meta", "eksternal", None),
    ("Google", "eksternal", None),
)

# Entri pembuka modal Owner (spesifikasi AB-MD-1): dicatat sekali oleh seed.
SETORAN_MODAL_AWAL = Decimal("20000000")
TANGGAL_SETORAN_MODAL_AWAL = date(2026, 9, 1)

# Kolom yang ditambahkan setelah tabel pertama kali dibuat. create_all tidak mengubah tabel yang sudah ada,
# jadi Postgres yang sudah berjalan disusulkan lewat ALTER idempoten (pola sama dengan tenant store).
_ALTER_POSTGRES = (
    "ALTER TABLE bl_users ADD COLUMN IF NOT EXISTS session_version INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE bl_transaksi ADD COLUMN IF NOT EXISTS status_kirim VARCHAR(16) NOT NULL DEFAULT 'terkirim'",
    "ALTER TABLE bl_transaksi ADD COLUMN IF NOT EXISTS kiriman_id VARCHAR(64)",
    "ALTER TABLE bl_transaksi ADD COLUMN IF NOT EXISTS sumber_sistem VARCHAR(32)",
    "ALTER TABLE bl_transaksi ADD COLUMN IF NOT EXISTS sumber_ref VARCHAR(255)",
    "ALTER TABLE bl_transaksi ADD COLUMN IF NOT EXISTS koreksi_periode VARCHAR(7)",
    "ALTER TABLE bl_pembayaran_pemasok ADD COLUMN IF NOT EXISTS status_kirim VARCHAR(16) NOT NULL DEFAULT 'terkirim'",
    "ALTER TABLE bl_pembayaran_pemasok ADD COLUMN IF NOT EXISTS kiriman_id VARCHAR(64)",
    "ALTER TABLE bl_penerimaan_reseller ADD COLUMN IF NOT EXISTS status_kirim VARCHAR(16) NOT NULL DEFAULT 'terkirim'",
    "ALTER TABLE bl_penerimaan_reseller ADD COLUMN IF NOT EXISTS kiriman_id VARCHAR(64)",
    "ALTER TABLE bl_order ADD COLUMN IF NOT EXISTS sumber_sistem VARCHAR(32)",
    "ALTER TABLE bl_order ADD COLUMN IF NOT EXISTS sumber_ref VARCHAR(255)",
    "ALTER TABLE bl_produk ADD COLUMN IF NOT EXISTS sumber_sistem VARCHAR(32)",
    "ALTER TABLE bl_produk ADD COLUMN IF NOT EXISTS sumber_ref VARCHAR(255)",
    "ALTER TABLE bl_order ADD COLUMN IF NOT EXISTS status_cair VARCHAR(16) NOT NULL DEFAULT 'belum'",
    "ALTER TABLE bl_order ADD COLUMN IF NOT EXISTS tgl_cair DATE",
    "ALTER TABLE bl_order ADD COLUMN IF NOT EXISTS pencairan_baris_id VARCHAR(64)",
    "ALTER TABLE bl_order ADD COLUMN IF NOT EXISTS potongan_aktual NUMERIC(14, 2)",
    "ALTER TABLE bl_order ADD COLUMN IF NOT EXISTS tgl_retur DATE",
    "ALTER TABLE bl_order ADD COLUMN IF NOT EXISTS alasan_retur TEXT",
    "ALTER TABLE bl_order ADD COLUMN IF NOT EXISTS kembali_stok BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE bl_transfer ADD COLUMN IF NOT EXISTS di_luar_jadwal BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE bl_transfer ADD COLUMN IF NOT EXISTS alasan_luar_jadwal TEXT",
    "ALTER TABLE bl_transaksi ADD COLUMN IF NOT EXISTS platform_iklan_id VARCHAR(64)",
    "ALTER TABLE bl_transaksi ADD COLUMN IF NOT EXISTS melebihi_porsi BOOLEAN NOT NULL DEFAULT false",
    "CREATE INDEX IF NOT EXISTS ix_bl_transaksi_platform_iklan_id ON bl_transaksi (platform_iklan_id)",
    "ALTER TABLE bl_profil ADD COLUMN IF NOT EXISTS porsi_iklan_internal NUMERIC(5, 2) NOT NULL DEFAULT 25",
    "ALTER TABLE bl_profil ADD COLUMN IF NOT EXISTS porsi_iklan_eksternal NUMERIC(5, 2) NOT NULL DEFAULT 75",
    "ALTER TABLE bl_profil ADD COLUMN IF NOT EXISTS budget_iklan_bulanan NUMERIC(14, 2)",
    "ALTER TABLE bl_order ADD COLUMN IF NOT EXISTS kolom_tambahan JSONB NOT NULL DEFAULT '{}'",
    "CREATE INDEX IF NOT EXISTS ix_bl_order_kolom_tambahan ON bl_order USING GIN (kolom_tambahan)",
    "ALTER TABLE bl_produk ADD COLUMN IF NOT EXISTS kolom_tambahan JSONB NOT NULL DEFAULT '{}'",
    "CREATE INDEX IF NOT EXISTS ix_bl_produk_kolom_tambahan ON bl_produk USING GIN (kolom_tambahan)",
    "ALTER TABLE bl_pemasok ADD COLUMN IF NOT EXISTS kolom_tambahan JSONB NOT NULL DEFAULT '{}'",
    "CREATE INDEX IF NOT EXISTS ix_bl_pemasok_kolom_tambahan ON bl_pemasok USING GIN (kolom_tambahan)",
    "ALTER TABLE bl_pelanggan ADD COLUMN IF NOT EXISTS kolom_tambahan JSONB NOT NULL DEFAULT '{}'",
    "CREATE INDEX IF NOT EXISTS ix_bl_pelanggan_kolom_tambahan ON bl_pelanggan USING GIN (kolom_tambahan)",
    "ALTER TABLE bl_transaksi ADD COLUMN IF NOT EXISTS kolom_tambahan JSONB NOT NULL DEFAULT '{}'",
    "CREATE INDEX IF NOT EXISTS ix_bl_transaksi_kolom_tambahan ON bl_transaksi USING GIN (kolom_tambahan)",
    "ALTER TABLE bl_karyawan ADD COLUMN IF NOT EXISTS kolom_tambahan JSONB NOT NULL DEFAULT '{}'",
    "CREATE INDEX IF NOT EXISTS ix_bl_karyawan_kolom_tambahan ON bl_karyawan USING GIN (kolom_tambahan)",
    "CREATE INDEX IF NOT EXISTS ix_bl_order_status_cair ON bl_order (status_cair)",
    "CREATE INDEX IF NOT EXISTS ix_bl_order_pencairan_baris_id ON bl_order (pencairan_baris_id)",
    "CREATE INDEX IF NOT EXISTS ix_bl_transaksi_status_kirim ON bl_transaksi (status_kirim)",
    "CREATE INDEX IF NOT EXISTS ix_bl_transaksi_kiriman_id ON bl_transaksi (kiriman_id)",
    "CREATE INDEX IF NOT EXISTS ix_bl_pembayaran_pemasok_kiriman_id ON bl_pembayaran_pemasok (kiriman_id)",
    "CREATE INDEX IF NOT EXISTS ix_bl_penerimaan_reseller_kiriman_id ON bl_penerimaan_reseller (kiriman_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_bl_transaksi_sumber ON bl_transaksi (sumber_sistem, sumber_ref)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_bl_order_sumber ON bl_order (sumber_sistem, sumber_ref)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_bl_produk_sumber ON bl_produk (sumber_sistem, sumber_ref)",
)


async def _self_heal_columns(conn) -> None:
    """Tambahkan kolom/indeks baru ke tabel lama (Postgres; idempoten)."""
    if conn.dialect.name != "postgresql":
        return
    for stmt in _ALTER_POSTGRES:
        await conn.execute(text(stmt))


async def _create_schema(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(BumiLestariBase.metadata.create_all)
        await _self_heal_columns(conn)


async def ensure_bumi_lestari_schema() -> None:
    """Startup hook (main.py lifespan). No-op when DATABASE_URL_BUMI_LESTARI is unset."""
    if bl_database.engine is None:
        return
    await _create_schema(bl_database.engine)


async def seed_bumi_lestari(session: AsyncSession) -> dict[str, str]:
    engine = bl_database.engine
    if engine is None:
        raise RuntimeError("DATABASE_URL_BUMI_LESTARI is not configured")
    await _create_schema(engine)

    email = (os.getenv("BUMI_LESTARI_SEED_OWNER_EMAIL") or DEFAULT_OWNER_EMAIL).strip()
    owner = (await session.execute(select(BlUser).where(BlUser.email == email))).scalar_one_or_none()
    if owner is None:
        password = os.getenv("BUMI_LESTARI_SEED_OWNER_PASSWORD") or ""
        if len(password) < 8:
            raise RuntimeError("BUMI_LESTARI_SEED_OWNER_PASSWORD (min 8 karakter) wajib diisi untuk membuat owner")
        owner = BlUser(
            nama="Owner", email=email, password_hash=hash_password(password), role="owner", must_change_password=True
        )
        session.add(owner)

    # Admin (di atas owner): dibuat hanya bila BUMI_LESTARI_SEED_ADMIN_PASSWORD diisi.
    admin_email = (os.getenv("BUMI_LESTARI_SEED_ADMIN_EMAIL") or DEFAULT_ADMIN_EMAIL).strip()
    admin_password = os.getenv("BUMI_LESTARI_SEED_ADMIN_PASSWORD") or ""
    if len(admin_password) >= 8 and (
        await session.execute(select(BlUser.id).where(BlUser.email == admin_email))
    ).first() is None:
        session.add(
            BlUser(
                nama="Admin", email=admin_email, password_hash=hash_password(admin_password),
                role="admin", must_change_password=True,
            )
        )

    for kode, nama, jenis, plafon in DEFAULT_AKUN:
        if (await session.execute(select(BlAkunKas.id).where(BlAkunKas.kode == kode))).first() is None:
            session.add(BlAkunKas(kode=kode, nama=nama, jenis=jenis, plafon=plafon))
    for nama, jenis in DEFAULT_KATEGORI:
        if (await session.execute(select(BlKategori.id).where(BlKategori.nama == nama))).first() is None:
            session.add(BlKategori(nama=nama, jenis=jenis))
    # Langganan bulanan awal (nominal diisi dari halaman Langganan; 0 = belum diisi, belum disisihkan).
    if (await session.execute(select(BlLangganan.id))).first() is None:
        for nama in DEFAULT_LANGGANAN:
            session.add(BlLangganan(nama=nama, jumlah_bulanan=0))
    await session.flush()
    # Saluran bawaan: dibuat bila belum ada; saluran lama tanpa akun saldo dihubungkan ke akunnya.
    for nama, jenis, kode_akun in DEFAULT_SALURAN:
        akun = (await session.execute(select(BlAkunKas).where(BlAkunKas.kode == kode_akun))).scalar_one()
        saluran = (await session.execute(select(BlSaluran).where(BlSaluran.nama == nama))).scalar_one_or_none()
        if saluran is None:
            session.add(BlSaluran(nama=nama, jenis=jenis, akun_id=akun.id))
        elif saluran.akun_id is None:
            saluran.akun_id = akun.id
    # Nilai awal proporsi bagi hasil -- hanya dibuat sekali; selanjutnya diubah dari halaman profil UMKM.
    if (await session.execute(select(BlProporsiBagiHasil.id))).first() is None:
        for penerima, persen in (("admin", 40), ("owner", 60)):
            session.add(BlProporsiBagiHasil(penerima=penerima, persen=persen))
    await session.flush()
    for nama, grup, nama_saluran in DEFAULT_PLATFORM_IKLAN:
        if (await session.execute(select(BlPlatformIklan.id).where(BlPlatformIklan.nama == nama))).first() is None:
            sid = None
            if nama_saluran:
                sid = (await session.execute(select(BlSaluran.id).where(BlSaluran.nama == nama_saluran))).scalar_one_or_none()
            session.add(BlPlatformIklan(nama=nama, grup=grup, saluran_id=sid))
    await session.flush()
    await _seed_format_shopee(session)
    await _seed_kolom_tambahan(session)
    await _seed_setoran_modal(session, owner)
    await session.commit()
    return {"owner_email": email, "status": "ok"}


# Kolom tambahan bawaan (spesifikasi 10.2). (entitas, label, tipe, pilihan, tampil_tabel, bisa_filter, tampil_staf)
DEFAULT_KOLOM_TAMBAHAN = (
    ("order", "Target selesai di tukang", "tanggal", (), True, True, False),
    ("order", "No. resi", "teks", (), True, False, False),
    ("order", "Kurir", "pilihan", ("JNE", "J&T", "SiCepat", "Kurir toko", "Lainnya"), True, True, False),
    ("order", "Kota tujuan", "teks", (), False, True, False),
    ("order", "Ukuran khusus", "teks", (), False, False, False),
    ("order", "Prioritas / mendesak", "ya_tidak", (), True, True, False),
    ("order", "Catatan untuk tukang", "teks", (), False, False, False),
    ("produk", "Bahan / jenis kayu", "pilihan", ("Jati", "Mahoni", "Pinus", "Mindi", "Lainnya"), True, True, False),
    ("produk", "Kategori produk", "pilihan", ("Partisi", "Rak", "Meja", "Lemari", "Lainnya"), True, True, False),
    ("produk", "Berat (gram)", "angka", (), False, False, False),
    ("produk", "Dimensi paket", "teks", (), False, False, False),
    ("produk", "Lama produksi (hari)", "angka", (), False, False, False),
    ("produk", "Link foto", "teks", (), False, False, False),
    ("pemasok", "Alamat / lokasi", "teks", (), False, False, False),
    ("pemasok", "Spesialisasi", "pilihan", ("Kayu", "Cat", "Besi", "Lainnya"), True, True, False),
    ("pemasok", "Kapasitas per minggu", "angka", (), False, False, False),
    ("pemasok", "Lama pengerjaan biasa (hari)", "angka", (), False, False, False),
    ("pemasok", "Mulai bekerja sama", "tanggal", (), False, False, False),
    ("pelanggan", "Nama toko penjual lain", "teks", (), True, False, False),
    ("pelanggan", "Kota / wilayah", "pilihan", ("Jabodetabek", "Jawa Barat", "Jawa Tengah", "Jawa Timur", "Luar Jawa"), True, True, False),
    ("pelanggan", "Rekening penjual lain", "teks", (), False, False, False),
    ("pelanggan", "Mulai bekerja sama", "tanggal", (), False, False, False),
    ("transaksi", "No. nota / struk", "teks", (), False, False, True),
    ("transaksi", "Ada nota", "ya_tidak", (), True, True, True),
    ("transaksi", "Toko / vendor", "teks", (), False, False, True),
    ("transaksi", "Metode bayar", "pilihan", ("Tunai", "Transfer", "QRIS"), False, True, True),
    ("karyawan", "No. HP", "teks", (), True, False, False),
    ("karyawan", "Tanggal mulai kerja", "tanggal", (), False, False, False),
    ("karyawan", "Rekening gaji", "teks", (), False, False, False),
)


async def _seed_kolom_tambahan(session: AsyncSession) -> None:
    """Hanya untuk entitas yang belum punya definisi kolom sama sekali, agar kolom yang dihapus Admin tidak muncul lagi."""
    from tenants.bumi_lestari.modules.bumi_lestari.application.kolom_core import kunci_dari_label
    from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_kolom import BlDefinisiKolom

    sudah = set((await session.execute(select(BlDefinisiKolom.entitas).distinct())).scalars())
    for i, (entitas, label, tipe, pilihan, tabel, filt, staf) in enumerate(DEFAULT_KOLOM_TAMBAHAN):
        if entitas in sudah:
            continue
        session.add(BlDefinisiKolom(
            entitas=entitas, kunci=kunci_dari_label(label), lapisan="tambahan", label=label, tipe=tipe,
            pilihan=[{"nilai": p, "arsip": False} for p in pilihan], tampil_tabel=tabel, bisa_filter=filt,
            tampil_staf=staf, urutan=100 + i,
        ))
    await session.flush()


async def _seed_setoran_modal(session: AsyncSession, owner: BlUser) -> None:
    """Setoran modal Owner Rp 20 juta, 1 Sep 2026, ke Kas utama -- hanya bila belum ada setoran modal sama sekali."""
    kategori = (await session.execute(select(BlKategori).where(BlKategori.nama == "Setoran modal"))).scalar_one()
    sudah = (
        await session.execute(
            select(BlTransaksi.id).where(BlTransaksi.kategori_id == kategori.id, BlTransaksi.dibatalkan.is_(False))
        )
    ).first()
    if sudah:
        return
    kas = (await session.execute(select(BlAkunKas).where(BlAkunKas.kode == KODE_KAS_UTAMA))).scalar_one()
    session.add(
        BlTransaksi(
            tanggal=TANGGAL_SETORAN_MODAL_AWAL, akun_id=kas.id, kategori_id=kategori.id, jenis="masuk",
            jumlah=SETORAN_MODAL_AWAL, keterangan="Setoran modal Owner (entri pembuka)", dibuat_oleh=owner.id,
            status_kirim="terkirim",
        )
    )


async def _seed_format_shopee(session: AsyncSession) -> None:
    """Format Shopee sementara sebagai v1 draf -- hanya bila Shopee belum punya format sama sekali."""
    shopee = (await session.execute(select(BlSaluran).where(BlSaluran.nama == "Shopee"))).scalar_one_or_none()
    if shopee is None or (
        await session.execute(select(BlFormatPenghasilan.id).where(BlFormatPenghasilan.saluran_id == shopee.id))
    ).first():
        return
    data = dict(FORMAT_SHOPEE_SEMENTARA)
    kolom = data.pop("kolom")
    fmt = BlFormatPenghasilan(saluran_id=shopee.id, versi=1, status="draf", **data)
    session.add(fmt)
    await session.flush()
    for i, (tujuan, sumber, operasi, rincian) in enumerate(kolom):
        session.add(
            BlFormatPenghasilanKolom(
                format_id=fmt.id, kolom_tujuan=tujuan, kolom_sumber=sumber, operasi=operasi, nama_rincian=rincian, urutan=i
            )
        )
