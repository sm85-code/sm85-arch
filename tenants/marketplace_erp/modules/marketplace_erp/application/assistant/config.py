import os
from decimal import Decimal, InvalidOperation
from datetime import datetime
from zoneinfo import ZoneInfo

def decimal_setting(name, default):
    try:
        return Decimal(os.getenv(name, default))
    except InvalidOperation:
        return Decimal("NaN")


def integer_setting(name, default):
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return 0


MODEL = os.getenv("ERP_AI_MODEL", "claude-sonnet-5-5")
# Estimation only; Anthropic sets the actual invoice prices. No cache/batch enabled.
RATES = {"claude-sonnet-5-5": ("2", "10"), "claude-opus-5-5": ("4", "20"), "claude-sonnet-4-6": ("3", "15")}
INPUT_RATE = decimal_setting("ERP_AI_INPUT_USD_PER_MILLION", RATES.get(MODEL, ("4", "20"))[0])
OUTPUT_RATE = decimal_setting("ERP_AI_OUTPUT_USD_PER_MILLION", RATES.get(MODEL, ("4", "20"))[1])
DAILY_USD = decimal_setting("ERP_AI_DAILY_USD", "10")
TURN_USD = decimal_setting("ERP_AI_TURN_USD", "1.5")
DAILY_TURNS = integer_setting("ERP_AI_DAILY_TURNS", "30")
MAX_CALLS = 6
MAX_WRITES = 5
MAX_TOOLS = 16
MAX_INPUT_BYTES = 60000
MAX_OUTPUT = min(8000, max(1000, integer_setting("ERP_AI_MAX_OUTPUT_TOKENS", "4000")))
USD_IDR = decimal_setting("KURS_USD_IDR", "16000")
if not USD_IDR.is_finite() or USD_IDR <= 0:
    USD_IDR = Decimal("16000")


def day():
    return datetime.now(ZoneInfo("Asia/Jakarta")).date()


def cost(tokens_in, tokens_out):
    return (Decimal(tokens_in) * INPUT_RATE + Decimal(tokens_out) * OUTPUT_RATE) / 1000000


def ready():
    return os.getenv("ERP_AI_ENABLED", "true").lower() == "true" and bool(os.getenv("ANTHROPIC_API_KEY", "").strip())


def valid_limits():
    return (MODEL in RATES or bool(os.getenv("ERP_AI_INPUT_USD_PER_MILLION") and os.getenv("ERP_AI_OUTPUT_USD_PER_MILLION"))) and all(v.is_finite() and v > 0 for v in (INPUT_RATE, OUTPUT_RATE, DAILY_USD, TURN_USD)) and DAILY_TURNS > 0
