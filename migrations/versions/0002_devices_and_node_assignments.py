"""devices and per-subscription node assignments

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "devices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subscription_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(64), nullable=False, server_default="Устройство"),
        sa.Column("device_token", sa.String(64), nullable=False),
        sa.Column("client_uuid", sa.String(36), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("device_token"),
    )
    op.create_index("ix_devices_subscription_id", "devices", ["subscription_id"])
    op.create_index("ix_devices_device_token", "devices", ["device_token"])

    op.create_table(
        "subscription_nodes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subscription_id", sa.Integer(), nullable=False),
        sa.Column("node_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["node_id"], ["nodes.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("subscription_id", "node_id"),
    )
    op.create_index("ix_subscription_nodes_subscription_id", "subscription_nodes", ["subscription_id"])
    op.create_index("ix_subscription_nodes_node_id", "subscription_nodes", ["node_id"])

    op.add_column("subscription_clients", sa.Column("device_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_subscription_clients_device_id",
        "subscription_clients",
        "devices",
        ["device_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint(
        "subscription_clients_subscription_id_inbound_id_key",
        "subscription_clients",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_subscription_clients_device_inbound",
        "subscription_clients",
        ["device_id", "inbound_id"],
    )
    op.create_index("ix_subscription_clients_device_id", "subscription_clients", ["device_id"])

    bind = op.get_bind()
    subscriptions = bind.execute(sa.text("SELECT id, sub_token, client_uuid FROM subscriptions")).mappings().all()
    active_nodes = [row[0] for row in bind.execute(sa.text("SELECT id FROM nodes WHERE is_active = true"))]
    for sub in subscriptions:
        device_id = bind.execute(
            sa.text(
                "INSERT INTO devices (subscription_id, name, device_token, client_uuid) "
                "VALUES (:sub_id, :name, :token, :uuid) RETURNING id"
            ),
            {"sub_id": sub["id"], "name": "Основное устройство", "token": sub["sub_token"], "uuid": sub["client_uuid"]},
        ).scalar_one()
        bind.execute(
            sa.text("UPDATE subscription_clients SET device_id = :device_id WHERE subscription_id = :sub_id"),
            {"device_id": device_id, "sub_id": sub["id"]},
        )
        for node_id in active_nodes:
            bind.execute(
                sa.text("INSERT INTO subscription_nodes (subscription_id, node_id) VALUES (:sub_id, :node_id) ON CONFLICT DO NOTHING"),
                {"sub_id": sub["id"], "node_id": node_id},
            )


def downgrade() -> None:
    op.drop_index("ix_subscription_clients_device_id", table_name="subscription_clients")
    op.drop_constraint("uq_subscription_clients_device_inbound", "subscription_clients", type_="unique")
    op.create_unique_constraint(
        "subscription_clients_subscription_id_inbound_id_key",
        "subscription_clients",
        ["subscription_id", "inbound_id"],
    )
    op.drop_constraint("fk_subscription_clients_device_id", "subscription_clients", type_="foreignkey")
    op.drop_column("subscription_clients", "device_id")
    op.drop_table("subscription_nodes")
    op.drop_table("devices")
