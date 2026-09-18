"""Transaction types, mitra, proofs JSON, is_closing flag.

Revision ID: 20260918_06_master_and_proofs
Revises: 20260918_05_identity
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260918_06_master_and_proofs"
down_revision = "20260918_05_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "transaction_types",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("debit", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("credit", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("group_code", sa.String(length=20), nullable=False, server_default="BUMDES"),
        sa.Column("unit_codes", postgresql.ARRAY(sa.String(length=20)), nullable=False, server_default="{}"),
        sa.UniqueConstraint("code", "group_code", name="uq_tx_types_code_group"),
    )
    op.create_index("ix_transaction_types_code", "transaction_types", ["code"])
    op.create_index("ix_transaction_types_group_code", "transaction_types", ["group_code"])

    op.create_table(
        "mitra",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("unit_usaha_id", sa.String(length=36), nullable=True),
        sa.Column("phone", sa.String(length=50), nullable=False, server_default=""),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.ForeignKeyConstraint(["unit_usaha_id"], ["unit_usaha.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_mitra_unit_usaha_id", "mitra", ["unit_usaha_id"])

    op.add_column(
        "transactions",
        sa.Column("is_closing", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "transactions",
        sa.Column("proofs", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
    )


def downgrade() -> None:
    op.drop_column("transactions", "proofs")
    op.drop_column("transactions", "is_closing")
    op.drop_index("ix_mitra_unit_usaha_id", table_name="mitra")
    op.drop_table("mitra")
    op.drop_index("ix_transaction_types_group_code", table_name="transaction_types")
    op.drop_index("ix_transaction_types_code", table_name="transaction_types")
    op.drop_table("transaction_types")
