"""add scheduledrun table

Revision ID: 13ed9c934c09
Revises: 665c77fdc151
Create Date: 2026-05-29 16:11:56.914334

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # autogenerate emits sqlmodel.sql.sqltypes.AutoString references


# revision identifiers, used by Alembic.
revision: str = '13ed9c934c09'
down_revision: Union[str, Sequence[str], None] = '665c77fdc151'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'scheduledrun',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('app_slug', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('tool_name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('args', sa.JSON(), nullable=True),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('label', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('frequency', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('hour', sa.Integer(), nullable=False),
        sa.Column('minute', sa.Integer(), nullable=False),
        sa.Column('day_of_week', sa.Integer(), nullable=False),
        sa.Column('day_of_month', sa.Integer(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('next_run_at', sa.DateTime(), nullable=False),
        sa.Column('last_run_at', sa.DateTime(), nullable=True),
        sa.Column('last_status', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('last_result', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
        sa.ForeignKeyConstraint(['created_by'], ['user.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('scheduledrun', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scheduledrun_app_slug'), ['app_slug'], unique=False)
        batch_op.create_index(batch_op.f('ix_scheduledrun_enabled'), ['enabled'], unique=False)
        batch_op.create_index(batch_op.f('ix_scheduledrun_next_run_at'), ['next_run_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('scheduledrun', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_scheduledrun_next_run_at'))
        batch_op.drop_index(batch_op.f('ix_scheduledrun_enabled'))
        batch_op.drop_index(batch_op.f('ix_scheduledrun_app_slug'))
    op.drop_table('scheduledrun')
