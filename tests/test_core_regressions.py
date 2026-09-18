from services.jwt_service import create_access_token, decode_token
from decimal import Decimal

from database import _matches, _query_condition
from models.transactions import TransactionCreate


def test_query_match_supports_common_filters():
    document = {"id": "tx-1", "date": "2026-01-15", "unit_usaha_id": "unit-1"}
    assert _matches(document, {"date": {"gte": "2026-01-01", "lte": "2026-01-31"}})
    assert _matches(document, {"id": {"in": ["tx-1", "tx-2"]}})
    assert not _matches(document, {"unit_usaha_id": "unit-2"})


def test_query_condition_compiles_for_nested_document_filters():
    expression = _query_condition({"date": {"gte": "2026-01-01"}, "unit_usaha_id": "unit-1"})
    assert expression is not None


def test_transaction_amount_uses_decimal():
    transaction = TransactionCreate(
        date="2026-01-15",
        transaction_type="penerimaan",
        description="Setoran",
        amount="1250.10",
        debit_account_code="1.1",
        credit_account_code="4.1",
    )
    assert transaction.amount == Decimal("1250.10")


def test_jwt_round_trip():
    token = create_access_token("user-1", "admin")
    payload = decode_token(token)
    assert payload["sub"] == "user-1"
    assert payload["role"] == "admin"
