"""Existing ERP commerce operations exposed through the assistant receipt boundary."""
from typing import Literal
from pydantic import Field
from fastapi import HTTPException
from uuid import uuid5, NAMESPACE_URL
from ..workflow_schemas import Input, DisputeIn, PublishIn
from .. import workflows
from ..commerce_schemas import ShopProfileEdit, ReplyComments, BoostProducts, HolidayEdit, ChannelEdit, AddressConfig, ModelAdd, ModelsInit
from .. import services
from ...infrastructure.adapters import erp_shopee_commerce as commerce, erp_shopee_promotions as promotions, erp_shopee_returns as returns, erp_shopee_workflows as workflow


class Publish(PublishIn):
    # Filled by server validation and hidden from model definitions.
    pass


class Automatic(Input):
    enabled: bool
    branch_id: str | None = Field(None, min_length=1, max_length=64)
    sender_real_name: str | None = Field(None, min_length=1, max_length=255)


class Metadata(Input):
    category_id: int | None = Field(None, gt=0)


class Copy(Input):
    item_id: int = Field(gt=0)


class Empty(Input):
    pass


class Catalog(Input):
    katalog_id: str = Field(min_length=1, max_length=64)


class Comments(Catalog):
    cursor: str = Field('', max_length=500)
    page_size: int = Field(20, ge=1, le=100)


class Reply(Catalog):
    balasan: ReplyComments


class ProductInfo(Catalog):
    jenis: Literal['promosi', 'pelanggaran']


class AddVariants(Catalog):
    perubahan: ModelAdd


class InitVariants(Catalog):
    perubahan: ModelsInit


class DeleteVariant(Catalog):
    model_id: int = Field(gt=0)


class Tracking(Input):
    pesanan_id: str = Field(min_length=1, max_length=64)
    package_number: str | None = Field(None, min_length=1, max_length=100)


class Channel(Input):
    channel_id: int = Field(gt=0)
    perubahan: ChannelEdit


class Return(Input):
    nomor_retur: str = Field(min_length=1, max_length=100)


class Dispute(Return):
    perubahan: DisputeIn


class EndPromotion(Input):
    promosi_id: str = Field(pattern=r'^[1-9][0-9]{0,19}$')
    hapus: bool = False


TOOLS = {
    'baca_ulasan_produk': (Comments, False, 'Baca ulasan produk dan balasan existing; gunakan cursor untuk halaman berikutnya.'),
    'balas_ulasan_produk': (Reply, True, 'Balas ulasan pembeli memakai ID terdaftar dan teks yang diminta admin. Memerlukan konfirmasi; jangan balas ulang tanpa memeriksa hasil.'),
    'daftar_produk_boost': (Empty, False, 'Baca produk yang sedang di-boost dan cooldown pada toko terpilih.'),
    'boost_produk': (BoostProducts, True, 'Boost 1–5 produk katalog pada toko terpilih setelah konfirmasi; baca cooldown, kegagalan parsial harus dijelaskan.'),
    "pengaturan_pesanan_otomatis": (Empty, False, "Baca pengaturan dan hasil pesanan drop off otomatis pada toko terpilih."),
    "atur_pesanan_otomatis": (Automatic, True, "Aktif/nonaktifkan pesanan drop off otomatis untuk toko terpilih, hanya pesanan baru setelah izin aktif. Konfirmasi adalah izin proses tanpa AI/token."),
    'metadata_publikasi': (Metadata, False, 'Baca kategori, atribut dan jasa kirim untuk publikasi produk baru.'),
    'salin_draf_produk': (Copy, False, 'Baca draf produk Shopee; belum mempublikasikan produk.'),
    'publikasikan_produk': (Publish, True, 'Publikasikan produk baru memakai image_ids yang sudah diunggah di ERP. Lengkapi metadata, harga, stok, berat dan varian; jangan mengarang gambar.'),
    'informasi_produk_shopee': (ProductInfo, False, 'Baca promosi atau pelanggaran produk pada toko terpilih.'),
    'hapus_produk_shopee': (Catalog, True, 'Hapus listing Shopee; pertahankan histori ERP. Memerlukan konfirmasi.'),
    'tambah_varian': (AddVariants, True, 'Tambahkan varian produk menggunakan kontrak varian ERP.'),
    'inisialisasi_varian': (InitVariants, True, 'Inisialisasi tier dan semua kombinasi varian produk.'),
    'hapus_varian': (DeleteVariant, True, 'Hapus satu varian Shopee setelah konfirmasi.'),
    'pelacakan_pesanan': (Tracking, False, 'Baca pelacakan live pesanan dan paket Shopee.'),
    'ubah_profil_toko': (ShopProfileEdit, True, 'Ubah nama, logo atau deskripsi toko terpilih.'),
    'ubah_mode_libur': (HolidayEdit, True, 'Aktif/nonaktifkan atau jadwalkan mode libur toko.'),
    'ubah_jasa_kirim': (Channel, True, 'Ubah jasa kirim, COD atau pengaturan panggil driver.'),
    'ubah_alamat_toko': (AddressConfig, True, 'Ubah fungsi alamat toko yang sudah terdaftar.'),
    'ringkasan_pendapatan_shopee': (Empty, False, 'Baca ringkasan pendapatan live Shopee.'),
    'konfirmasi_retur': (Return, True, 'Konfirmasi retur melalui fungsi ERP yang ada.'),
    'alasan_sengketa_retur': (Return, False, 'Baca alasan dan persyaratan bukti sengketa retur.'),
    'ajukan_sengketa_retur': (Dispute, True, 'Ajukan sengketa; bukti harus sudah diunggah melalui ERP, jangan membuat URL bukti.'),
    'akhiri_promosi': (EndPromotion, True, 'Akhiri atau hapus promosi setelah konfirmasi.'),
}


async def execute(session, user, turn, name, payload, akun, fingerprint):
    if name in ("pengaturan_pesanan_otomatis", "atur_pesanan_otomatis"):
        from tenants.marketplace_erp.adapters.api.v1 import auto_order_router as automatic
        if name == "atur_pesanan_otomatis":
            fields = payload.model_dump()
            existing = await session.get(automatic.AutoOrderPolicy, akun.id)
            if existing:
                for field in ("branch_id", "sender_real_name"):
                    if field not in payload.model_fields_set:
                        fields[field] = getattr(existing, field)
            return await automatic.save(automatic.PoliciesIn(items=[automatic.PolicyIn(akun_id=akun.id, **fields)]), session, user)
        data = await automatic.settings(session, user)
        data["items"] = [r for r in data["items"] if r["akun_id"] == akun.id]
        data["attempts"] = [r for r in data["attempts"] if r["akun_id"] == akun.id]
        return data
    if name == "metadata_publikasi":
        return await workflow.metadata(session, akun, payload.category_id)
    if name == "salin_draf_produk":
        return await workflow.copy_draft(session, akun, payload.item_id)
    if name == "publikasikan_produk":
        payload.operation_id = uuid5(NAMESPACE_URL, turn.id + fingerprint)
        return await workflows.publish(session, akun.id, payload)
    # Resolve local identifiers before invoking existing routes; admin-wide access
    # must never bypass the shop pinned to this confirmed assistant turn.
    if isinstance(payload, Catalog):
        row, _ = await services.get_katalog_shopee(session, payload.katalog_id)
        if row.akun_id != akun.id:
            raise HTTPException(403, 'Produk bukan milik toko yang dipilih')
    if isinstance(payload, Tracking):
        row = await services.get_pesanan(session, payload.pesanan_id)
        if row.akun_id != akun.id or row.platform != 'shopee':
            raise HTTPException(403, 'Pesanan bukan milik toko yang dipilih')
    from tenants.marketplace_erp.adapters.api.v1 import commerce_router as routes
    if name == 'baca_ulasan_produk':
        return await routes.product_comments(payload.katalog_id, payload.cursor, payload.page_size, session, user)
    if name == 'balas_ulasan_produk':
        return await routes.reply_comments(payload.katalog_id, payload.balasan, session, user)
    if name == 'daftar_produk_boost':
        return await routes.boosted_products(akun.id, session, user)
    if name == 'boost_produk':
        return await routes.boost_products(akun.id, payload, session, user)
    if name == 'informasi_produk_shopee':
        return await routes.product_details(payload.katalog_id, payload.jenis, session, user)
    if name == 'hapus_produk_shopee':
        return await routes.delete_product(payload.katalog_id, session, user)
    if name == 'tambah_varian':
        return await routes.add_models(payload.katalog_id, payload.perubahan, session, user)
    if name == 'inisialisasi_varian':
        return await routes.init_models(payload.katalog_id, payload.perubahan, session, user)
    if name == 'hapus_varian':
        return await routes.delete_model(payload.katalog_id, payload.model_id, session, user)
    if name == 'pelacakan_pesanan':
        return await routes.tracking(payload.pesanan_id, payload.package_number, session, user)
    if name == 'ubah_profil_toko':
        result = await commerce.acknowledged(session, akun, '/api/v2/shop/update_profile', payload.model_dump(mode='json', exclude_none=True))
        if payload.shop_name is not None:
            akun.nama_toko = payload.shop_name
        await session.flush()
        return result
    if name == 'ubah_mode_libur':
        return await routes.holiday(akun.id, payload, session, user)
    if name == 'ubah_jasa_kirim':
        return await routes.channel(akun.id, payload.perubahan, payload.channel_id, session, user)
    if name == 'ubah_alamat_toko':
        return await routes.address_config(akun.id, payload, session, user)
    if name == 'ringkasan_pendapatan_shopee':
        return await commerce.income_overview(session, akun)
    if name == 'konfirmasi_retur':
        return await returns.konfirmasi_retur(session, akun, payload.nomor_retur)
    if name == 'alasan_sengketa_retur':
        return await workflow.dispute_reasons(session, akun, payload.nomor_retur)
    if name == 'ajukan_sengketa_retur':
        return await workflow.dispute(session, akun, payload.nomor_retur, payload.perubahan)
    if name == 'akhiri_promosi':
        return await promotions.akhiri(session, akun, payload.promosi_id, hapus=payload.hapus)
    raise HTTPException(422, 'Fungsi tidak tersedia')
