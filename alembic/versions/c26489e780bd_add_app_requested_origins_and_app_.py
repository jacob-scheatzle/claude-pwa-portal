"""add app.requested_origins and app.allowed_origins

Revision ID: c26489e780bd
Revises: 3e8276bd98cf
Create Date: 2026-05-26 17:22:52.717311

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # autogenerate emits sqlmodel.sql.sqltypes.AutoString references


# revision identifiers, used by Alembic.
revision: str = 'c26489e780bd'
down_revision: Union[str, Sequence[str], None] = '3e8276bd98cf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('app', schema=None) as batch_op:
        batch_op.add_column(sa.Column('requested_origins', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('allowed_origins', sa.JSON(), nullable=True))

    # Backfill existing rows so application code can rely on a list value
    # without defensively coercing None. New rows from the Python model get
    # an empty list via default_factory=list at insert time.
    op.execute("UPDATE app SET requested_origins = '[]' WHERE requested_origins IS NULL")
    op.execute("UPDATE app SET allowed_origins = '[]' WHERE allowed_origins IS NULL")


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('app', schema=None) as batch_op:
        batch_op.drop_column('allowed_origins')
        batch_op.drop_column('requested_origins')
