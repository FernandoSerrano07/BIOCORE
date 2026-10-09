import enum
from datetime import datetime
from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    DateTime,
    Time,
    Float,
    Text,
    Enum,
    ForeignKey,
    Table,
    JSON,
)
from sqlalchemy.orm import relationship
from database.connection import Base


# Tabla intermedia entre Administradores y Sucursales
administrador_sucursales = Table(
    "administrador_sucursales",
    Base.metadata,
    Column("admin_id", Integer, ForeignKey("administradores.id", ondelete="CASCADE"), primary_key=True),
    Column("sucursal_id", Integer, ForeignKey("sucursales.id", ondelete="CASCADE"), primary_key=True),
)


class TipoMarcaje(str, enum.Enum):
    ENTRADA = "ENTRADA"
    SALIDA_ALMUERZO = "SALIDA_ALMUERZO"
    REGRESO_ALMUERZO = "REGRESO_ALMUERZO"
    SALIDA = "SALIDA"
    SALIDA_IMPREVISTA = "SALIDA_IMPREVISTA"
    SALIDA_ENFERMEDAD = "SALIDA_ENFERMEDAD"


class EstadoJustificacion(str, enum.Enum):
    PENDIENTE = "PENDIENTE"
    APROBADA = "APROBADA"
    RECHAZADA = "RECHAZADA"


class TipoAccion(str, enum.Enum):
    LOGIN_ADMIN = "LOGIN_ADMIN"
    LOGOUT_ADMIN = "LOGOUT_ADMIN"
    CREAR_USUARIO = "CREAR_USUARIO"
    ACTUALIZAR_HORARIO = "ACTUALIZAR_HORARIO"
    DESACTIVAR_USUARIO = "DESACTIVAR_USUARIO"
    CREAR_ADMINISTRADOR = "CREAR_ADMINISTRADOR"
    CAMBIAR_PASSWORD_ADMIN = "CAMBIAR_PASSWORD_ADMIN"
    ELIMINAR_ADMIN = "ELIMINAR_ADMIN"
    RESOLVER_JUSTIFICACION = "RESOLVER_JUSTIFICACION"
    EXPORTAR_REPORTE = "EXPORTAR_REPORTE"
    # Acciones para Sucursales
    CREAR_SUCURSAL = "CREAR_SUCURSAL"
    EDITAR_SUCURSAL = "EDITAR_SUCURSAL"
    DESACTIVAR_SUCURSAL = "DESACTIVAR_SUCURSAL"
    ACTIVAR_SUCURSAL = "ACTIVAR_SUCURSAL"
    ASIGNAR_SUCURSALES_ADMIN = "ASIGNAR_SUCURSALES_ADMIN"


class Sucursal(Base):
    __tablename__ = "sucursales"

    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String(100), nullable=False)
    codigo = Column(String(20), unique=True, nullable=False, index=True)
    direccion = Column(String(255), nullable=True)
    zona_horaria = Column(String(50), nullable=False, default="America/El_Salvador")
    activa = Column(Boolean, nullable=False, default=True)
    creado_en = Column(DateTime, default=datetime.utcnow)

    # Relaciones
    usuarios = relationship("Usuario", back_populates="sucursal")
    marcajes = relationship("Marcaje", back_populates="sucursal")
    administradores = relationship("Administrador", secondary=administrador_sucursales, back_populates="sucursales")


class Usuario(Base):
    __tablename__ = "usuarios"

    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String(100), nullable=False)
    apellido = Column(String(100), nullable=False)
    cargo = Column(String(100), nullable=False)
    departamento = Column(String(100), nullable=True)
    email = Column(String(150), unique=True, nullable=False, index=True)
    activo = Column(Boolean, default=True)
    foto_path = Column(String(255), nullable=True)
    encoding_facial = Column(Text, nullable=True)
    creado_en = Column(DateTime, default=datetime.utcnow)

    # Horario predeterminado
    turno_entrada = Column(Time, nullable=True)
    turno_salida = Column(Time, nullable=True)
    minutos_almuerzo = Column(Integer, default=60)
    horas_laborales_dia = Column(Integer, default=8)

    # Bloqueo temporal por reintentos de reconocimiento facial
    intentos_fallidos = Column(Integer, default=0)
    bloqueado_hasta = Column(DateTime, nullable=True)

    # FK Sucursal
    sucursal_id = Column(Integer, ForeignKey("sucursales.id"), nullable=True)
    sucursal = relationship("Sucursal", back_populates="usuarios")

    marcajes = relationship("Marcaje", back_populates="usuario", cascade="all, delete-orphan")


class Administrador(Base):
    __tablename__ = "administradores"

    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String(100), nullable=False)
    email = Column(String(150), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    es_superadmin = Column(Boolean, default=False)
    creado_en = Column(DateTime, default=datetime.utcnow)
    ultimo_login = Column(DateTime, nullable=True)

    # Relación con Sucursales
    sucursales = relationship("Sucursal", secondary=administrador_sucursales, back_populates="administradores")


class Marcaje(Base):
    __tablename__ = "marcajes"

    id = Column(Integer, primary_key=True, index=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    tipo = Column(Enum(TipoMarcaje), nullable=False)
    fecha_hora = Column(DateTime, default=datetime.utcnow, index=True)

    # Justificación de salidas especiales / imprevistas
    es_especial = Column(Boolean, default=False)
    justificacion_texto = Column(Text, nullable=True)
    justificacion_audio = Column(String(255), nullable=True)
    estado_justificacion = Column(
        Enum(EstadoJustificacion),
        default=EstadoJustificacion.PENDIENTE,
    )
    comentario_admin = Column(Text, nullable=True)

    # Foto del marcaje y cálculo de tardanza/extras
    foto_marcaje = Column(String(255), nullable=True)
    minutos_tardanza = Column(Integer, default=0)
    minutos_extra = Column(Integer, default=0)
    similitud_biometrica = Column(Float, nullable=True)

    # FK Sucursal
    sucursal_id = Column(Integer, ForeignKey("sucursales.id"), nullable=True)
    sucursal = relationship("Sucursal", back_populates="marcajes")

    usuario = relationship("Usuario", back_populates="marcajes")


class LogAuditoria(Base):
    __tablename__ = "logs_auditoria"

    id = Column(Integer, primary_key=True, index=True)
    admin_id = Column(Integer, ForeignKey("administradores.id"), nullable=True)
    admin_email = Column(String(150), nullable=False)
    accion = Column(Enum(TipoAccion), nullable=False)
    detalle = Column(JSON, nullable=True)  # Mapeado como JSON nativo de SQLAlchemy
    ip = Column(String(45), nullable=True)
    fecha_hora = Column(DateTime, default=datetime.utcnow)
