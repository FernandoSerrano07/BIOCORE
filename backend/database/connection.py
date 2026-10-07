import os
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

# Cargar las variables de entorno desde el archivo .env (si existe en entorno local)
load_dotenv()

# Obtener la URL de la base de datos desde el entorno o .env
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError(
        "La variable de entorno DATABASE_URL no se encuentra inyectada. "
        "Verifica que la variable DATABASE_URL esté definida en el panel de Railway."
    )

# Asegurar compatibilidad con SQLAlchemy si la URL empieza con 'postgres://'
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Configuración del Engine para SQLAlchemy con el Transaction Pooler de Supabase
connect_args = {"prepare_threshold": None} if "postgresql" in DATABASE_URL else {}

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
    """Proporciona una sesión de base de datos dentro del contexto de la solicitud."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_extensions(db_engine):
    """Activa extensiones necesarias en PostgreSQL/Supabase."""
    with db_engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        conn.commit()
