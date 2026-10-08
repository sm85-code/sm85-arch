from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator


# --- Auth -----------------------------------------------------------------


PASSWORD_MIN_LENGTH = 8
# bcrypt only looks at the first 72 bytes of the secret (and newer bcrypt
# releases raise instead of silently truncating) -- cap it explicitly.
PASSWORD_MAX_BYTES = 72

# admin (everything, incl. other users' usernames and roles) > owner (everything except that) > staff (scoped to
# the shops it is assigned to).
USER_ROLES = ("admin", "owner", "staff")


def _validate_new_password(value: str) -> str:
    if len(value) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"Password minimal {PASSWORD_MIN_LENGTH} karakter")
    if len(value.encode("utf-8")) > PASSWORD_MAX_BYTES:
        raise ValueError(f"Password maksimal {PASSWORD_MAX_BYTES} byte")
    if not value.strip():
        raise ValueError("Password tidak boleh kosong")
    return value


class RegisterIn(BaseModel):
    nama: str
    email: EmailStr
    password: str

    @field_validator("password")
    @classmethod
    def _password_ok(cls, value: str) -> str:
        return _validate_new_password(value)


class UserCreateIn(BaseModel):
    """Account creation by an admin (any role) or an owner (staff). The username is required, the email optional."""

    nama: str = Field(min_length=1, max_length=255)
    username: str
    email: Optional[EmailStr] = None
    password: str
    role: str = "staff"

    @field_validator("username")
    @classmethod
    def _username_ok(cls, value: str) -> str:
        return normalisasi_username(value)

    @field_validator("password")
    @classmethod
    def _password_ok(cls, value: str) -> str:
        return _validate_new_password(value)

    @field_validator("role")
    @classmethod
    def _role_ok(cls, value: str) -> str:
        role = (value or "").strip().lower()
        if role not in USER_ROLES:
            raise ValueError(f"Role harus salah satu dari: {', '.join(USER_ROLES)}")
        return role


USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")
USERNAME_PESAN = "Username 3-32 karakter: huruf, angka, titik, garis bawah, atau strip; diawali huruf atau angka"


def normalisasi_username(value: str) -> str:
    """Usernames are free text but stored lower-case, so "Budi" and "budi" are the same login."""
    username = (value or "").strip().lower()
    if not USERNAME_RE.match(username):
        raise ValueError(USERNAME_PESAN)
    return username


def periksa_format_email(value: str) -> str:
    """Syntax check only (no network); the deliverability check happens in the service."""
    from email_validator import EmailNotValidError, validate_email

    try:
        return validate_email(value.strip(), check_deliverability=False).normalized.lower()
    except EmailNotValidError as exc:
        raise ValueError(f"Format email tidak valid: {exc}") from exc


class LoginIn(BaseModel):
    """Login with the username or the email. ``email`` is still accepted as the field name for older clients."""

    username: Optional[str] = None
    email: Optional[str] = None
    password: str

    @model_validator(mode="after")
    def _ada_identitas(self):
        if not ((self.username or "").strip() or (self.email or "").strip()):
            raise ValueError("Isi username atau email")
        return self

    @property
    def identitas(self) -> str:
        return ((self.username or "").strip() or (self.email or "").strip()).lower()


class ChangePasswordIn(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _new_password_ok(cls, value: str) -> str:
        return _validate_new_password(value)


class UserUpdateIn(BaseModel):
    """Admin edits an account: username, display name and role. All optional."""

    nama: Optional[str] = Field(None, min_length=1, max_length=255)
    username: Optional[str] = None
    role: Optional[str] = None

    @field_validator("username")
    @classmethod
    def _username_ok(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else normalisasi_username(value)

    @field_validator("role")
    @classmethod
    def _role_ok(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        role = value.strip().lower()
        if role not in USER_ROLES:
            raise ValueError(f"Role harus salah satu dari: {', '.join(USER_ROLES)}")
        return role


class ProfilUpdateIn(BaseModel):
    """What a user may change about themselves: display name and contact email (null/empty removes it). Not the
    username and not the role; sending those is rejected rather than silently ignored. Only the fields sent change."""

    model_config = ConfigDict(extra="forbid")

    nama: Optional[str] = Field(None, min_length=1, max_length=255)
    email: Optional[str] = None

    @field_validator("email")
    @classmethod
    def _email_ok(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not value.strip():
            return None
        return periksa_format_email(value)


class UserOut(BaseModel):
    id: str
    nama: str
    username: Optional[str] = None
    email: Optional[str] = None
    role: str
    # True for the seeded default-password owner (and accounts an owner
    # created with a temporary password) until they call
    # POST /auth/change-password. FE redirects to "Ganti Password" on it.
    must_change_password: bool = False

    model_config = ConfigDict(from_attributes=True)


# --- Akun Marketplace -------------------------------------------------------


class AkunMarketplaceIn(BaseModel):
    platform: str
    nama_toko: str
    id_toko_eksternal: Optional[str] = None
    catatan: Optional[str] = None


class AkunMarketplacePatch(BaseModel):
    nama_toko: Optional[str] = None
    id_toko_eksternal: Optional[str] = None
    status: Optional[str] = None
    catatan: Optional[str] = None
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    token_kedaluwarsa: Optional[datetime] = None


class AkunMarketplaceOut(BaseModel):
    id: str
    platform: str
    nama_toko: str
    id_toko_eksternal: Optional[str]
    status: str
    catatan: Optional[str]

    model_config = ConfigDict(from_attributes=True)


# --- Produk (SKU induk) + Listing -------------------------------------------


def normalisasi_proses(preorder: bool, hari: int) -> int:
    """Ready stock is processed within 2 days (whatever was sent); a pre-order must say 3-14 days."""
    if not preorder:
        return 2
    if not 3 <= hari <= 14:
        raise ValueError("Pre-order harus 3 sampai 14 hari")
    return hari


class ProdukVarianOpsi(BaseModel):
    tier: str = Field(min_length=1, max_length=64)
    opsi: str = Field(min_length=1, max_length=128)

    @field_validator("tier", "opsi")
    @classmethod
    def bersihkan(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Jenis dan pilihan varian tidak boleh kosong")
        return value


class ProdukKeluargaIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nama: str = Field(min_length=1, max_length=255)
    tiers: list[str] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def valid_tiers(self):
        self.nama = self.nama.strip()
        self.tiers = [tier.strip() for tier in self.tiers]
        if not self.nama or any(not tier or len(tier) > 64 for tier in self.tiers):
            raise ValueError("Nama produk induk dan jenis varian wajib diisi")
        if len({tier.casefold() for tier in self.tiers}) != len(self.tiers):
            raise ValueError("Jenis varian tidak boleh berulang")
        return self


class ProdukIn(BaseModel):
    keluarga_id: Optional[str] = None
    opsi_varian: list[ProdukVarianOpsi] = Field(default_factory=list)
    sku_induk: str
    nama: str
    deskripsi: str = ""
    harga_dasar: Decimal
    stok: int = 0
    foto_url: Optional[str] = None
    berat_gram: int = Field(0, ge=0, le=500_000)
    panjang_cm: Decimal = Field(Decimal("0"), ge=0, le=1000)
    lebar_cm: Decimal = Field(Decimal("0"), ge=0, le=1000)
    tinggi_cm: Decimal = Field(Decimal("0"), ge=0, le=1000)
    preorder: bool = False
    hari_proses: int = 2

    @model_validator(mode="after")
    def _proses(self):
        self.hari_proses = normalisasi_proses(self.preorder, self.hari_proses)
        return self


class ProdukPatch(BaseModel):
    keluarga_id: Optional[str] = None
    opsi_varian: list[ProdukVarianOpsi] = Field(default_factory=list)
    nama: Optional[str] = None
    deskripsi: Optional[str] = None
    harga_dasar: Optional[Decimal] = None
    stok: Optional[int] = None
    foto_url: Optional[str] = None
    aktif: Optional[bool] = None
    berat_gram: Optional[int] = Field(None, ge=0, le=500_000)
    panjang_cm: Optional[Decimal] = Field(None, ge=0, le=1000)
    lebar_cm: Optional[Decimal] = Field(None, ge=0, le=1000)
    tinggi_cm: Optional[Decimal] = Field(None, ge=0, le=1000)
    preorder: Optional[bool] = None
    hari_proses: Optional[int] = None


class ProdukOut(BaseModel):
    nama_induk: Optional[str] = None
    keluarga_id: Optional[str] = None
    opsi_varian: list[ProdukVarianOpsi] = Field(default_factory=list)
    id: str
    sku_induk: str
    nama: str
    deskripsi: str
    harga_dasar: Decimal
    stok: int
    foto_url: Optional[str]
    aktif: bool
    berat_gram: int = 0
    panjang_cm: Decimal = Decimal("0")
    lebar_cm: Decimal = Decimal("0")
    tinggi_cm: Decimal = Decimal("0")
    preorder: bool = False
    hari_proses: int = 2

    model_config = ConfigDict(from_attributes=True)


class PublishTokoIn(BaseModel):
    """Options for copying one ERP product into the online store."""

    aktif: bool = True
    # Price in the store; defaults to the ERP base price.
    harga: Optional[Decimal] = Field(None, ge=0)
    # Starting stock in the store (first publish only -- the store owns its
    # stock afterwards); defaults to the current ERP stock.
    stok: Optional[int] = Field(None, ge=0)
    salin_foto: bool = True


class ProdukListingIn(BaseModel):
    produk_id: str
    akun_id: str
    platform: str
    id_eksternal: str
    harga_jual: Optional[Decimal] = None
    stok_listing: Optional[int] = None


class ProdukListingPatch(BaseModel):
    harga_jual: Optional[Decimal] = None
    stok_listing: Optional[int] = None
    aktif: Optional[bool] = None


class ListingMarketplaceDetail(BaseModel):
    item_id: str
    model_id: Optional[str] = None
    nama_produk: str
    sku: str
    opsi: list[dict[str, str]] = Field(default_factory=list)
    harga: Optional[Decimal] = None
    harga_asli: Optional[Decimal] = None
    berat_gram: Optional[int] = None
    panjang_cm: Optional[Decimal] = None
    lebar_cm: Optional[Decimal] = None
    tinggi_cm: Optional[Decimal] = None
    preorder: Optional[bool] = None
    hari_kirim: Optional[int] = None
    ikut_produk: list[str] = Field(default_factory=list)
    diambil_at: datetime


class ProdukListingOut(BaseModel):
    id: str
    produk_id: str
    akun_id: str
    platform: str
    id_eksternal: str
    harga_jual: Optional[Decimal]
    stok_listing: Optional[int]
    aktif: bool

    detail_marketplace: Optional[ListingMarketplaceDetail] = None

    model_config = ConfigDict(from_attributes=True)


# --- Tahap 2: Stock ---------------------------------------------------------


class StokAdjustIn(BaseModel):
    produk_id: str
    qty_delta: int
    expected_stock: Optional[int] = None
    catatan: Optional[str] = None
    gudang_id: Optional[str] = None


class StokLedgerOut(BaseModel):
    id: str
    produk_id: str
    gudang_id: Optional[str]
    qty_delta: int
    reason: str
    ref_type: Optional[str]
    ref_id: Optional[str]
    catatan: Optional[str]
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class GudangOut(BaseModel):
    id: str
    kode: str
    nama: str
    aktif: bool

    model_config = ConfigDict(from_attributes=True)


class StokReservasiOut(BaseModel):
    id: str
    produk_id: str
    gudang_id: Optional[str]
    pesanan_id: str
    qty: int
    status: str

    model_config = ConfigDict(from_attributes=True)


# --- Tahap 2: Orders OMS ----------------------------------------------------


class ItemPesananIn(BaseModel):
    nama_produk: str
    harga_satuan: Decimal
    qty: int = Field(gt=0)
    produk_id: Optional[str] = None
    listing_id: Optional[str] = None
    subtotal: Optional[Decimal] = None


class PesananIn(BaseModel):
    platform: str
    id_eksternal: str
    akun_id: Optional[str] = None
    status: str = "unpaid"
    nama_pembeli: str = ""
    total: Optional[Decimal] = None
    items: list[ItemPesananIn] = Field(default_factory=list)


class PesananStatusIn(BaseModel):
    status: str


class BatalkanPesananIn(BaseModel):
    alasan: str = "CUSTOMER_REQUEST"


class PembatalanPembeliIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operasi: Literal["ACCEPT", "REJECT"]


class TandaiResiIn(BaseModel):
    dicetak: bool = True


class ResiMassalIn(BaseModel):
    pesanan_ids: list[str] = Field(min_length=1, max_length=50)
    tipe: Optional[str] = Field(default=None, pattern="^(THERMAL_AIR_WAYBILL|NORMAL_AIR_WAYBILL)$")


class ResiGabunganIn(BaseModel):
    """Labels for any mix of shops and couriers: the server splits them the way Shopee needs and joins the PDFs."""

    pesanan_ids: list[str] = Field(min_length=1, max_length=200)
    tipe: Optional[str] = Field(default=None, pattern="^(THERMAL_AIR_WAYBILL|NORMAL_AIR_WAYBILL)$")


class PengaturanPengirimanIn(BaseModel):
    metode: Literal["dropoff", "pickup"]
    address_id: Optional[int] = Field(default=None, gt=0)
    pickup_time_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    branch_id: Optional[int] = Field(default=None, gt=0)
    sender_real_name: Optional[str] = Field(default=None, min_length=1, max_length=255)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_method_fields(self):
        if self.metode == "dropoff" and (self.address_id is not None or self.pickup_time_id is not None):
            raise ValueError("Drop Off tidak menggunakan alamat/jadwal pickup")
        if self.metode == "pickup" and (self.branch_id is not None or self.sender_real_name is not None):
            raise ValueError("Pickup tidak menggunakan cabang/nama pengirim drop-off")
        return self


class ProsesMassalIn(BaseModel):
    # Each order costs several marketplace calls, so one request is capped; the UI sends chunks.
    pesanan_ids: list[str] = Field(min_length=1, max_length=25)
    pengaturan: dict[str, PengaturanPengirimanIn] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_settings(self):
        if self.pengaturan and set(self.pengaturan) != set(self.pesanan_ids):
            raise ValueError("Pengaturan harus mencakup tepat semua pesanan yang diproses")
        return self


class ItemPesananOut(BaseModel):
    id: str
    produk_id: Optional[str]
    listing_id: Optional[str]
    nama_produk: str
    model_name: str = ""
    item_sku: str = ""
    model_sku: str = ""
    foto: Optional[str] = None
    item_id_eksternal: Optional[str] = None
    harga_satuan: Decimal
    qty: int
    subtotal: Decimal

    model_config = ConfigDict(from_attributes=True)


class PesananOut(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def snapshot_fields(cls, value):
        """Use the same typed snapshot in list, detail and action responses."""
        import json

        if isinstance(value, dict):
            return value
        data = {name: getattr(value, name) for name in cls.model_fields if hasattr(value, name)}
        try:
            snapshot = json.loads(getattr(value, "detail_json", None) or "{}")
        except (ValueError, TypeError):
            snapshot = {}
        fields = {
            "payment_method", "currency", "cod", "days_to_ship", "ship_by_date", "pay_time",
            "estimated_shipping_fee", "actual_shipping_fee", "actual_shipping_fee_confirmed",
            "message_to_seller", "note", "cancel_by", "cancel_reason", "penerima", "kota",
        }
        if isinstance(snapshot, dict):
            data.update({k: v for k, v in snapshot.items() if k in fields and v is not None})
        return data

    id: str
    platform: str
    id_eksternal: str
    akun_id: Optional[str]
    status: str
    nama_pembeli: str
    total: Decimal
    tersinkron_marketplace: bool
    catatan_sinkron: Optional[str]
    status_marketplace: Optional[str] = None
    metode_pengiriman: Optional[str] = None
    resi_dicetak_at: Optional[datetime] = None
    resi_dicetak_oleh: Optional[str] = None
    kurir: Optional[str] = None
    nomor_resi: Optional[str] = None
    tanggal_kirim: Optional[datetime] = None
    dipesan_at: Optional[datetime] = None
    items: list[ItemPesananOut] = Field(default_factory=list)
    payment_method: str = ""
    currency: str = ""
    cod: bool = False
    days_to_ship: Optional[int] = None
    estimated_shipping_fee: Optional[Decimal] = None
    actual_shipping_fee: Optional[Decimal] = None
    actual_shipping_fee_confirmed: Optional[bool] = None
    ship_by_date: Optional[int] = None
    pay_time: Optional[int] = None
    message_to_seller: str = ""
    note: str = ""
    cancel_by: str = ""
    cancel_reason: str = ""
    penerima: str = ""
    kota: str = ""
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


# --- OAuth ------------------------------------------------------------------


class OAuthStartOut(BaseModel):
    platform: str
    akun_id: str
    authorize_url: str


# --- Tahap 3: multi-gudang, staff scoping, pengiriman, settlement, laporan --


class GudangIn(BaseModel):
    kode: str = Field(min_length=1, max_length=64)
    nama: str = Field(min_length=1, max_length=255)


class StokTransferIn(BaseModel):
    produk_id: str
    dari_gudang_id: str
    ke_gudang_id: str
    qty: int = Field(gt=0)
    catatan: Optional[str] = None


class StaffAkunIn(BaseModel):
    user_id: str
    akun_id: str


class StaffAkunOut(BaseModel):
    id: str
    user_id: str
    akun_id: str

    model_config = ConfigDict(from_attributes=True)


class PengirimanIn(BaseModel):
    kurir: str = Field(min_length=1, max_length=64)
    nomor_resi: str = Field(min_length=1, max_length=128)
    tanggal_kirim: Optional[datetime] = None


class SettlementIn(BaseModel):
    akun_id: str
    periode_mulai: datetime
    periode_selesai: datetime
    gross_sales: Decimal = Decimal("0")
    fee_platform: Decimal = Decimal("0")
    fee_payment: Decimal = Decimal("0")
    ongkir_subsidi: Decimal = Decimal("0")
    penalti: Decimal = Decimal("0")
    net: Decimal = Decimal("0")
    catatan: Optional[str] = None


class SettlementPatch(BaseModel):
    gross_sales: Optional[Decimal] = None
    fee_platform: Optional[Decimal] = None
    fee_payment: Optional[Decimal] = None
    ongkir_subsidi: Optional[Decimal] = None
    penalti: Optional[Decimal] = None
    net: Optional[Decimal] = None
    status: Optional[str] = None
    catatan: Optional[str] = None


class SettlementOut(BaseModel):
    id: str
    akun_id: str
    platform: str
    periode_mulai: datetime
    periode_selesai: datetime
    gross_sales: Decimal
    fee_platform: Decimal
    fee_payment: Decimal
    ongkir_subsidi: Decimal
    penalti: Decimal
    net: Decimal
    status: str
    catatan: Optional[str]

    model_config = ConfigDict(from_attributes=True)


class ProdukTerlarisOut(BaseModel):
    produk_id: Optional[str]
    nama_produk: str
    qty_terjual: int
    omzet: Decimal


class StokKritisOut(BaseModel):
    produk_id: str
    sku_induk: str
    nama: str
    stok: int


class LaporanRingkasOut(BaseModel):
    dari: datetime
    sampai: datetime
    total_omzet: Decimal
    jumlah_pesanan_per_status: dict[str, int]
    produk_terlaris: list[ProdukTerlarisOut]
    stok_kritis: list[StokKritisOut]


# --- Tahap 4: Iklan (ads) ----------------------------------------------------


class IklanCampaignIn(BaseModel):
    akun_id: str
    produk_id: Optional[str] = None
    nama: str = Field(min_length=1, max_length=255)
    budget_harian: Decimal = Decimal("0")
    tanggal_mulai: datetime
    tanggal_selesai: Optional[datetime] = None
    catatan: Optional[str] = None


class IklanCampaignPatch(BaseModel):
    nama: Optional[str] = None
    status: Optional[str] = None
    budget_harian: Optional[Decimal] = None
    tanggal_selesai: Optional[datetime] = None
    catatan: Optional[str] = None


class IklanCampaignOut(BaseModel):
    id: str
    akun_id: str
    platform: str
    produk_id: Optional[str]
    nama: str
    status: str
    budget_harian: Decimal
    tanggal_mulai: datetime
    tanggal_selesai: Optional[datetime]
    catatan: Optional[str]

    model_config = ConfigDict(from_attributes=True)


class IklanMetrikHarianIn(BaseModel):
    tanggal: datetime
    impression: int = Field(ge=0, default=0)
    klik: int = Field(ge=0, default=0)
    biaya: Decimal = Decimal("0")


class IklanMetrikHarianOut(BaseModel):
    id: str
    campaign_id: str
    tanggal: datetime
    impression: int
    klik: int
    biaya: Decimal

    model_config = ConfigDict(from_attributes=True)


class IklanLaporanOut(BaseModel):
    campaign_id: str
    dari: datetime
    sampai: datetime
    total_impression: int
    total_klik: int
    ctr: Decimal
    total_biaya: Decimal
    omzet_atribusi: Decimal
    roas: Optional[Decimal]


class KatalogKirimIn(BaseModel):
    """Send chosen Shopee catalogue items to the online store (as drafts unless ``aktif``)."""

    ids: list[str] = Field(..., min_length=1, max_length=20)
    # Drafts by default: the store owner reviews and activates each product in the admin dashboard.
    aktif: bool = False
    # An item already sent is skipped (the store copy may have been edited); true refreshes it from Shopee.
    timpa: bool = False


class ShopeeProdukEditIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nama: Optional[str] = Field(default=None, min_length=1, max_length=255)
    sku: Optional[str] = Field(default=None, max_length=128)
    deskripsi: Optional[str] = Field(default=None, min_length=1, max_length=3000)

    @model_validator(mode="after")
    def perubahan_valid(self):
        if self.nama is None and self.sku is None and self.deskripsi is None:
            raise ValueError("Isi nama, SKU, atau deskripsi yang akan diperbarui")
        if self.deskripsi is not None and not self.deskripsi.strip():
            raise ValueError("Deskripsi tidak boleh kosong")
        if self.nama is not None:
            self.nama = self.nama.strip()
            if not self.nama:
                raise ValueError("Nama tidak boleh kosong")
        return self


class ShopeeProdukStatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    unlist: bool


class ProdukKeluargaNamaIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nama: str = Field(min_length=1, max_length=255)

    @field_validator("nama")
    @classmethod
    def nama_valid(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Nama produk induk tidak boleh kosong")
        return value


# Read-only returns remain independent from order, inventory and settlement statuses.
class ReturItemOut(BaseModel):
    item_id: str | None = None
    model_id: str | None = None
    nama: str
    sku: str | None = None
    sku_varian: str | None = None
    qty: int | None = None
    harga: Decimal | None = None
    nominal_refund: Decimal | None = None


class ReturOut(BaseModel):
    akun_id: str
    platform: str
    nama_toko: str
    pesanan_id: str | None = None
    nomor_retur: str
    nomor_pesanan: str
    status: str
    alasan: str | None = None
    alasan_pembeli: str | None = None
    alasan_peninjauan: str | None = None
    nominal_refund: Decimal | None = None
    mata_uang: str | None = None
    perlu_pengembalian_barang: bool | None = None
    solusi: int | None = None
    dibuat_at: int | None = None
    diperbarui_at: int | None = None
    tenggat_at: int | None = None
    tenggat_kirim_at: int | None = None
    tenggat_penjual_at: int | None = None
    nomor_resi: str | None = None
    kurir: str | None = None
    status_negosiasi: str | None = None
    status_bukti: str | None = None
    status_kompensasi: str | None = None
    items: list[ReturItemOut] = Field(default_factory=list)


class ReturDaftarOut(BaseModel):
    items: list[ReturOut]
    halaman: int
    per_halaman: int
    ada_lagi: bool


class PromosiIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nama: str = Field(min_length=1, max_length=255)
    mulai_at: int = Field(gt=0)
    selesai_at: int = Field(gt=0)

    @field_validator("nama")
    @classmethod
    def nama_tidak_kosong(cls, value):
        if not value.strip():
            raise ValueError("Nama promosi tidak boleh kosong")
        return value.strip()


class PromosiProdukIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operasi: Literal["tambah", "ubah", "hapus"]
    katalog_id: str
    model_id: str | None = Field(default=None, pattern=r"^[0-9]+$")
    harga: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    batas_pembelian: int = Field(default=0, ge=0)
    stok_promo: int | None = Field(default=None, gt=0, le=2147483647, strict=True)

    @model_validator(mode="after")
    def harga_wajib(self):
        if self.stok_promo is not None and self.operasi != "tambah":
            raise ValueError("Stok promosi hanya dapat diatur saat menambahkan produk/varian, bukan diubah langsung.")
        if self.operasi != "hapus" and self.harga is None:
            raise ValueError("Harga promosi wajib diisi")
        return self


class PromosiBarangOut(BaseModel):
    item_id: str
    model_id: str | None = None
    nama: str
    nama_varian: str | None = None
    harga_asli: str | None = None
    harga_promo: str | None = None
    stok_promo: int | None = None
    batas_pembelian: int | None = None


class PromosiOut(BaseModel):
    id: str
    nama: str
    status: str
    mulai_at: int
    selesai_at: int
    barang: list[PromosiBarangOut] = Field(default_factory=list)


class PromosiHalamanOut(BaseModel):
    items: list[PromosiOut]
    halaman: int
    ada_lagi: bool


class PromosiDetailOut(PromosiOut):
    halaman: int
    ada_lagi: bool


class MutasiMarketplaceOut(BaseModel):
    ok: bool
    id: str | None = None
    request_id: str | None = None
    warnings: list[str] = Field(default_factory=list)
    gagal: list[dict] = Field(default_factory=list)
    retur: ReturOut | None = None


class StaffAkunBanyakIn(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    akun_ids: list[str] = Field(min_length=1, max_length=200)
