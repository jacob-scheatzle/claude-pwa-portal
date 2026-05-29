"""add app.tools for declarative MCP tools

Revision ID: 665c77fdc151
Revises: b2f343b20d7e
Create Date: 2026-05-29 12:26:50.259918

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # autogenerate emits sqlmodel.sql.sqltypes.AutoString references


# revision identifiers, used by Alembic.
revision: str = '665c77fdc151'
down_revision: Union[str, Sequence[str], None] = 'b2f343b20d7e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # New JSON column; existing rows get NULL, which the app reads as an empty
    # tool list (``list(app.tools or [])``). No backfill needed — pre-feature
    # apps expose no MCP tools until re-uploaded with a manifest declaring them.
    with op.batch_alter_table('app', schema=None) as batch_op:
        batch_op.add_column(sa.Column('tools', sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('app', schema=None) as batch_op:
        batch_op.drop_column('tools')
