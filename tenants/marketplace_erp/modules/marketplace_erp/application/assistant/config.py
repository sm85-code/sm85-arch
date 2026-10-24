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
RATES = {"claude-haiku-5-5": ("0.1", "0.5"), "claude-sonnet-5-5": ("2", "10"), "claude-opus-5-5": ("4", "20"), "claude-sonnet-4-6": ("3", "15")}
INPUT_RATE = decimal_setting("ERP_AI_INPUT_USD_PER_MILLION", RATES.get(MODEL, ("4", "20"))[0])
OUTPUT_RATE = decimal_setting("ERP_AI_OUTPUT_USD_PER_MILLION", RATES.get(MODEL, ("4", "20"))[1])
DAILY_USD = decimal_setting("ERP_AI_DAILY_USD", "0.50")
TURN_USD = decimal_setting("ERP_AI_TURN_USD", "0.20")
# Product-designer turns run web research and image generation, so they reserve more per message.
DESIGNER_TURN_USD = decimal_setting("ERP_AI_DESIGNER_TURN_USD", "0.45")
DAILY_TURNS = integer_setting("ERP_AI_DAILY_TURNS", "30")
MAX_CALLS = int(os.getenv("ERP_AI_MAX_CALLS", "60"))  # dibatasi biaya per pesan/harian, bukan langkah
MAX_WRITES = 5
MAX_TOOLS = int(os.getenv("ERP_AI_MAX_TOOLS", "200"))
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


# Explicit allowlist: never infer an API provider from an arbitrary model name.
MANAGER_MODEL = os.getenv("OPENAI_MODEL", "gpt-6.1-sol")
MANAGER_RATES = {"gpt-6.1-sol": ("2", "10"), "gpt-6-luna": ("0.1", "0.5"), "gpt-6-astra": ("10", "50")}
MANAGER_INPUT_RATE = decimal_setting("ERP_MANAGER_INPUT_USD_PER_MILLION", MANAGER_RATES.get(MANAGER_MODEL, ("0", "0"))[0])
MANAGER_OUTPUT_RATE = decimal_setting("ERP_MANAGER_OUTPUT_USD_PER_MILLION", MANAGER_RATES.get(MANAGER_MODEL, ("0", "0"))[1])


def manager_ready():
    return os.getenv("ERP_AI_ENABLED", "true").lower() == "true" and bool(os.getenv("OPENAI_API_KEY", "").strip())


def is_manager(model):
    return model in MANAGER_RATES


def selected_model(agent="auto"):
    if manager_ready() and (agent == "auto" or not ready()):
        return MANAGER_MODEL
    return MODEL


def model_ready(model):
    return manager_ready() if is_manager(model) else (model == MODEL or model in RATES) and ready()


def rates(model):
    if is_manager(model):
        if model != MANAGER_MODEL:
            return tuple(Decimal(v) for v in MANAGER_RATES[model])
        return MANAGER_INPUT_RATE, MANAGER_OUTPUT_RATE
    if model != MODEL and model in RATES:
        return tuple(Decimal(v) for v in RATES[model])
    return INPUT_RATE, OUTPUT_RATE


def model_limits(model):
    price = rates(model)
    known = model in MANAGER_RATES if is_manager(model) else (model != MODEL and model in RATES) or model == MODEL and (MODEL in RATES or bool(os.getenv("ERP_AI_INPUT_USD_PER_MILLION") and os.getenv("ERP_AI_OUTPUT_USD_PER_MILLION")))
    return known and all(v.is_finite() and v > 0 for v in (*price, DAILY_USD, TURN_USD)) and DAILY_TURNS > 0
