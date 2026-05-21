from sqlmodel import Session, SQLModel, create_engine

from portal.config import ensure_data_dir, settings

ensure_data_dir()

engine = create_engine(
    settings.database_url,
    echo=False,
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
)


def init_db() -> None:
    from portal import models  # noqa: F401  (register models with metadata)
    SQLModel.metadata.create_all(engine)


def get_db():
    with Session(engine) as session:
        yield session
