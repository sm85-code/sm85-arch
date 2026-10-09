"""Finite typed tools. Shop is pinned by the user, never by model-supplied URLs."""
import json
from datetime import date, datetime, time
from decimal import Decimal
from typing import Literal
from uuid import uuid5, NAMESPACE_URL
from zoneinfo import ZoneInfo
from pydantic import Field
from fastapi import HTTPException
from ..workflow_schemas import Input
from pydantic import model_validator
from .. import services
from ..management_schemas import ItemEdit, ModelsEdit, TiersEdit, GmvCreate, GmvEdit, GmvItems
from ..schemas import PromosiIn, PromosiUpdateIn, PromosiProdukIn
from ...infrastructure.adapters import erp_shopee_management as management, erp_shopee_insights as insights, erp_shopee_promotions as promotions
from ...infrastructure import ads_ai


class Empty(Input):
    pass


class Page(Input):
    halaman: int = Field(default=1, ge=1, le=1000)


class CatalogSearch(Page):
    q: str | None = Field(default=None, max_length=100)


class CatalogRef(Input):
    katalog_id: str = Field(min_length=1, max_length=64)


class CategoryMetadata(CatalogRef):
    category_id: int | None = Field(default=None, gt=0)


class ItemChange(CatalogRef):
    perubahan: ItemEdit


class ModelChange(CatalogRef):
    perubahan: ModelsEdit


class TierChange(CatalogRef):
    perubahan: TiersEdit


class AdsPeriod(Input):
    hari: int = Field(default=7, ge=2, le=28)


class AdsChange(Input):
    campaign_id: int = Field(gt=0)
    aksi: Literal["pause", "resume", "change_budget", "change_roas_target"]
    budget: Decimal | None = Field(default=None, gt=0, le=100000000, allow_inf_nan=False)
    roas_target: Decimal | None = Field(default=None, gt=0, le=100, allow_inf_nan=False)


class Keyword(Input):
    aksi: Literal["add", "delete", "change_bid_price", "change_match_type"]
    kata: str = Field(min_length=1, max_length=100)
    tipe: Literal["exact", "broad"] | None = None
    bid: Decimal | None = Field(default=None, gt=0, le=100000, allow_inf_nan=False)


class AdsCreate(CatalogRef):
    bidding: Literal["manual", "auto"]
    budget: Decimal = Field(gt=0, le=100000000, allow_inf_nan=False)
    mulai: date
    roas_target: Decimal | None = Field(default=None, gt=0, le=100, allow_inf_nan=False)
    kata_kunci: list[Keyword] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def explicit_values(self):
        from .config import day
        if self.mulai < day() or (self.bidding == "manual" and not self.kata_kunci):
            raise ValueError("Jadwal/keyword iklan tidak lengkap")
        return self


class KeywordChange(Input):
    campaign_id: int = Field(gt=0)
    kata_kunci: list[Keyword] = Field(min_length=1, max_length=20)


class GmvReport(Input):
    campaign_id: int = Field(gt=0)
    mulai: date
    selesai: date
    per_produk: bool = False
    offset: int = Field(default=0, ge=0)


class PromoList(Page):
    status: Literal["all", "upcoming", "ongoing", "expired"] = "all"


class PromoRef(Page):
    promosi_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")


class PromoChange(Input):
    promosi_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    perubahan: PromosiUpdateIn


class PromoProducts(Input):
    promosi_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    perubahan: PromosiProdukIn


class SummaryPeriod(Input):
    mulai: date
    selesai: date


# name: (input model, writes, description). No official campaign nomination/enrollment.
TOOLS = {
    "daftar_toko": (Empty, False, "Daftar nama dan ID toko ERP tanpa credential."),
    "cari_produk": (CatalogSearch, False, "Cari katalog toko terpilih atau seluruh toko; 20 produk per halaman. Snapshot ERP, bukan stok live."),
    "statistik_produk": (CatalogRef, False, "Statistik live Shopee: views 30 hari, sale kumulatif, rating; bukan skor kualitas."),
    "diagnosis_produk": (CatalogRef, False, "Diagnosis kualitas konten dan masalah/saran resmi Shopee."),
    "metadata_produk": (CategoryMetadata, False, "Atribut/aturan kategori produk. Gunakan sebelum mengubah kategori/atribut; category_id kosong memakai kategori saat ini."),
    "pengaturan_produk": (CatalogRef, False, "Baca informasi, model dan pilihan varian live sebelum mengusulkan perubahan."),
    "performa_toko": (Empty, False, "Indikator kesehatan live dari Account Health toko terpilih."),
    "riwayat_penalti": (Page, False, "Poin penalti kuartal berjalan, paginated."),
    "ringkasan_usaha": (SummaryPeriod, False, "Ringkasan pesanan/penjualan berdasarkan snapshot ERP dan periode WIB, bukan laba bersih."),
    "daftar_iklan": (AdsPeriod, False, "Kampanye iklan produk, pengaturan dan kinerja toko terpilih. Bedakan manual/auto dengan Shop GMV Max."),
    "kelayakan_gmv": (Empty, False, "Cek apakah toko diizinkan membuat Shop GMV Max."),
    "laporan_gmv": (GmvReport, False, "Performa Shop GMV Max kampanye/per produk. Tiga bulan dalam enam bulan terakhir."),
    "daftar_promosi": (PromoList, False, "Daftar diskon toko; bukan pendaftaran kampanye resmi marketplace."),
    "detail_promosi": (PromoRef, False, "Detail promosi diskon dan produk/varian, paginated."),
    "ubah_produk": (ItemChange, True, "Edit field yang diminta saja. Baca pengaturan dulu. Berat/dimensi induk menimpa seluruh model: konfirmasi eksplisit pengguna diperlukan."),
    "ubah_model": (ModelChange, True, "Edit SKU/pengiriman/GTIN model existing; identitas model diperiksa pada toko."),
    "ubah_pilihan_varian": (TierChange, True, "Ubah nama/standardisasi pilihan existing, pertahankan indeks, semua model dan image_id."),
    "buat_iklan": (AdsCreate, True, "Buat iklan produk manual/auto dengan budget/jadwal eksplisit; gunakan katalog toko terpilih, bukan Shop GMV Max."),
    "ubah_iklan": (AdsChange, True, "Jeda/lanjutkan atau ubah budget/ROAS iklan produk existing. Jangan gunakan untuk Shop GMV Max."),
    "ubah_kata_kunci": (KeywordChange, True, "Kelola keyword/bid kampanye manual existing sesuai instruksi pengguna."),
    "buat_gmv": (GmvCreate, True, "Buat Shop GMV Max hanya jika admin menyebut budget, jadwal, dan menginstruksikan membuat; cek eligibility."),
    "ubah_gmv": (GmvEdit, True, "Jeda/lanjut/mulai/ubah budget/jadwal/ROAS Shop GMV Max existing sesuai instruksi."),
    "produk_gmv": (GmvItems, True, "Tambah/hapus produk dalam Shop GMV Max, hanya toko terpilih."),
    "buat_promosi": (PromosiIn, True, "Buat diskon toko dengan nama/jadwal eksplisit. Tidak mendaftarkan kampanye resmi Shopee."),
    "ubah_promosi": (PromoChange, True, "Ubah nama/jadwal diskon existing; pembatasan status diterapkan oleh ERP."),
    "produk_promosi": (PromoProducts, True, "Tambah/ubah/hapus produk/varian diskon; harga/stok/batas sesuai instruksi admin."),
}


def encode(value):
    return json.dumps(value, ensure_ascii=False, default=lambda v: str(v) if isinstance(v, (Decimal, date, datetime)) else v.model_dump(mode="json"), allow_nan=False)


def definitions(writes):
    result = []
    for name, (schema, write, description) in TOOLS.items():
        if write and not writes:
            continue
        spec = schema.model_json_schema()
        # Operation reference belongs to the server receipt, not generated by AI.
        for node in [spec, *spec.get("$defs", {}).values()]:
            node.get("properties", {}).pop("reference_id", None)
            if "required" in node:
                node["required"] = [v for v in node["required"] if v != "reference_id"]
        result.append({"name": name, "description": description, "input_schema": spec})
    return result


def validate(name, arguments, reference):
    if name not in TOOLS:
        raise HTTPException(422, "Fungsi AI tidak tersedia")
    schema = TOOLS[name][0]
    args = dict(arguments)
    if schema in (GmvCreate, GmvEdit):
        args["reference_id"] = reference
    return schema.model_validate(args)


async def execute(session, user, turn, name, payload, fingerprint):
    if user.role != "admin" or user.session_version != turn.session_version:
        raise HTTPException(403, "Akses admin/sesi berubah; eksekusi dihentikan")
    write = TOOLS[name][1]
    if write and (turn.mode != "perintah" or not turn.akun_id):
        raise HTTPException(403, "Mode Tanya tidak mengizinkan perubahan")
    if name == "daftar_toko":
        return [{"id": a.id, "nama": a.nama_toko, "platform": a.platform} for a in await services.list_akun_marketplace(session)]
    if name == "cari_produk":
        result = await services.list_katalog_shopee(session, akun_id=turn.akun_id, q=payload.q, halaman=payload.halaman, per_halaman=20)
        result["items"] = [{k: r.get(k) for k in ("id", "akun_id", "nama_toko", "item_id", "nama", "sku", "harga_min", "harga_max", "stok_shopee", "status", "diambil_at")} for r in result["items"]]
        return result
    if name == "ringkasan_usaha":
        if payload.mulai > payload.selesai or (payload.selesai - payload.mulai).days > 92:
            raise HTTPException(422, "Pilih periode maksimal 93 hari")
        zona = ZoneInfo("Asia/Jakarta")
        r = await services.laporan_dashboard(session, dari=datetime.combine(payload.mulai, time.min, zona), sampai=datetime.combine(payload.selesai, time.max, zona), akun_diizinkan=[turn.akun_id] if turn.akun_id else None)
        return {k: v for k, v in r.items() if k not in ("per_hari", "stok_kritis", "produk_terlaris")}
    if not turn.akun_id:
        raise HTTPException(422, "Pilih toko sebelum membaca data live atau menjalankan tindakan")
    akun = await services.akun_shopee_pengelolaan(session, turn.akun_id)
    if isinstance(payload, CatalogRef):
        katalog, _ = await services.get_katalog_shopee(session, payload.katalog_id)
        if katalog.akun_id != akun.id:
            raise HTTPException(403, "Produk tidak berasal dari toko yang dipilih")
        if name == "metadata_produk":
            current = await management.item(session, akun, katalog.item_id)
            meta = await management.workflows.metadata(session, akun, payload.category_id or current["category_id"])
            meta["categories"] = [c for c in meta["categories"] if c.get("category_id") == (payload.category_id or current["category_id"])]
            return meta
        if name == "statistik_produk":
            return await insights.product_stats(session, akun, katalog.item_id)
        if name == "diagnosis_produk":
            return await insights.diagnosis(session, akun, [int(katalog.item_id)])
        if name == "pengaturan_produk":
            parent = await management.item(session, akun, katalog.item_id)
            fields = {k: parent.get(k) for k in ("item_id", "item_name", "item_sku", "description", "category_id", "attribute_list", "brand", "image", "weight", "dimension", "pre_order", "has_model")}
            if parent.get("has_model"):
                fields["models"] = await management.workflows.read(session, akun, "/api/v2/product/get_model_list", {"item_id": int(katalog.item_id)})
            return fields
        if name == "buat_iklan":
            body = payload.model_dump(mode="json", exclude={"katalog_id"}, exclude_none=True)
            body["item_id"] = int(katalog.item_id)
            body["reference_id"] = str(uuid5(NAMESPACE_URL, turn.id + fingerprint))
            return await services.ubah_iklan_shopee(session, akun, "buat", None, body, user)
        if name == "ubah_produk":
            result = await management.update_item(session, akun, katalog.item_id, payload.perubahan)
        else:
            result = await management.update_variants(session, akun, katalog.item_id, payload.perubahan, tiers=name == "ubah_pilihan_varian")
        # The mutation receipt is completed before the optional local refresh.
        result["warnings"].append("Sinkronkan produk untuk memperbarui snapshot katalog ERP.")
        return result
    if name == "performa_toko":
        return await insights.shop_performance(session, akun)
    if name == "riwayat_penalti":
        return await insights.penalties(session, akun, payload.halaman)
    if name == "daftar_iklan":
        r = await services.daftar_kampanye_iklan(session, akun, payload.hari)
        return {"hari": r.get("hari"), "saldo": r.get("saldo"), "kampanye": ads_ai.ringkas_kampanye(r.get("kampanye", [])), "dibatasi": 40}
    if name in ("ubah_iklan", "ubah_kata_kunci"):
        live = await services.daftar_kampanye_iklan(session, akun, 7)
        campaign = next((r for r in live.get("kampanye", []) if str(r.get("campaign_id")) == str(payload.campaign_id)), None)
        if campaign is None:
            raise HTTPException(409, "Kampanye tidak ditemukan di toko ini")
        if name == "ubah_kata_kunci" and campaign.get("bidding") != "manual":
            raise HTTPException(422, "Kata kunci hanya untuk iklan manual")
        body = payload.model_dump(mode="json", exclude={"campaign_id"}, exclude_none=True)
        body["reference_id"] = str(uuid5(NAMESPACE_URL, turn.id + fingerprint))
        return await services.ubah_iklan_shopee(session, akun, "aksi" if name == "ubah_iklan" else "kata_kunci", payload.campaign_id, body, user)
    if name == "kelayakan_gmv":
        return await management.eligibility(session, akun)
    if name == "laporan_gmv":
        return await management.gmv_performance(session, akun, payload.campaign_id, payload.mulai, payload.selesai, payload.offset, 25, payload.per_produk)
    if name in ("buat_gmv", "ubah_gmv", "produk_gmv"):
        return await management.gmv_mutation(session, akun, payload, {"buat_gmv": "create", "ubah_gmv": "edit", "produk_gmv": "items"}[name])
    if name == "daftar_promosi":
        return await promotions.daftar(session, akun, payload.status, payload.halaman)
    if name == "detail_promosi":
        return await promotions.detail(session, akun, payload.promosi_id, payload.halaman)
    if name == "buat_promosi":
        return await promotions.buat(session, akun, payload)
    if name == "ubah_promosi":
        return await promotions.ubah(session, akun, payload.promosi_id, payload.perubahan)
    if name == "produk_promosi":
        return await services.kelola_barang_promosi(session, akun.id, payload.promosi_id, payload.perubahan)
    raise HTTPException(422, "Fungsi tidak tersedia")
