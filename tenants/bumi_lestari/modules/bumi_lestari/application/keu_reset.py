"""Owner-confirmed, atomic reset of the keu ledger only; masters and audit survive."""
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, text, tuple_
from sqlalchemy.exc import DBAPIError
from sqlalchemy.sql.ddl import sort_tables

from shared.security import verify_password
from . import keu_services as svc
from .audit_core import catat_audit
from ..infrastructure import models_keu as m, models_keu_finance as f
from ..infrastructure.models import BlAuditLog

RESET_MODELS = (m.KeuPesanan, m.KeuItem, m.KeuAlokasiVendor, m.KeuSettlement,
                m.KeuAlokasiSettlement, m.KeuTransaksi, m.KeuImpor, m.KeuMasukan, m.KeuCursor,
                f.KeuBuku, f.KeuJurnal, f.KeuJurnalBaris, f.KeuStokMutasi, f.KeuStokPemakaian)


def owner(user):
    if user.role != "owner" or not user.aktif or user.must_change_password:
        svc.bad("Reset keuangan hanya dapat dilakukan owner setelah mengganti password bawaan", 403)


async def lock_tables(session, *, reset=False):
    if session.bind.dialect.name == "postgresql":
        await session.execute(text("SET LOCAL lock_timeout = '5s'"))
        await session.execute(text("SET LOCAL statement_timeout = '120s'"))
        names = ", ".join('"' + model.__tablename__ + '"' for model in RESET_MODELS)
        await session.execute(text(f"LOCK TABLE {names} IN {'ACCESS EXCLUSIVE' if reset else 'SHARE'} MODE"))


async def inventory(session):
    counts, digest = {}, hashlib.sha256()
    for model in RESET_MODELS:
        digest.update(model.__tablename__.encode())
        count = 0
        keys = list(model.__table__.primary_key.columns)
        last = None
        while True:
            statement = select(model.__table__).order_by(*keys).limit(500)
            if last is not None:
                condition = keys[0] > last[0] if len(keys) == 1 else tuple_(*keys) > tuple_(*last)
                statement = statement.where(condition)
            rows = (await session.execute(statement)).mappings().all()
            if not rows:
                break
            for row in rows:
                digest.update(json.dumps(svc.json_value(dict(row)), sort_keys=True, separators=(",", ":")).encode())
                digest.update(b"\n")
                count += 1
            last = tuple(rows[-1][key.name] for key in keys)
        counts[model.__tablename__] = count
    return counts, digest.hexdigest()


async def preview(session, user):
    owner(user)
    from . import keu_ledger
    await keu_ledger.lock(session)
    try:
        await lock_tables(session)
        counts, fingerprint = await inventory(session)
    except DBAPIError:
        svc.bad("Data sedang diproses. Coba pratinjau reset kembali setelah proses selesai.", 409)
    token = secrets.token_urlsafe(48)
    expires = datetime.now(timezone.utc) + timedelta(minutes=5)
    challenge = BlAuditLog(user_id=user.id, aksi="reset-keu-preview", entitas="keu_reset", sesudah={
        "scope": "keu", "counts": counts, "fingerprint": fingerprint,
        "token_hash": hashlib.sha256(token.encode()).hexdigest(), "expires_at": expires.isoformat(), "consumed": False})
    session.add(challenge)
    await session.flush()
    return {"challenge_id": challenge.id, "token": token, "expires_at": expires.isoformat(), "counts": counts}


async def execute(session, user, payload):
    owner(user)
    from . import keu_ledger
    await keu_ledger.lock(session)
    if not verify_password(payload.password.get_secret_value(), user.password_hash):
        svc.bad("Password owner tidak sesuai", 403)
    challenge = (await session.execute(select(BlAuditLog).where(BlAuditLog.id == payload.challenge_id).with_for_update())).scalar_one_or_none()
    state = challenge.sesudah if challenge else None
    if (not challenge or challenge.user_id != user.id or challenge.aksi != "reset-keu-preview" or not isinstance(state, dict)
            or state.get("consumed") or state.get("scope") != "keu"
            or not hmac.compare_digest(state.get("token_hash", ""), hashlib.sha256(payload.token.encode()).hexdigest())
            or datetime.fromisoformat(state["expires_at"]) <= datetime.now(timezone.utc)):
        svc.bad("Konfirmasi reset tidak valid atau kedaluwarsa. Buat pratinjau baru.", 409)
    try:
        await lock_tables(session, reset=True)
        counts, fingerprint = await inventory(session)
        if fingerprint != state["fingerprint"]:
            svc.bad("Data berubah sejak pratinjau. Periksa jumlah data dan konfirmasi ulang.", 409)
        if session.bind.dialect.name == "postgresql":
            # No CASCADE, no master tables, no trigger disabling. TRUNCATE is transactional.
            names = ", ".join('"' + model.__tablename__ + '"' for model in RESET_MODELS)
            await session.execute(text(f"TRUNCATE TABLE {names}"))
        else:
            for table in reversed(sort_tables([model.__table__ for model in RESET_MODELS])):
                await session.execute(delete(table))
        challenge.sesudah = {**state, "consumed": True}
        await catat_audit(session, user.id, "reset-keu", "keu_reset", challenge.id,
                          sebelum={"counts": counts}, sesudah={"counts": {key: 0 for key in counts}}, alasan="RESET-KEUANGAN")
        await session.flush()
    except DBAPIError:
        svc.bad("Reset tidak dijalankan: data sedang diproses atau memiliki keterkaitan di luar lingkup keu. Coba kembali setelah proses selesai.", 409)
    return {"direset": True, "counts": counts}
