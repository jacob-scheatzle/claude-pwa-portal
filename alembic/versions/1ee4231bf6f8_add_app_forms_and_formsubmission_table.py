"""add app.forms and formsubmission table

Revision ID: 1ee4231bf6f8
Revises: 13ed9c934c09
Create Date: 2026-05-29 16:23:42.632714

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # autogenerate emits sqlmodel.sql.sqltypes.AutoString references


# revision identifiers, used by Alembic.
revision: str = '1ee4231bf6f8'
down_revision: Union[str, Sequence[str], None] = '13ed9c934c09'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # New JSON column on app; existing rows get NULL, read as [] (``app.forms or []``).
    with op.batch_alter_table('app', schema=None) as batch_op:
        batch_op.add_column(sa.Column('forms', sa.JSON(), nullable=True))

    op.create_table(
        'formsubmission',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('app_slug', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('form_name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('data', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('source_ip', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('formsubmission', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_formsubmission_app_slug'), ['app_slug'], unique=False)
        batch_op.create_index(batch_op.f('ix_formsubmission_form_name'), ['form_name'], unique=False)
        batch_op.create_index(batch_op.f('ix_formsubmission_created_at'), ['created_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('formsubmission', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_formsubmission_created_at'))
        batch_op.drop_index(batch_op.f('ix_formsubmission_form_name'))
        batch_op.drop_index(batch_op.f('ix_formsubmission_app_slug'))
    op.drop_table('formsubmission')
    with op.batch_alter_table('app', schema=None) as batch_op:
        batch_op.drop_column('forms')
