"""app: remember admin revocations across replaces; gate undeclared apps

Revision ID: 4f7a2c9e1b05
Revises: 9c41e7d2a8b3
Create Date: 2026-10-07 16:20:00.000000

Replace used to infer revocations from the previous manifest (requested but
not allowed), so a revocation was forgotten as soon as one version stopped
requesting the origin or service — the next version that asked again got it
auto-approved. ``revoked_origins`` / ``revoked_services`` record the admin's
"no" directly. Backfilled from exactly that inference, so existing
revocations carry over.

``services_ungated``: an app that declared no services used to be allowed
every service. From here on it's allowed none; apps already installed that
way are flagged so they keep working until re-uploaded with a list.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # autogenerate emits sqlmodel.sql.sqltypes.AutoString references


# revision identifiers, used by Alembic.
revision: str = '4f7a2c9e1b05'
down_revision: Union[str, Sequence[str], None] = '9c41e7d2a8b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("app") as batch_op:
        batch_op.add_column(sa.Column("revoked_origins", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("revoked_services", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column(
            "services_ungated", sa.Boolean(), nullable=False, server_default=sa.false()))

    app = sa.table(
        "app",
        sa.column("id", sa.Integer),
        sa.column("services", sa.JSON),
        sa.column("allowed_services", sa.JSON),
        sa.column("requested_origins", sa.JSON),
        sa.column("allowed_origins", sa.JSON),
        sa.column("revoked_origins", sa.JSON),
        sa.column("revoked_services", sa.JSON),
        sa.column("services_ungated", sa.Boolean),
    )
    bind = op.get_bind()
    rows = bind.execute(sa.select(
        app.c.id, app.c.services, app.c.allowed_services,
        app.c.requested_origins, app.c.allowed_origins,
    )).all()
    for row in rows:
        bind.execute(
            app.update().where(app.c.id == row.id).values(
                revoked_origins=sorted(set(row.requested_origins or []) - set(row.allowed_origins or [])),
                revoked_services=sorted(set(row.services or []) - set(row.allowed_services or [])),
                services_ungated=not row.services,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("app") as batch_op:
        batch_op.drop_column("services_ungated")
        batch_op.drop_column("revoked_services")
        batch_op.drop_column("revoked_origins")
