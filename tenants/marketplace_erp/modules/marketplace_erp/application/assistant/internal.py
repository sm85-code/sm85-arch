"""Deterministic, bounded commands. Unrecognised/compound text goes to Sonnet.

Only exact complete requests are routed here; no partial keyword match can turn
an analysis, negation, or different period into an operational instruction.
"""
import re
from datetime import timedelta
from decimal import Decimal
from . import config

MODEL = "internal-v1"
HELP = """**Asisten ERP: internal + Sonnet**
Permintaan rutin berikut memakai kode internal, tanpa token model:
- daftar toko
- cari produk rak bambu
- daftar pesanan / pesanan perlu diproses / cari pesanan NOMOR
- ringkasan penjualan hari ini / kemarin / bulan ini / 7 hari terakhir
- stok kritis / produk terlaris bulan ini
- saldo toko / pendapatan toko / daftar iklan / daftar promosi / kesehatan toko
- mutasi dana 7 hari terakhir
- kotak masuk chat / chat belum dibaca
- jeda iklan 123 / lanjutkan iklan 123 / ubah budget iklan 123 menjadi 50 ribu
- sinkron katalog
- catat perbaikan fitur: jelaskan masalah dan hasil yang diinginkan
Pilih toko untuk data live dan perubahan. Semua perubahan menjadi draf yang perlu dikonfirmasi.
Untuk analisis, nama produk ambigu, atau bahasa bebas, Sonnet membantu memakai tools resmi.
Coding saat ini membantu menyusun kebutuhan dan rencana; belum mengeksekusi kode, migrasi, PR atau deploy.
GPT manajer dan otomasi terjadwal belum diaktifkan. Ada hal lain yang ingin Anda periksa?"""


def period(value):
    today = config.day()
    value = (value or "bulan ini").strip()
    if value == "hari ini":
        start = today
    elif value == "kemarin":
        today -= timedelta(days=1)
        start = today
    elif value in ("bulan ini", "bulan berjalan"):
        start = today.replace(day=1)
    else:
        match = re.fullmatch(r"(\d{1,2}) hari terakhir", value)
        if not match or not 1 <= int(match[1]) <= 93:
            return None
        start = today - timedelta(days=int(match[1]) - 1)
    return {"mulai": start.isoformat(), "selesai": today.isoformat()}


def plan(prompt, agent="auto"):
    raw = re.sub(r"\s+", " ", prompt.strip())
    text = raw.lower().rstrip("?.!")
    text = re.sub(r"^(?:tolong |lihat |cek |tampilkan )", "", text)
    if text in ("bantuan", "help", "kemampuan asisten"):
        return {"answer": HELP}
    if agent == "coding" or text.startswith("catat perbaikan fitur:"):
        if text.startswith("catat perbaikan fitur:"):
            return {"answer": "**Permintaan perbaikan dicatat dalam percakapan ini.**\n" + raw.split(":", 1)[1].strip() + "\n\nBelum ada perubahan kode. Jelaskan langkah terjadinya masalah, hasil sekarang, dan hasil yang diinginkan. Pilih Coding untuk meminta Sonnet menyusun rencana dan kriteria penerimaan."}
        return None
    simple = {"daftar toko": "daftar_toko", "saldo toko": "saldo_toko", "saldo shopee": "saldo_toko", "pendapatan toko": "ringkasan_pendapatan", "kesehatan toko": "performa_toko", "daftar promosi": "daftar_promosi", "daftar iklan": "daftar_iklan", "kelayakan gmv": "kelayakan_gmv", "sinkron katalog": "sinkron_katalog"}
    if text in simple:
        return {"name": simple[text], "input": {}}
    if text in ("kotak masuk chat", "chat belum dibaca"):
        return {"name": "kotak_masuk_chat", "input": {"unread": text == "chat belum dibaca"}}
    match = re.fullmatch(r"(?:ringkasan penjualan|ringkasan usaha|stok kritis|produk terlaris)(?: (hari ini|kemarin|bulan ini|bulan berjalan|\d{1,2} hari terakhir))?", text)
    if match:
        window = period(match[1])
        if window:
            name = "stok_kritis" if text.startswith("stok kritis") else "produk_terlaris" if text.startswith("produk terlaris") else "ringkasan_usaha"
            return {"name": name, "input": window}
    if text == "daftar pesanan":
        return {"name": "daftar_pesanan", "input": {}}
    stages = {"belum bayar": "belum_bayar", "perlu diproses": "perlu_diproses", "menunggu kurir": "menunggu_kurir", "dikirim": "dikirim", "selesai": "selesai", "dibatalkan": "dibatalkan"}
    if text.startswith("pesanan ") and text[8:] in stages:
        return {"name": "daftar_pesanan", "input": {"tahap": stages[text[8:]]}}
    match = re.fullmatch(r"cari (produk|pesanan) (.{1,100})", text)
    if match:
        # Search is read-only, bounded, and treats the entire trailing text as data.
        return {"name": "cari_produk" if match[1] == "produk" else "daftar_pesanan", "input": {"q": match[2]}}
    match = re.fullmatch(r"(daftar iklan|mutasi dana) (\d{1,2}) hari terakhir", text)
    if match:
        days = int(match[2])
        if match[1] == "daftar iklan" and 2 <= days <= 28:
            return {"name": "daftar_iklan", "input": {"hari": days}}
        if match[1] == "mutasi dana" and 1 <= days <= 15:
            return {"name": "mutasi_dana", "input": period(f"{days} hari terakhir")}
    match = re.fullmatch(r"(jeda|lanjutkan) iklan ([1-9]\d{0,18})", text)
    if match:
        return {"name": "ubah_iklan", "input": {"campaign_id": int(match[2]), "aksi": "pause" if match[1] == "jeda" else "resume"}}
    match = re.fullmatch(r"ubah budget iklan ([1-9]\d{0,18}) menjadi (\d+(?:[.,]\d{1,2})?)\s*(ribu|rb|juta|jt)?", text)
    if match:
        amount = Decimal(match[2].replace(",", ".")) * (1000 if match[3] in ("ribu", "rb") else 1000000 if match[3] in ("juta", "jt") else 1)
        if 0 < amount <= 100000000:
            return {"name": "ubah_iklan", "input": {"campaign_id": int(match[1]), "aksi": "change_budget", "budget": str(amount)}}
    return None


def compact(result):
    """Limit only model context, never the durable receipt or its outcome."""
    from .tools import encode
    if len(encode(result).encode()) <= 24000:
        return result
    metadata = {k: result[k] for k in ("ok", "error", "status", "request_id", "warnings") if isinstance(result, dict) and k in result}
    return {**metadata, "terpotong": True, "pesan": "Hasil besar disimpan dalam catatan; gunakan pencarian/periode lebih spesifik."}


def answer(name, result, stop=False):
    if isinstance(result, dict) and result.get("draft_id"):
        return "Draf perubahan sudah siap. Periksa toko, sasaran, dan nilainya di bawah. **Belum ada data yang diubah.** Pilih Konfirmasi untuk menjalankan atau Batal."
    if stop or (isinstance(result, dict) and (result.get("error") or result.get("ok") is False)):
        detail = str(result.get("error") or "")[:500] if isinstance(result, dict) else ""
        return (detail + "\n\n" if detail else "") + "Hasil belum berhasil atau belum pasti. Periksa catatan tindakan di bawah; jangan mengulang perubahan yang belum pasti."
    return f"Berikut hasil **{name.replace('_', ' ')}** untuk cakupan toko yang Anda pilih. Rincian tersedia di bawah, tanpa memakai token AI. Ada hal lain yang bisa saya bantu?"
