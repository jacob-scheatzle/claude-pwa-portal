"""user: AUTOINCREMENT ids on SQLite so a deleted user's id is never reused

Revision ID: 9c41e7d2a8b3
Revises: 2154c41b0659
Create Date: 2026-10-07 15:10:00.000000

SQLite's plain INTEGER PRIMARY KEY hands out max(id) + 1, so deleting the
newest user and then creating another gives the newcomer the old id — and with
it everything still keyed by that id: per-app storage namespaces
(``storage/<slug>/<uid>/``), storage share links that re-read them, and audit
history. Rebuilding ``user`` with AUTOINCREMENT makes ids strictly increasing.

The rebuild seeds ``sqlite_sequence`` from the current max id, which alone
wouldn't cover users deleted *before* this migration whose data is still
around. So the sequence is raised to the highest user id referenced anywhere
— every user-id column, plus the storage namespaces on disk.

PostgreSQL is untouched: its sequences never reuse an id.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # autogenerate emits sqlmodel.sql.sqltypes.AutoString references


# revision identifiers, used by Alembic.
revision: str = '9c41e7d2a8b3'
down_revision: Union[str, Sequence[str], None] = '2154c41b0659'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Every column that holds a user id, as of this revision.
_USER_ID_COLUMNS = (
    ("user", "id"),
    ("apitoken", "created_by"),
    ("oauthcode", "user_id"),
    ("oauthtoken", "user_id"),
    ("usersession", "user_id"),
    ("applaunchtoken", "user_id"),
    ("appsession", "user_id"),
    ("emailsendlog", "user_id"),
    ("sharelink", "created_by"),
    ("auditevent", "actor_user_id"),
    ("userappaccess", "user_id"),
    ("scheduledrun", "user_id"),
    ("scheduledrun", "created_by"),
)


def _storage_user_ids() -> set[int]:
    """User ids that own a storage namespace (``storage/<slug>/<uid>/...``).

    Best-effort: a storage backend that can't be listed here only means the
    floor falls back to the DB-derived one.
    """
    try:
        from portal.storage_backend import get_storage

        ids = set()
        for obj in get_storage().list("storage"):
            parts = obj.key.split("/")
            if len(parts) > 3 and parts[2].isdigit():
                ids.add(int(parts[2]))
        return ids
    except Exception:
        return set()


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return

    with op.batch_alter_table(
        "user", recreate="always", table_kwargs={"sqlite_autoincrement": True}
    ):
        pass

    floor = 0
    for table, column in _USER_ID_COLUMNS:
        value = bind.execute(sa.text(f'SELECT MAX("{column}") FROM "{table}"')).scalar()
        floor = max(floor, value or 0)
    floor = max([floor, *_storage_user_ids()])

    # The rebuild's row copy created the sequence row (when there were rows to
    # copy); make sure it exists, then raise it to the floor.
    bind.execute(sa.text(
        "INSERT INTO sqlite_sequence (name, seq) SELECT 'user', 0 "
        "WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name = 'user')"
    ))
    bind.execute(
        sa.text("UPDATE sqlite_sequence SET seq = :floor WHERE name = 'user' AND seq < :floor"),
        {"floor": floor},
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return

    with op.batch_alter_table(
        "user", recreate="always", table_kwargs={"sqlite_autoincrement": False}
    ):
        pass
    bind.execute(sa.text("DELETE FROM sqlite_sequence WHERE name = 'user'"))
