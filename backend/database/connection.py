import logging

from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

from backend.config import settings

logger = logging.getLogger(__name__)

DATABASE_URL = settings.DATABASE_URL

# Railway puede entregar "postgres://" o "postgresql://"; normalizamos
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Detectar qué driver de Postgres está instalado
connect_args = {}
if DATABASE_URL.startswith("postgresql://"):
    try:
        import psycopg  # noqa: F401  (psycopg v3)
        DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)
        connect_args = {"prepare_threshold": None}
    except ImportError:
        # Sin psycopg v3: SQLAlchemy usará psycopg2 por defecto
        connect_args = {}

engine = create_engine(
    DATABASE_URL,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    connect_args=connect_args,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_extensions():
    """Crea la extensión pgvector si la base de datos la soporta."""
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        logger.info("Extensión pgvector lista.")
    except Exception as exc:
        logger.error("No se pudo crear la extensión 'vector': %s", exc)
        raise
