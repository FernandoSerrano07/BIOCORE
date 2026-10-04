from sqlalchemy import Column, Integer, String, DateTime, Text, Boolean, Enum, Time, Float, Index, event
from sqlalchemy.sql import func
from sqlalchemy.dialects.postgresql import JSONB
import enum
from .connection import Base


class TipoMarcaje(enum.Enum):
    ENTRADA            = "entrada"
    SALIDA_ALMUERZO    = "salida_almuerzo"
    REGRESO_ALMUERZO   = "regreso_almuerzo"
    SALIDA             = "salida"
    SALIDA_IMPREVISTA  = "salida_imprevista"
    SALIDA_ENFERMEDAD  = "salida_enfermedad"


class EstadoJustificacion(enum.Enum):
    PENDIENTE = "pendiente"
    APROBADA  = "aprobada"
    RECHAZADA = "rechazada"


class TipoAccion(enum.Enum):
    LOGIN_ADMIN          = "login_admin"
    LOGOUT_ADMIN         = "logout_admin"
    CREAR_USUARIO        = "crear_usuario"
    DESACTIVAR_USUARIO   = "desactivar_usuario"
    ACTUALIZAR_HORARIO   = "actualizar_horario"
    RESOLVER_JUSTIFICACION = "resolver_justificacion"
    EXPORTAR_REPORTE     = "exportar_reporte"
    ELIMINAR_ENCODING    = "eliminar_encoding"


class Usuario(Base):
    __tablename__ = "usuarios"

    id                   = Column(Integer, primary_key=True, index=True)
    nombre               = Column(String(100), nullable=False)
    apellido             = Column(String(100), nullable=False)
    cargo                = Column(String(100), nullable=False)
    departamento         = Column(String(100), nullable=True)
    email                = Column(String(150), unique=True, nullable=False, index=True)
    activo               = Column(Boolean, default=True, index=True)
    foto_path            = Column(String(255), nullable=True)
    encoding_facial      = Column(Text, nullable=True)       # JSON string — se mantiene para compatibilidad
    creado_en            = Column(DateTime, server_default=func.now())

    # Horario laboral
    turno_entrada        = Column(Time, nullable=True)
    turno_salida         = Column(Time, nullable=True)
    minutos_almuerzo     = Column(Integer, default=60)
    horas_laborales_dia  = Column(Integer, default=8)

    # Control de intentos fallidos (anti-spoofing)
    intentos_fallidos    = Column(Integer, default=0)
    bloqueado_hasta      = Column(DateTime, nullable=True)


class Marcaje(Base):
    __tablename__ = "marcajes"

    id                   = Column(Integer, primary_key=True, index=True)
    usuario_id           = Column(Integer, nullable=False, index=True)
    tipo                 = Column(Enum(TipoMarcaje), nullable=False)
    fecha_hora           = Column(DateTime, server_default=func.now(), index=True)
    es_especial          = Column(Boolean, default=False, index=True)

    justificacion_texto  = Column(Text, nullable=True)
    justificacion_audio  = Column(String(255), nullable=True)
    estado_justificacion = Column(Enum(EstadoJustificacion), nullable=True)
    comentario_admin     = Column(Text, nullable=True)
    foto_marcaje         = Column(String(255), nullable=True)

    # Campos de análisis de tiempo (se calculan al registrar)
    minutos_tardanza     = Column(Integer, default=0)     # minutos de retraso respecto al turno
    minutos_extra        = Column(Integer, default=0)     # minutos extra trabajados ese día
    similitud_biometrica = Column(Float, nullable=True)   # confianza del reconocimiento


class Administrador(Base):
    __tablename__ = "administradores"

    id            = Column(Integer, primary_key=True, index=True)
    nombre        = Column(String(100), nullable=False)
    email         = Column(String(150), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    es_superadmin = Column(Boolean, default=False)
    creado_en     = Column(DateTime, server_default=func.now())
    ultimo_login  = Column(DateTime, nullable=True)


class LogAuditoria(Base):
    __tablename__ = "logs_auditoria"

    id            = Column(Integer, primary_key=True, index=True)
    admin_id      = Column(Integer, nullable=True)          # null = superadmin maestro
    admin_email   = Column(String(150), nullable=False)
    accion        = Column(Enum(TipoAccion), nullable=False)
    detalle       = Column(JSONB, nullable=True)            # datos extra en JSON
    ip            = Column(String(45), nullable=True)       # IPv4 o IPv6
    fecha_hora    = Column(DateTime, server_default=func.now(), index=True)


# ── Índices compuestos para queries frecuentes ──────────────────────────
# Buscar marcajes de un usuario en un rango de fechas (el query más común)
Index("ix_marcajes_usuario_fecha", Marcaje.usuario_id, Marcaje.fecha_hora)

# Filtrar justificaciones pendientes rápidamente
Index("ix_marcajes_especial_estado", Marcaje.es_especial, Marcaje.estado_justificacion)