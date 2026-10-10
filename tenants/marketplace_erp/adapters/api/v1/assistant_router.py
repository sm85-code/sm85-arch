"""Admin-only async assistant API. Request bodies never contain model/API secrets."""
import json
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tenants.marketplace_erp.modules.marketplace_erp.application.assistant import config, service, tools
from tenants.marketplace_erp.modules.marketplace_erp.application.assistant.schemas import TurnIn, ResolveIn
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import require_roles_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import AiConversation, AiTurn, AiToolReceipt, AiDailyBudget, UserMarketplaceErp

router = APIRouter(prefix="/asisten")
DB = Depends(get_db_marketplace_erp)
ADMIN = Depends(require_roles_marketplace_erp("admin"))


async def turn_out(session, turn):
    receipts = (await session.execute(select(AiToolReceipt).where(AiToolReceipt.turn_id == turn.id).order_by(AiToolReceipt.created_at, AiToolReceipt.id))).scalars().all()
    return {"id": turn.id, "conversation_id": turn.conversation_id, "akun_id": turn.akun_id, "mode": turn.mode,
            "prompt": turn.prompt, "context": json.loads(turn.context_json or "{}"), "status": turn.status, "answer": turn.answer, "model": turn.model,
            "input_tokens": turn.input_tokens, "output_tokens": turn.output_tokens, "cost_usd": str(turn.cost_usd),
            "created_at": turn.created_at, "finished_at": turn.finished_at,
            "actions": [{"id": r.id, "tool": r.tool, "label": tools.TOOLS.get(r.tool, (None, None, r.tool))[2],
                         "is_write": r.is_write, "status": r.status, "arguments": json.loads(r.arguments_json), "result": json.loads(r.result_json)} for r in receipts]}


@router.get("/status")
async def status(session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN):
    budget = await session.get(AiDailyBudget, config.day())
    unresolved = (await session.execute(select(AiToolReceipt, AiTurn.akun_id).join(AiTurn, AiTurn.id == AiToolReceipt.turn_id).where(AiToolReceipt.status == "unknown", AiToolReceipt.is_write.is_(True)).order_by(AiToolReceipt.created_at).limit(20))).all()
    return {"unresolved": [{"id": r.id, "tool": r.tool, "akun_id": akun_id} for r, akun_id in unresolved], "available": config.ready() and config.valid_limits(), "model": config.MODEL,
            "usd_idr": str(config.USD_IDR), "daily_usd": str(config.DAILY_USD), "turn_usd": str(config.TURN_USD), "daily_turns": config.DAILY_TURNS,
            "spent_usd": str(budget.spent_usd if budget else 0), "reserved_usd": str(budget.reserved_usd if budget else 0),
            "used_turns": budget.turns if budget else 0, "max_writes": config.MAX_WRITES,
            "capabilities": [{"name": n, "write": w, "description": d} for n, (_s, w, d) in tools.TOOLS.items()]}


@router.get("/percakapan")
async def conversations(session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN, halaman: int = Query(1, ge=1)):
    rows = (await session.execute(select(AiConversation).where(AiConversation.user_id == user.id).order_by(AiConversation.created_at.desc()).offset((halaman-1)*20).limit(21))).scalars().all()
    return {"items": [{"id": r.id, "title": r.title, "created_at": r.created_at} for r in rows[:20]], "ada_lagi": len(rows) > 20}


@router.get("/percakapan/{id}")
async def history(id: str, session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN, halaman: int = Query(1, ge=1)):
    await service.owned_conversation(session, user, id)
    rows = (await session.execute(select(AiTurn).where(AiTurn.conversation_id == id).order_by(AiTurn.created_at.desc()).offset((halaman-1)*20).limit(21))).scalars().all()
    return {"items": [await turn_out(session, r) for r in reversed(rows[:20])], "ada_lagi": len(rows) > 20}


@router.post("/pesan", status_code=202)
async def send(payload: TurnIn, session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN):
    return await turn_out(session, await service.enqueue(session, user, payload))


@router.post("/tindakan/{id}/selesai-periksa")
async def resolve(id: str, body: ResolveIn, session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN):
    receipt = await session.get(AiToolReceipt, id)
    if not receipt:
        raise HTTPException(404, "Catatan tindakan tidak ditemukan")
    # Every admin may resolve business-shop uncertainty, even if the original admin lost access.
    lease = await service.lock_row(session, service.AiWorkerLease, "global", id="global")
    if lease.turn_id:
        raise HTTPException(409, "Tunggu proses AI selesai sebelum menyelesaikan pemeriksaan")
    if receipt.status != "unknown":
        raise HTTPException(409, "Tindakan tidak memerlukan pemeriksaan")
    result = json.loads(receipt.result_json)
    receipt.result_json = tools.encode({"hasil_sebelumnya": result, "pemeriksaan_admin": {"oleh": user.id, "waktu": service.now(), "catatan": body.note.strip()}})
    receipt.status = "reviewed"
    await session.commit()
    return {"ok": True}
