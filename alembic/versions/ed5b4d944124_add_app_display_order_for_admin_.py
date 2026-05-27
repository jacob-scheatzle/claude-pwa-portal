"""add app.display_order for admin-controlled tile ordering

Revision ID: ed5b4d944124
Revises: a54093f2954d
Create Date: 2026-05-27 09:30:45.560901

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # autogenerate emits sqlmodel.sql.sqltypes.AutoString references


# revision identifiers, used by Alembic.
revision: str = 'ed5b4d944124'
down_revision: Union[str, Sequence[str], None] = 'a54093f2954d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # NOT NULL Integer requires a server default for the ALTER on a populated
    # table — SQLite otherwise rejects ALTER ADD COLUMN. New rows from the
    # ORM supply their own default of 0; existing rows are backfilled below
    # from the current alphabetical-by-name order so the dashboard tile
    # order doesn't visibly shift on upgrade.
    with op.batch_alter_table('app', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'display_order',
                sa.Integer(),
                nullable=False,
                server_default='0',
            )
        )
        batch_op.create_index(
            batch_op.f('ix_app_display_order'),
            ['display_order'],
            unique=False,
        )

    # Backfill existing rows with spaced increments (10, 20, …) so admins
    # have headroom to reshuffle via the up/down chips without colliding on
    # the same display_order value before the full-list renumber runs.
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id FROM app ORDER BY name")).fetchall()
    for i, row in enumerate(rows):
        bind.execute(
            sa.text("UPDATE app SET display_order = :v WHERE id = :id"),
            {"v": (i + 1) * 10, "id": row[0]},
        )

    # Drop the server default after backfill so the column behaves like the
    # other integer state in the table — values flow from the model.
    with op.batch_alter_table('app', schema=None) as batch_op:
        batch_op.alter_column('display_order', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('app', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_app_display_order'))
        batch_op.drop_column('display_order')
