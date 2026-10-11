"""Current-turn evidence only; provider booleans override generated eligibility claims."""
import json
import re
from sqlalchemy import select
from . import tools
from ...infrastructure.models import AiToolReceipt


async def snapshot(session, turn):
    receipts = (await session.execute(select(AiToolReceipt).where(
        AiToolReceipt.turn_id == turn.id, AiToolReceipt.is_write.is_(False),
        AiToolReceipt.tool != "delegasi_sonnet",
    ).order_by(AiToolReceipt.created_at, AiToolReceipt.id))).scalars().all()
    # Do not turn specialist prose into verified tool evidence.
    sources = []
    allowance = 6000
    eligibility = None
    for receipt in receipts:
        result = json.loads(receipt.result_json)
        if receipt.tool == "kelayakan_gmv":
            value = result.get("is_eligible") if isinstance(result, dict) and receipt.status == "succeeded" else None
            eligibility = {"is_eligible": value if type(value) is bool else None,
                           "request_id": result.get("request_id") if isinstance(result, dict) else None,
                           "receipt_id": receipt.id, "status": receipt.status,
                           "akun_id": turn.akun_id}
        encoded = tools.encode(result)
        if len(encoded) <= min(1600, allowance):
            sources.append({"tool": receipt.tool, "receipt_id": receipt.id,
                            "status": receipt.status, "hasil": result})
            allowance -= len(encoded)
        else:
            sources.append({"tool": receipt.tool, "receipt_id": receipt.id,
                            "status": receipt.status, "hasil_tidak_disertakan": True})
    return {"akun_id": turn.akun_id, "kelayakan_gmv": eligibility, "sumber": sources}


def reconcile(answer, facts):
    fact = facts.get("kelayakan_gmv")
    if fact is None:
        return answer
    # Keep unrelated analysis; replace generated eligibility sentences, including
    # an immediately following unsupported-claim explanation, with a typed fact.
    chunks = re.split(r"(?<=[.!?])\s+(?=[A-ZÀ-Ý])|\n", answer)
    kept = []
    removed = False
    for chunk in chunks:
        eligibility_claim = re.search(r"gmv\s*max", chunk, re.I) and re.search(r"layak|eligib|diizinkan|didukung|terkonfirmasi", chunk, re.I)
        followup = removed and re.match(r"\s*(?:\*\*)?(?:klaim|pernyataan)\s+(?:ini|tersebut)\b", chunk, re.I)
        if eligibility_claim or followup:
            removed = True
            continue
        kept.append(chunk)
        if chunk.strip():
            removed = False
    value = fact["is_eligible"]
    if value is True:
        statement = "**Kelayakan Shop GMV Max terkonfirmasi:** API Shopee mengembalikan `is_eligible=true` untuk toko terpilih."
    elif value is False:
        statement = "**Toko belum layak menggunakan Shop GMV Max:** API Shopee mengembalikan `is_eligible=false` untuk toko terpilih."
    else:
        statement = "**Kelayakan Shop GMV Max belum dapat dipastikan:** pembacaan tidak memberikan nilai boolean yang terverifikasi."
    statement += " Kelayakan berbeda dari status kampanye aktif dan bukan jaminan keuntungan."
    return "\n".join(kept).strip() + "\n\n" + statement
