import os
import asyncio
from datetime import datetime, date, timedelta

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from database.connection import get_db
from database.models import Usuario, Marcaje, TipoMarcaje, EstadoJustificacion
from biometric.detector import generar_encoding, buscar_en_indice, agregar_usuario_al_indice

router = APIRouter()

FOTOS_DIR = "fotos_registro"

# ── Configuración para Autenticación JWT ──────────────────────────────
SECRET_KEY = os.getenv("SECRET_KEY", "biocore_secret_key_change_me")
ALGORITHM = "HS256"
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token", auto_error=False)

def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    """Dependencia de autenticación para validar solicitudes seguras."""
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No autenticado",
            headers={"WWW-Authenticate": "Bearer"},
        )
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="No se pudieron validar las credenciales",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    
    usuario = db.query(Usuario).filter(Usuario.email == username).first()
    if usuario is None:
        raise credentials_exception
    return usuario


# ── Orden válido de marcajes en el día ─────────────────────────────────
ORDEN_MARCAJES = [
    TipoMarcaje.ENTRADA,
    TipoMarcaje.SALIDA_ALMUERZO,
    TipoMarcaje.REGRESO_ALMUERZO,
    TipoMarcaje.SALIDA,
]
# Los especiales interrumpen el flujo — se permiten después de ENTRADA
TIPOS_ESPECIALES = {TipoMarcaje.SALIDA_IMPREVISTA, TipoMarcaje.SALIDA_ENFERMEDAD}


def _marcajes_hoy(usuario_id: int, db: Session):
    """Retorna los marcajes del usuario de hoy, ordenados cronológicamente."""
    hoy = date.today()
    inicio_dia = datetime.combine(hoy, datetime.min.time())
    fin_dia = datetime.combine(hoy, datetime.max.time())
    
    return (
        db.query(Marcaje)
        .filter(
            Marcaje.usuario_id == usuario_id,
            Marcaje.fecha_hora >= inicio_dia,
            Marcaje.fecha_hora <= fin_dia,
        )
        .order_by(Marcaje.fecha_hora)
        .all()
    )


def _validar_orden_marcaje(tipo_nuevo: TipoMarcaje, marcajes_hoy: list):
    """
    Valida que el marcaje tenga sentido dado lo que ya se registró hoy.
    Retorna None si está bien, o un string con el motivo del rechazo.
    """
    tipos_hoy = [m.tipo for m in marcajes_hoy]

    # No puede haber dos marcajes del mismo tipo el mismo día
    if tipo_nuevo in tipos_hoy:
        val_str = tipo_nuevo.value if hasattr(tipo_nuevo, 'value') else str(tipo_nuevo)
        return f"Ya registraste '{val_str}' hoy."

    if tipo_nuevo == TipoMarcaje.ENTRADA:
        # La entrada siempre es el primero — si ya hay algo, error
        if tipos_hoy:
            return "Ya registraste entrada hoy."
        return None

    # Para cualquier otro tipo, debe haber entrada primero
    if TipoMarcaje.ENTRADA not in tipos_hoy:
        return "Debes registrar entrada primero."

    if tipo_nuevo == TipoMarcaje.SALIDA_ALMUERZO:
        return None  # Solo necesita entrada previa

    if tipo_nuevo == TipoMarcaje.REGRESO_ALMUERZO:
        if TipoMarcaje.SALIDA_ALMUERZO not in tipos_hoy:
            return "Debes registrar salida de almuerzo primero."
        return None

    if tipo_nuevo == TipoMarcaje.SALIDA:
        # No puede salir si ya hubo una salida especial
        for t in tipos_hoy:
            if t in TIPOS_ESPECIALES:
                return "Ya registraste una salida especial hoy."
        return None

    if tipo_nuevo in TIPOS_ESPECIALES:
        # Ya se chequeó que no haya dos del mismo tipo
        # No puede ser especial si ya registró salida normal
        if TipoMarcaje.SALIDA in tipos_hoy:
            return "Ya registraste salida normal hoy."
        return None

    return None


def _calcular_tardanza_minutos(usuario: Usuario, hora_entrada: datetime):
    """
    Calcula cuántos minutos llegó tarde respecto a su turno.
    Retorna 0 si llegó a tiempo o no tiene turno asignado.
    """
    if not usuario.turno_entrada:
        return 0

    turno_dt = datetime.combine(hora_entrada.date(), usuario.turno_entrada)
    diferencia = (hora_entrada - turno_dt).total_seconds() / 60.0

    # Tolerancia de 5 minutos
    if diferencia > 5:
        return int(diferencia)
    return 0


def _calcular_horas_extra_minutos(usuario: Usuario, marcajes_hoy: list):
    """
    Al registrar la salida, calcula cuántos minutos extra trabajó.
    Necesita los marcajes del día completos.
    """
    if not usuario.turno_salida:
        return 0

    # Buscar la hora de salida registrada
    salida = next(
        (m for m in marcajes_hoy if m.tipo in (TipoMarcaje.SALIDA, *TIPOS_ESPECIALES)),
        None
    )
    if not salida:
        return 0

    turno_salida_dt = datetime.combine(salida.fecha_hora.date(), usuario.turno_salida)
    diferencia = (salida.fecha_hora - turno_salida_dt).total_seconds() / 60.0

    if diferencia > 0:
        return int(diferencia)
    return 0


# ══════════════════════════════════════════
#  USUARIOS
# ══════════════════════════════════════════

@router.get("/usuarios")
def listar_usuarios(db: Session = Depends(get_db)):
    usuarios = db.query(Usuario).filter(Usuario.activo == True).all()
    return [
        {
            "id": u.id,
            "nombre": u.nombre,
            "apellido": u.apellido,
            "cargo": u.cargo,
            "departamento": u.departamento,
            "email": u.email,
            "tiene_encoding": bool(u.encoding_facial),
        }
        for u in usuarios
    ]


@router.post("/usuarios/registrar-web")
async def registrar_usuario_web(
    nombre: str = Form(...),
    apellido: str = Form(...),
    cargo: str = Form(...),
    departamento: str = Form(None),
    email: str = Form(...),
    foto_0: UploadFile = File(...),
    foto_1: UploadFile = File(...),
    foto_2: UploadFile = File(...),
    foto_3: UploadFile = File(...),
    foto_4: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Registra un empleado con fotos capturadas desde el navegador."""
    existe = db.query(Usuario).filter(Usuario.email == email).first()
    if existe:
        raise HTTPException(status_code=400, detail="El email ya está registrado.")

    nuevo_usuario = Usuario(
        nombre=nombre, apellido=apellido,
        cargo=cargo, departamento=departamento, email=email,
    )
    db.add(nuevo_usuario)
    db.commit()
    db.refresh(nuevo_usuario)

    try:
        carpeta = os.path.join(FOTOS_DIR, str(nuevo_usuario.id))
        os.makedirs(carpeta, exist_ok=True)

        fotos = [foto_0, foto_1, foto_2, foto_3, foto_4]
        rutas = []
        for i, foto in enumerate(fotos):
            ruta = os.path.join(carpeta, f"foto_{i+1}.jpg")
            contenido = await foto.read()
            with open(ruta, "wb") as f:
                f.write(contenido)
            rutas.append(ruta)

        loop = asyncio.get_event_loop()
        encoding = await loop.run_in_executor(None, generar_encoding, carpeta)

        nuevo_usuario.encoding_facial = encoding
        nuevo_usuario.foto_path = rutas[0]
        db.commit()

        # Agregar al índice FAISS sin reconstruirlo completo
        agregar_usuario_al_indice(nuevo_usuario.id, encoding)

        return {"mensaje": "Usuario registrado correctamente", "usuario_id": nuevo_usuario.id}

    except Exception as e:
        db.delete(nuevo_usuario)
        db.commit()
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════
#  MARCAJES
# ══════════════════════════════════════════

@router.post("/marcaje/verificar")
async def verificar_identidad(foto: UploadFile = File(...), db: Session = Depends(get_db)):
    """
    Recibe un frame del navegador y encuentra al empleado usando FAISS.
    Con 1500 empleados: ~50ms. Antes con loop lineal: ~90 segundos.
    """
    ruta_temp = f"temp_verif_{os.getpid()}_{datetime.now().strftime('%f')}.jpg"
    try:
        contenido = await foto.read()
        with open(ruta_temp, "wb") as f:
            f.write(contenido)

        loop = asyncio.get_event_loop()
        usuario_id, similitud = await loop.run_in_executor(
            None, buscar_en_indice, ruta_temp
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en verificación biométrica: {e}")
    finally:
        if os.path.exists(ruta_temp):
            os.remove(ruta_temp)

    if not usuario_id:
        return {"identificado": False, "mensaje": "Rostro no reconocido"}

    usuario = db.query(Usuario).filter(Usuario.id == usuario_id, Usuario.activo == True).first()
    if not usuario:
        return {"identificado": False, "mensaje": "Empleado inactivo o no encontrado"}

    # Retornar también qué marcaje puede hacer a continuación
    marcajes_hoy = _marcajes_hoy(usuario_id, db)
    tipos_hoy = [
        m.tipo.value if hasattr(m.tipo, 'value') else str(m.tipo) 
        for m in marcajes_hoy
    ]

    return {
        "identificado": True,
        "usuario_id": usuario.id,
        "nombre": f"{usuario.nombre} {usuario.apellido}",
        "cargo": usuario.cargo,
        "departamento": usuario.departamento,
        "similitud": round(float(similitud) * 100, 2),
        "marcajes_hoy": tipos_hoy,
        "turno_entrada": str(usuario.turno_entrada) if usuario.turno_entrada else None,
        "turno_salida": str(usuario.turno_salida) if usuario.turno_salida else None,
    }


@router.post("/marcaje/registrar")
def registrar_marcaje(
    usuario_id: int = Form(...),
    tipo: str = Form(...),
    justificacion_texto: str = Form(None),
    similitud_biometrica: float = Form(None),
    db: Session = Depends(get_db),
):
    """Registra el marcaje del usuario con validación de orden y cálculo de tardanza."""
    usuario = db.query(Usuario).filter(Usuario.id == usuario_id, Usuario.activo == True).first()
    if not usuario:
        raise HTTPException(status_code=404, detail="Usuario no encontrado o inactivo.")

    try:
        tipo_enum = TipoMarcaje(tipo)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Tipo de marcaje inválido: {tipo}")

    # ── Validar orden del flujo ────────────────────────────────────────
    marcajes_hoy = _marcajes_hoy(usuario_id, db)
    error_orden = _validar_orden_marcaje(tipo_enum, marcajes_hoy)
    if error_orden:
        raise HTTPException(status_code=400, detail=error_orden)

    # ── Tipos especiales requieren justificación ───────────────────────
    es_especial = tipo_enum in TIPOS_ESPECIALES
    if es_especial and not justificacion_texto:
        raise HTTPException(status_code=400, detail="Este tipo de marcaje requiere justificación.")

    # ── Calcular tardanza si es ENTRADA ───────────────────────────────
    minutos_tardanza = 0
    if tipo_enum == TipoMarcaje.ENTRADA:
        minutos_tardanza = _calcular_tardanza_minutos(usuario, datetime.now())

    # ── Calcular horas extra si es SALIDA (o especial) ────────────────
    minutos_extra = 0
    if tipo_enum in (TipoMarcaje.SALIDA, *TIPOS_ESPECIALES):
        todos_marcajes = marcajes_hoy  # todavía no incluye el actual
        minutos_extra = _calcular_horas_extra_minutos(usuario, todos_marcajes)

    nuevo_marcaje = Marcaje(
        usuario_id=usuario_id,
        tipo=tipo_enum,
        es_especial=es_especial,
        justificacion_texto=justificacion_texto,
        estado_justificacion=EstadoJustificacion.PENDIENTE if es_especial else None,
        minutos_tardanza=minutos_tardanza,
        minutos_extra=minutos_extra,
        similitud_biometrica=similitud_biometrica,
    )
    db.add(nuevo_marcaje)
    db.commit()
    db.refresh(nuevo_marcaje)

    respuesta = {
        "mensaje": "Marcaje registrado correctamente",
        "marcaje_id": nuevo_marcaje.id,
        "tipo": tipo,
        "fecha_hora": nuevo_marcaje.fecha_hora,
        "es_especial": es_especial,
    }

    if minutos_tardanza > 0:
        respuesta["aviso_tardanza"] = f"Llegaste {minutos_tardanza} minutos tarde."
    if minutos_extra > 0:
        respuesta["aviso_extra"] = f"Trabajaste {minutos_extra} minutos extra hoy."

    return respuesta


@router.post("/marcaje/audio/{marcaje_id}")
async def guardar_audio(
    marcaje_id: int,
    audio: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Guarda la nota de voz de un marcaje especial."""
    marcaje = db.query(Marcaje).filter(Marcaje.id == marcaje_id).first()
    if not marcaje:
        raise HTTPException(status_code=404, detail="Marcaje no encontrado.")

    carpeta_audio = "static/audio"
    os.makedirs(carpeta_audio, exist_ok=True)
    nombre_audio = f"audio_{marcaje_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}.webm"
    ruta_audio = os.path.join(carpeta_audio, nombre_audio)

    contenido = await audio.read()
    with open(ruta_audio, "wb") as f:
        f.write(contenido)

    marcaje.justificacion_audio = ruta_audio
    db.commit()

    return {"mensaje": "Audio guardado correctamente", "ruta": ruta_audio}


# ── Historial del empleado (autoservicio básico) ───────────────────────

@router.get("/marcajes/empleado/{usuario_id}")
def historial_empleado(
    usuario_id: int,
    fecha_inicio: str = None,
    fecha_fin: str = None,
    db: Session = Depends(get_db),
):
    """Retorna el historial de marcajes de un empleado con tardanzas y extras."""
    usuario = db.query(Usuario).filter(Usuario.id == usuario_id).first()
    if not usuario:
        raise HTTPException(status_code=404, detail="Usuario no encontrado.")

    query = db.query(Marcaje).filter(Marcaje.usuario_id == usuario_id)

    if fecha_inicio:
        fi = datetime.strptime(fecha_inicio, "%Y-%m-%d")
        query = query.filter(Marcaje.fecha_hora >= fi)
    if fecha_fin:
        ff = datetime.strptime(fecha_fin, "%Y-%m-%d")
        query = query.filter(Marcaje.fecha_hora <= ff.replace(hour=23, minute=59, second=59))

    marcajes = query.order_by(Marcaje.fecha_hora.desc()).all()

    return [
        {
            "id": m.id,
            "tipo": m.tipo.value if hasattr(m.tipo, 'value') else str(m.tipo),
            "fecha_hora": m.fecha_hora,
            "es_especial": m.es_especial,
            "minutos_tardanza": m.minutos_tardanza,
            "minutos_extra": m.minutos_extra,
            "estado_justificacion": m.estado_justificacion.value if m.estado_justificacion and hasattr(m.estado_justificacion, 'value') else (str(m.estado_justificacion) if m.estado_justificacion else None),
            "comentario_admin": m.comentario_admin,
        }
        for m in marcajes
    ]
