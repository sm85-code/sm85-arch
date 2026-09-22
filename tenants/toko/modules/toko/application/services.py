from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shared.security import hash_password, verify_password
from tenants.toko.modules.toko.application.schemas import (
    AlamatIn,
    AlamatPatch,
    KategoriIn,
    PengirimanIn,
    ProdukIn,
    ProdukPatch,
    RegisterRequest,
)
from tenants.toko.modules.toko.infrastructure.models import (
    STATUS_PENGIRIMAN,
    STATUS_PESANAN,
    AlamatToko,
    ItemKeranjang,
    ItemPesanan,
    KategoriToko,
    PercakapanToko,
    PengirimanToko,
    PesanChatToko,
    PesananToko,
    ProdukToko,
    UserToko,
)

# Transisi status pengiriman yang diizinkan.
_TRANSISI_STATUS_PENGIRIMAN = {
    "menunggu_pickup": {"dikirim", "bermasalah"},
    "dikirim": {"diterima", "bermasalah"},
    "diterima": set(),
    "bermasalah": {"dikirim"},
}

# Status pesanan yang dihitung sebagai penjualan sah untuk laporan --
# menunggu_pembayaran & dibatalkan tidak dihitung (belum tentu jadi uang).
_STATUS_TERHITUNG_PENJUALAN = ("dibayar", "diproses", "dikirim", "selesai")

# Transisi status yang diizinkan. dibatalkan bisa dari status manapun
# sebelum selesai/dikirim (pembatalan setelah dikirim harus lewat proses
# retur, bukan sekadar ubah status -- di luar scope modul ini).
_TRANSISI_STATUS = {
    "menunggu_pembayaran": {"dibayar", "dibatalkan"},
    "dibayar": {"diproses", "dibatalkan"},
    "diproses": {"dikirim"},
    "dikirim": {"selesai"},
    "selesai": set(),
    "dibatalkan": set(),
}


def user_out(user: UserToko) -> dict:
    return {"id": user.id, "nama": user.nama, "email": user.email, "role": user.role}


def produk_out(produk: ProdukToko) -> dict:
    return {
        "id": produk.id,
        "nama": produk.nama,
        "deskripsi": produk.deskripsi,
        "kategori_id": produk.kategori_id,
        "kategori_nama": produk.kategori.nama if produk.kategori else None,
        "harga": str(produk.harga),
        "stok": produk.stok,
        "foto_url": produk.foto_url,
        "aktif": produk.aktif,
    }


async def authenticate(session: AsyncSession, email: str, password: str) -> UserToko:
    user = (await session.execute(select(UserToko).where(UserToko.email == email))).scalar_one_or_none()
    if not user or not verify_password(password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Email atau password salah")
    return user


async def register(session: AsyncSession, payload: RegisterRequest) -> UserToko:
    existing = (await session.execute(select(UserToko).where(UserToko.email == payload.email))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email sudah terdaftar")
    user = UserToko(
        nama=payload.nama,
        email=payload.email,
        password_hash=hash_password(payload.password),
        role="pembeli",
    )
    session.add(user)
    await session.flush()
    return user


async def list_produk(session: AsyncSession, *, hanya_aktif: bool = False) -> list[ProdukToko]:
    stmt = select(ProdukToko).options(selectinload(ProdukToko.kategori)).order_by(ProdukToko.created_at.desc())
    if hanya_aktif:
        stmt = stmt.where(ProdukToko.aktif.is_(True))
    return list((await session.execute(stmt)).scalars().all())


async def get_produk(session: AsyncSession, produk_id: str) -> ProdukToko:
    stmt = select(ProdukToko).where(ProdukToko.id == produk_id).options(selectinload(ProdukToko.kategori))
    produk = (await session.execute(stmt)).scalar_one_or_none()
    if not produk:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Produk tidak ditemukan")
    return produk


async def create_produk(session: AsyncSession, payload: ProdukIn) -> ProdukToko:
    produk = ProdukToko(**payload.model_dump())
    session.add(produk)
    await session.flush()
    return produk


async def update_produk(session: AsyncSession, produk_id: str, payload: ProdukPatch) -> ProdukToko:
    produk = await get_produk(session, produk_id)
    fields = payload.model_dump(exclude_unset=True)
    for field, value in fields.items():
        setattr(produk, field, value)
    await session.flush()
    if "kategori_id" in fields:
        # Relationship was eager-loaded before the FK changed -- refresh so
        # produk_out() reflects the new kategori, not the stale cached one.
        await session.refresh(produk, attribute_names=["kategori"])
    return produk


async def delete_produk(session: AsyncSession, produk_id: str) -> None:
    produk = await get_produk(session, produk_id)
    await session.delete(produk)


def keranjang_item_out(item: ItemKeranjang) -> dict:
    return {
        "produk_id": item.produk_id,
        "nama": item.produk.nama,
        "harga": str(item.produk.harga),
        "qty": item.qty,
        "subtotal": str(item.produk.harga * item.qty),
        "stok_tersedia": item.produk.stok,
    }


def pesanan_out(pesanan: PesananToko) -> dict:
    return {
        "id": pesanan.id,
        "status": pesanan.status,
        "total": str(pesanan.total),
        "metode_pembayaran": pesanan.metode_pembayaran,
        "created_at": pesanan.created_at.isoformat(),
        "items": [
            {
                "produk_id": it.produk_id,
                "nama_produk": it.nama_produk,
                "harga_satuan": str(it.harga_satuan),
                "qty": it.qty,
                "subtotal": str(it.subtotal),
            }
            for it in pesanan.items
        ],
    }


async def get_keranjang(session: AsyncSession, user_id: str) -> list[ItemKeranjang]:
    stmt = (
        select(ItemKeranjang)
        .where(ItemKeranjang.user_id == user_id)
        .options(selectinload(ItemKeranjang.produk))
    )
    return list((await session.execute(stmt)).scalars().all())


async def tambah_ke_keranjang(session: AsyncSession, user_id: str, produk_id: str, qty: int) -> ItemKeranjang:
    if qty < 1:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Qty minimal 1")
    produk = await get_produk(session, produk_id)
    if not produk.aktif:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Produk tidak tersedia")

    stmt = select(ItemKeranjang).where(ItemKeranjang.user_id == user_id, ItemKeranjang.produk_id == produk_id)
    item = (await session.execute(stmt)).scalar_one_or_none()
    if item:
        item.qty += qty
    else:
        item = ItemKeranjang(user_id=user_id, produk_id=produk_id, qty=qty)
        session.add(item)
    await session.flush()
    await session.refresh(item, attribute_names=["produk"])
    return item


async def ubah_qty_keranjang(session: AsyncSession, user_id: str, produk_id: str, qty: int) -> ItemKeranjang:
    if qty < 1:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Qty minimal 1 (hapus item untuk qty 0)")
    stmt = select(ItemKeranjang).where(ItemKeranjang.user_id == user_id, ItemKeranjang.produk_id == produk_id)
    item = (await session.execute(stmt)).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item keranjang tidak ditemukan")
    item.qty = qty
    await session.flush()
    await session.refresh(item, attribute_names=["produk"])
    return item


async def hapus_dari_keranjang(session: AsyncSession, user_id: str, produk_id: str) -> None:
    stmt = select(ItemKeranjang).where(ItemKeranjang.user_id == user_id, ItemKeranjang.produk_id == produk_id)
    item = (await session.execute(stmt)).scalar_one_or_none()
    if item:
        await session.delete(item)
        await session.flush()


async def checkout(session: AsyncSession, user_id: str) -> PesananToko:
    items = await get_keranjang(session, user_id)
    if not items:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Keranjang kosong")

    for item in items:
        if not item.produk.aktif:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=f"Produk '{item.produk.nama}' sudah tidak tersedia"
            )
        if item.produk.stok < item.qty:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Stok '{item.produk.nama}' tidak cukup (tersisa {item.produk.stok})",
            )

    pesanan = PesananToko(user_id=user_id, status="menunggu_pembayaran", total=Decimal("0"))
    session.add(pesanan)
    await session.flush()

    total = Decimal("0")
    for item in items:
        subtotal = item.produk.harga * item.qty
        total += subtotal
        session.add(
            ItemPesanan(
                pesanan_id=pesanan.id,
                produk_id=item.produk_id,
                nama_produk=item.produk.nama,
                harga_satuan=item.produk.harga,
                qty=item.qty,
                subtotal=subtotal,
            )
        )
        item.produk.stok -= item.qty
        await session.delete(item)

    pesanan.total = total
    await session.flush()
    await session.refresh(pesanan, attribute_names=["items"])
    return pesanan


async def get_pesanan(session: AsyncSession, pesanan_id: str) -> PesananToko:
    stmt = (
        select(PesananToko)
        .where(PesananToko.id == pesanan_id)
        .options(selectinload(PesananToko.items))
    )
    pesanan = (await session.execute(stmt)).scalar_one_or_none()
    if not pesanan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pesanan tidak ditemukan")
    return pesanan


async def list_pesanan_milik(session: AsyncSession, user_id: str) -> list[PesananToko]:
    stmt = (
        select(PesananToko)
        .where(PesananToko.user_id == user_id)
        .options(selectinload(PesananToko.items))
        .order_by(PesananToko.created_at.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_semua_pesanan(session: AsyncSession) -> list[PesananToko]:
    stmt = select(PesananToko).options(selectinload(PesananToko.items)).order_by(PesananToko.created_at.desc())
    return list((await session.execute(stmt)).scalars().all())


async def ubah_status_pesanan(session: AsyncSession, pesanan_id: str, status_baru: str) -> PesananToko:
    if status_baru not in STATUS_PESANAN:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Status tidak dikenal")
    pesanan = await get_pesanan(session, pesanan_id)
    if status_baru not in _TRANSISI_STATUS.get(pesanan.status, set()):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Tidak bisa ubah status dari '{pesanan.status}' ke '{status_baru}'",
        )
    pesanan.status = status_baru
    await session.flush()
    return pesanan


async def catat_metode_pembayaran(session: AsyncSession, pesanan_id: str, *, metode: str, gateway_ref: str | None) -> PesananToko:
    """Dipanggil setelah adapter payment gateway (mis. iPaymu) berhasil
    membuat sesi pembayaran, sebelum pengguna diarahkan ke checkout_url."""
    pesanan = await get_pesanan(session, pesanan_id)
    pesanan.metode_pembayaran = metode
    pesanan.gateway_ref = gateway_ref
    await session.flush()
    return pesanan


async def tandai_dibayar_dari_webhook(session: AsyncSession, gateway_ref: str) -> PesananToko | None:
    """Dipanggil dari endpoint webhook payment gateway. Mencari pesanan
    berdasarkan gateway_ref (bukan pesanan_id, karena provider tidak selalu
    tahu ID internal kita) lalu jalankan transisi status yang sama seperti
    ubah_status_pesanan -- supaya aturan transisi tetap satu tempat."""
    stmt = select(PesananToko).where(PesananToko.gateway_ref == gateway_ref)
    pesanan = (await session.execute(stmt)).scalar_one_or_none()
    if not pesanan:
        return None
    if "dibayar" in _TRANSISI_STATUS.get(pesanan.status, set()):
        pesanan.status = "dibayar"
        await session.flush()
    return pesanan


def pengiriman_out(pengiriman: PengirimanToko) -> dict:
    return {
        "id": pengiriman.id,
        "pesanan_id": pengiriman.pesanan_id,
        "kurir": pengiriman.kurir,
        "layanan": pengiriman.layanan,
        "ongkir": str(pengiriman.ongkir),
        "nama_penerima": pengiriman.nama_penerima,
        "telepon_penerima": pengiriman.telepon_penerima,
        "alamat_tujuan": pengiriman.alamat_tujuan,
        "tracking_id": pengiriman.tracking_id,
        "status": pengiriman.status,
    }


async def buat_pengiriman_lokal(session: AsyncSession, pesanan_id: str, payload: PengirimanIn) -> PengirimanToko:
    """Catat data pengiriman (termasuk ongkir yang dipilih pembeli dari hasil
    cek-ongkir) di database kita. Pemanggilan API Biteship yang sesungguhnya
    (assign kurir, dapat tracking_id) ada di infrastructure/
    shipping_biteship.py -- belum terhubung, lihat docstring di sana."""
    await get_pesanan(session, pesanan_id)
    existing = (
        await session.execute(select(PengirimanToko).where(PengirimanToko.pesanan_id == pesanan_id))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Pesanan ini sudah punya data pengiriman")

    pengiriman = PengirimanToko(pesanan_id=pesanan_id, **payload.model_dump())
    session.add(pengiriman)
    await session.flush()
    return pengiriman


async def get_pengiriman(session: AsyncSession, pesanan_id: str) -> PengirimanToko:
    stmt = select(PengirimanToko).where(PengirimanToko.pesanan_id == pesanan_id)
    pengiriman = (await session.execute(stmt)).scalar_one_or_none()
    if not pengiriman:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Data pengiriman tidak ditemukan")
    return pengiriman


async def ubah_status_pengiriman(
    session: AsyncSession, pesanan_id: str, status_baru: str, tracking_id: str | None = None
) -> PengirimanToko:
    if status_baru not in STATUS_PENGIRIMAN:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Status pengiriman tidak dikenal")
    pengiriman = await get_pengiriman(session, pesanan_id)
    if status_baru not in _TRANSISI_STATUS_PENGIRIMAN.get(pengiriman.status, set()):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Tidak bisa ubah status pengiriman dari '{pengiriman.status}' ke '{status_baru}'",
        )
    pengiriman.status = status_baru
    if tracking_id:
        pengiriman.tracking_id = tracking_id
    await session.flush()

    # Status pengiriman & status pesanan disinkronkan otomatis, supaya admin
    # tidak perlu update keduanya terpisah secara manual: pengiriman
    # "dikirim" mendorong pesanan ke "dikirim", dan "diterima" mendorong
    # pesanan ke "selesai" -- masing-masing hanya kalau pesanan sedang di
    # status yang mengizinkan transisi itu (lihat _TRANSISI_STATUS).
    status_pesanan_target = {"dikirim": "dikirim", "diterima": "selesai"}.get(status_baru)
    if status_pesanan_target:
        pesanan = await get_pesanan(session, pesanan_id)
        if status_pesanan_target in _TRANSISI_STATUS.get(pesanan.status, set()):
            pesanan.status = status_pesanan_target
            await session.flush()

    return pengiriman


async def laporan_penjualan(session: AsyncSession, dari: date, sampai: date) -> dict:
    """Total penjualan per hari, dalam rentang [dari, sampai] inklusif.
    Hanya menghitung pesanan berstatus dibayar/diproses/dikirim/selesai --
    lihat _STATUS_TERHITUNG_PENJUALAN."""
    stmt = (
        select(
            func.date(PesananToko.created_at).label("tanggal"),
            func.count(PesananToko.id).label("jumlah_pesanan"),
            func.sum(PesananToko.total).label("total_penjualan"),
        )
        .where(
            PesananToko.status.in_(_STATUS_TERHITUNG_PENJUALAN),
            func.date(PesananToko.created_at) >= dari,
            func.date(PesananToko.created_at) <= sampai,
        )
        .group_by(func.date(PesananToko.created_at))
        .order_by(func.date(PesananToko.created_at))
    )
    rows = (await session.execute(stmt)).all()
    harian = [
        {
            "tanggal": r.tanggal if isinstance(r.tanggal, str) else r.tanggal.isoformat(),
            "jumlah_pesanan": r.jumlah_pesanan,
            "total_penjualan": str(r.total_penjualan or Decimal("0")),
        }
        for r in rows
    ]
    grand_total = sum((Decimal(h["total_penjualan"]) for h in harian), Decimal("0"))
    return {"dari": dari.isoformat(), "sampai": sampai.isoformat(), "harian": harian, "grand_total": str(grand_total)}


async def laporan_produk_terlaris(session: AsyncSession, dari: date, sampai: date, limit: int = 10) -> list[dict]:
    stmt = (
        select(
            ItemPesanan.produk_id,
            ItemPesanan.nama_produk,
            func.sum(ItemPesanan.qty).label("total_qty"),
            func.sum(ItemPesanan.subtotal).label("total_omzet"),
        )
        .join(PesananToko, PesananToko.id == ItemPesanan.pesanan_id)
        .where(
            PesananToko.status.in_(_STATUS_TERHITUNG_PENJUALAN),
            func.date(PesananToko.created_at) >= dari,
            func.date(PesananToko.created_at) <= sampai,
        )
        .group_by(ItemPesanan.produk_id, ItemPesanan.nama_produk)
        .order_by(func.sum(ItemPesanan.qty).desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [
        {
            "produk_id": r.produk_id,
            "nama_produk": r.nama_produk,
            "total_qty": r.total_qty,
            "total_omzet": str(r.total_omzet or Decimal("0")),
        }
        for r in rows
    ]


async def laporan_ringkasan_status(session: AsyncSession) -> dict:
    stmt = select(PesananToko.status, func.count(PesananToko.id)).group_by(PesananToko.status)
    rows = (await session.execute(stmt)).all()
    return {status_: jumlah for status_, jumlah in rows}


# --- Kategori ---------------------------------------------------------


def kategori_out(kategori: KategoriToko) -> dict:
    return {"id": kategori.id, "nama": kategori.nama}


async def list_kategori(session: AsyncSession) -> list[KategoriToko]:
    stmt = select(KategoriToko).order_by(KategoriToko.nama)
    return list((await session.execute(stmt)).scalars().all())


async def create_kategori(session: AsyncSession, payload: KategoriIn) -> KategoriToko:
    existing = (
        await session.execute(select(KategoriToko).where(KategoriToko.nama == payload.nama))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Kategori sudah ada")
    kategori = KategoriToko(nama=payload.nama)
    session.add(kategori)
    await session.flush()
    return kategori


async def delete_kategori(session: AsyncSession, kategori_id: str) -> None:
    kategori = await session.get(KategoriToko, kategori_id)
    if not kategori:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Kategori tidak ditemukan")
    # Produk yang masih pakai kategori ini otomatis jadi kategori_id=NULL
    # (ON DELETE SET NULL di model) -- tidak ikut terhapus.
    await session.delete(kategori)


# --- Alamat (buku alamat pembeli) --------------------------------------


def alamat_out(alamat: AlamatToko) -> dict:
    return {
        "id": alamat.id,
        "label": alamat.label,
        "nama_penerima": alamat.nama_penerima,
        "telepon_penerima": alamat.telepon_penerima,
        "alamat_lengkap": alamat.alamat_lengkap,
        "kota": alamat.kota,
        "provinsi": alamat.provinsi,
        "kode_pos": alamat.kode_pos,
        "utama": alamat.utama,
    }


async def list_alamat(session: AsyncSession, user_id: str) -> list[AlamatToko]:
    stmt = select(AlamatToko).where(AlamatToko.user_id == user_id).order_by(AlamatToko.utama.desc(), AlamatToko.created_at.desc())
    return list((await session.execute(stmt)).scalars().all())


async def _unset_other_utama(session: AsyncSession, user_id: str, kecuali_id: str | None = None) -> None:
    stmt = select(AlamatToko).where(AlamatToko.user_id == user_id, AlamatToko.utama.is_(True))
    if kecuali_id:
        stmt = stmt.where(AlamatToko.id != kecuali_id)
    for row in (await session.execute(stmt)).scalars().all():
        row.utama = False


async def create_alamat(session: AsyncSession, user_id: str, payload: AlamatIn) -> AlamatToko:
    is_first = len((await session.execute(select(AlamatToko).where(AlamatToko.user_id == user_id)))
                    .scalars().all()) == 0
    alamat = AlamatToko(user_id=user_id, **payload.model_dump())
    if is_first:
        alamat.utama = True  # alamat pertama otomatis jadi utama
    session.add(alamat)
    await session.flush()
    if alamat.utama:
        await _unset_other_utama(session, user_id, kecuali_id=alamat.id)
        await session.flush()
    return alamat


async def _get_alamat_milik(session: AsyncSession, user_id: str, alamat_id: str) -> AlamatToko:
    alamat = await session.get(AlamatToko, alamat_id)
    if not alamat or alamat.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alamat tidak ditemukan")
    return alamat


async def update_alamat(session: AsyncSession, user_id: str, alamat_id: str, payload: AlamatPatch) -> AlamatToko:
    alamat = await _get_alamat_milik(session, user_id, alamat_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(alamat, field, value)
    await session.flush()
    if alamat.utama:
        await _unset_other_utama(session, user_id, kecuali_id=alamat.id)
        await session.flush()
    return alamat


async def delete_alamat(session: AsyncSession, user_id: str, alamat_id: str) -> None:
    alamat = await _get_alamat_milik(session, user_id, alamat_id)
    await session.delete(alamat)


# --- Login Google --------------------------------------------------------


async def login_or_register_google(session: AsyncSession, *, google_sub: str, email: str, nama: str) -> UserToko:
    """Cari user berdasarkan google_sub dulu (identitas stabil), baru fallback
    ke email untuk menautkan akun password biasa yang emailnya sama dengan
    akun Google-nya. Kalau tidak ada keduanya, daftarkan akun baru."""
    user = (
        await session.execute(select(UserToko).where(UserToko.google_sub == google_sub))
    ).scalar_one_or_none()
    if user:
        return user

    user = (await session.execute(select(UserToko).where(UserToko.email == email))).scalar_one_or_none()
    if user:
        user.google_sub = google_sub
        await session.flush()
        return user

    user = UserToko(nama=nama, email=email, google_sub=google_sub, role="pembeli")
    session.add(user)
    await session.flush()
    return user


# --- Chat (toko web, terpisah dari chat Shopee) --------------------------


async def get_or_create_percakapan(session: AsyncSession, user_id: str) -> PercakapanToko:
    stmt = select(PercakapanToko).where(PercakapanToko.user_id == user_id)
    percakapan = (await session.execute(stmt)).scalar_one_or_none()
    if not percakapan:
        percakapan = PercakapanToko(user_id=user_id)
        session.add(percakapan)
        await session.flush()
    return percakapan


async def get_percakapan(session: AsyncSession, percakapan_id: str) -> PercakapanToko:
    stmt = (
        select(PercakapanToko)
        .where(PercakapanToko.id == percakapan_id)
        .options(selectinload(PercakapanToko.pesan), selectinload(PercakapanToko.pembeli))
    )
    percakapan = (await session.execute(stmt)).scalar_one_or_none()
    if not percakapan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Percakapan tidak ditemukan")
    return percakapan


async def list_percakapan_admin(session: AsyncSession) -> list[PercakapanToko]:
    stmt = (
        select(PercakapanToko)
        .options(selectinload(PercakapanToko.pembeli))
        .order_by(PercakapanToko.updated_at.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def kirim_pesan(session: AsyncSession, percakapan_id: str, pengirim: UserToko, isi: str) -> PesanChatToko:
    percakapan = await session.get(PercakapanToko, percakapan_id)
    if not percakapan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Percakapan tidak ditemukan")
    is_admin = (pengirim.role or "").lower() in ("admin_toko", "owner")
    pesan = PesanChatToko(percakapan_id=percakapan_id, pengirim_id=pengirim.id, pengirim_admin=is_admin, isi=isi)
    session.add(pesan)
    percakapan.unread_admin = not is_admin
    percakapan.unread_pembeli = is_admin
    await session.flush()
    # percakapan.pesan bisa saja sudah ter-selectinload sebelumnya di
    # identity map sesi ini (mis. dari get_percakapan) -- expire supaya
    # pembacaan berikutnya benar-benar mengambil ulang termasuk pesan baru
    # ini, bukan koleksi lama yang ter-cache.
    session.expire(percakapan, ["pesan"])
    return pesan


async def tandai_dibaca(session: AsyncSession, percakapan_id: str, sebagai_admin: bool) -> None:
    percakapan = await session.get(PercakapanToko, percakapan_id)
    if not percakapan:
        return
    if sebagai_admin:
        percakapan.unread_admin = False
    else:
        percakapan.unread_pembeli = False
    await session.flush()


def pesan_out(pesan: PesanChatToko) -> dict:
    return {
        "id": pesan.id,
        "pengirim_admin": pesan.pengirim_admin,
        "isi": pesan.isi,
        "created_at": pesan.created_at.isoformat(),
    }


def percakapan_out(percakapan: PercakapanToko, *, dengan_pesan: bool = False) -> dict:
    out = {
        "id": percakapan.id,
        "user_id": percakapan.user_id,
        "nama_pembeli": percakapan.pembeli.nama if percakapan.pembeli else None,
        "unread_admin": percakapan.unread_admin,
        "unread_pembeli": percakapan.unread_pembeli,
        "updated_at": percakapan.updated_at.isoformat(),
    }
    if dengan_pesan:
        out["pesan"] = [pesan_out(p) for p in percakapan.pesan]
    return out
