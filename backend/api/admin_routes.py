import csv
from datetime import date, datetime, timedelta
import io
import json
import os
import time
from collections import defaultdict
from pathlib import Path

from database.connection import get_db
from database.models import (
    Administrador,
    EstadoJustificacion,
    LogAuditoria,
    Marcaje,
    Sucursal,
    TipoAccion,
    TipoMarcaje,
    Usuario,
    administrador_sucursales,
)
from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from jose import JWTError, jwt
import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from passlib.context import CryptContext
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import func as sqlfunc, text
from sqlalchemy.orm import Session

# Importación del motor de cálculo avanzado
from calculo_asistencia import calcular_resumen_diario

router = APIRouter()

# ── Configuración de ruta absoluta para plantillas ───────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent  # Apunta a /code/backend
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# ══════════════════════════════════════════
# CONFIGURACIÓN AUTH
# ══════════════════════════════════════════

SECRET_KEY = os.getenv("SECRET_KEY", "cambia-esto-urgente")
ALGORITHM = "HS256"
ACCESS_TOKEN_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", 480))
SUPERADMIN_EMAIL = os.getenv("SUPERADMIN_EMAIL", "soporte@tuempresa.com")
SUPERADMIN_PASSWORD = os.getenv("SUPERADMIN_PASSWORD", "cambia-esto")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ── Rate limiting en memoria (login) ───────────────────────────────────
_login_intentos: dict[str, list] = defaultdict(list)
MAX_INTENTOS_LOGIN = 10
VENTANA_LOGIN_SEG = 300  # 5 minutos


def _ip(request: Request) -> str:
    """Extrae la IP real considerando proxies."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "127.0.0.1"


def _check_rate_limit_login(ip: str):
    """Lanza 429 si la IP superó el límite de intentos de login."""
    ahora = time.time()
    recientes = [t for t in _login_intentos[ip] if ahora - t < VENTANA_LOGIN_SEG]
    _login_intentos[ip] = recientes
    if len(recientes) >= MAX_INTENTOS_LOGIN:
        raise HTTPException(
            status_code=429,
            detail=f"Demasiados intentos. Espera {VENTANA_LOGIN_SEG // 60} minutos.",
        )
    _login_intentos[ip].append(ahora)


# ══════════════════════════════════════════
# HELPERS AUTH & ALCANCE MULTI-SUCURSAL
# ══════════════════════════════════════════

def verificar_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def hashear_password(plain: str) -> str:
    return pwd_context.hash(plain)


def crear_token(data: dict, minutos: int = ACCESS_TOKEN_MINUTES) -> str:
    payload = data.copy()
    payload["exp"] = datetime.utcnow() + timedelta(minutes=minutos)
    payload["iat"] = datetime.utcnow()
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def sucursales_visibles(admin: dict, db: Session):
    """None = todas las sucursales (Superadmin). Lista de IDs = solo asignadas."""
    if admin.get("es_superadmin"):
        return None
    filas = db.execute(
        text("SELECT sucursal_id FROM administrador_sucursales WHERE admin_id = :i"),
        {"i": admin["id"]},
    ).all()
    return [f[0] for f in filas]


def obtener_admin_actual(request: Request, db: Session = Depends(get_db)) -> dict:
    """Lee el JWT de la cookie, lo valida y retorna el dict del admin con sus permisos."""
    token = request.cookies.get("biocore_token")
    if not token:
        raise HTTPException(status_code=401, detail="No autenticado")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email = payload.get("sub")
        es_super = payload.get("superadmin", False)
    except JWTError as e:
        raise HTTPException(status_code=401, detail=f"Sesión inválida o expirada: {e}")

    if es_super:
        sucursales_objs = db.query(Sucursal).filter(Sucursal.activa == True).all()
        return {
            "id": 0,
            "nombre": "Soporte BioCore",
            "email": email,
            "es_superadmin": True,
            "sucursales_ids": [s.id for s in sucursales_objs],
        }

    admin = db.query(Administrador).filter(Administrador.email == email).first()
    if not admin:
        raise HTTPException(status_code=401, detail="Administrador no encontrado")

    visibles = sucursales_visibles({"id": admin.id, "es_superadmin": False}, db)

    return {
        "id": admin.id,
        "nombre": admin.nombre,
        "email": admin.email,
        "es_superadmin": getattr(admin, "es_superadmin", False),
        "sucursales_ids": visibles,
    }


def requiere_superadmin(admin=Depends(obtener_admin_actual)) -> dict:
    """Middleware/Dependencia para restringir rutas a sólo superadministradores."""
    if not admin.get("es_superadmin"):
        raise HTTPException(status_code=403, detail="Solo superadministradores")
    return admin


def exigir_acceso_usuario(db: Session, admin: dict, usuario_id: int) -> Usuario:
    """Retorna el usuario si el admin tiene acceso a su sucursal, o lanza 404."""
    usuario = db.query(Usuario).filter(Usuario.id == usuario_id).first()
    if not usuario:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    if not admin.get("es_superadmin"):
        if usuario.sucursal_id not in admin.get("sucursales_ids", []):
            raise HTTPException(status_code=404, detail="Usuario no encontrado")
    return usuario


def exigir_acceso_marcaje(db: Session, admin: dict, marcaje_id: int) -> Marcaje:
    """Retorna el marcaje si el admin tiene acceso a su sucursal, o lanza 404."""
    marcaje = db.query(Marcaje).filter(Marcaje.id == marcaje_id).first()
    if not marcaje:
        raise HTTPException(status_code=404, detail="Marcaje no encontrado")
    if not admin.get("es_superadmin"):
        if marcaje.sucursal_id not in admin.get("sucursales_ids", []):
            raise HTTPException(status_code=404, detail="Marcaje no encontrado")
    return marcaje


def _obtener_lista_sucursales_admin(admin: dict, db: Session):
    """Devuelve objetos Sucursal que el admin puede seleccionar en el frontend."""
    if admin.get("es_superadmin"):
        return db.query(Sucursal).order_by(Sucursal.nombre).all()
    visibles = admin.get("sucursales_ids", [])
    if not visibles:
        return []
    return db.query(Sucursal).filter(Sucursal.id.in_(visibles)).order_by(Sucursal.nombre).all()


# ══════════════════════════════════════════
# HELPER AUDITORÍA
# ══════════════════════════════════════════

def _log(
    db: Session,
    admin: dict,
    accion: TipoAccion,
    detalle: dict = None,
    ip: str = None,
):
    """Registra una acción de admin en logs_auditoria pasando el JSON directo."""
    entrada = LogAuditoria(
        admin_id=admin["id"] if admin["id"] != 0 else None,
        admin_email=admin["email"],
        accion=accion,
        detalle=detalle,
        ip=ip,
    )
    db.add(entrada)
    db.commit()


# ══════════════════════════════════════════
# LOGIN / LOGOUT
# ══════════════════════════════════════════

@router.get("/admin/login", response_class=HTMLResponse)
@router.get("/admin_login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "admin_login.html")


@router.post("/admin/login")
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    ip = _ip(request)
    _check_rate_limit_login(ip)

    if email == SUPERADMIN_EMAIL and password == SUPERADMIN_PASSWORD:
        token = crear_token({"sub": email, "superadmin": True})
        resp = RedirectResponse(url="/admin/dashboard", status_code=302)
        resp.set_cookie(
            "biocore_token",
            token,
            httponly=True,
            samesite="lax",
            max_age=ACCESS_TOKEN_MINUTES * 60,
        )
        _log(
            db,
            {"id": 0, "email": email, "es_superadmin": True},
            TipoAccion.LOGIN_ADMIN,
            {"via": "superadmin"},
            ip,
        )
        return resp

    admin = db.query(Administrador).filter(Administrador.email == email).first()
    if not admin or not verificar_password(password, admin.password_hash):
        return templates.TemplateResponse(
            request, "admin_login.html", {"error": "Credenciales incorrectas"}
        )

    if hasattr(admin, "ultimo_login"):
        admin.ultimo_login = datetime.utcnow()
        db.commit()

    es_superadmin_flag = getattr(admin, "es_superadmin", False)
    token = crear_token({"sub": admin.email, "superadmin": es_superadmin_flag})
    resp = RedirectResponse(url="/admin/dashboard", status_code=302)
    resp.set_cookie(
        "biocore_token",
        token,
        httponly=True,
        samesite="lax",
        max_age=ACCESS_TOKEN_MINUTES * 60,
    )
    _log(
        db,
        {"id": admin.id, "email": admin.email, "es_superadmin": es_superadmin_flag},
        TipoAccion.LOGIN_ADMIN,
        {"via": "admin_normal"},
        ip,
    )
    return resp


@router.get("/admin/logout")
async def logout(
    request: Request,
    db: Session = Depends(get_db),
    admin=Depends(obtener_admin_actual),
):
    _log(db, admin, TipoAccion.LOGOUT_ADMIN, ip=_ip(request))
    resp = RedirectResponse(url="/admin/login", status_code=302)
    resp.delete_cookie("biocore_token")
    return resp


# ══════════════════════════════════════════
# DASHBOARD
# ══════════════════════════════════════════

@router.get("/admin/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    sucursal_id: int = None,
    db: Session = Depends(get_db),
    admin=Depends(obtener_admin_actual),
):
    hoy = date.today()
    sucursales_disponibles = _obtener_lista_sucursales_admin(admin, db)
    sucursales_ids = [s.id for s in sucursales_disponibles]

    # Filtrado por selector de sucursal
    if sucursal_id:
        if sucursal_id not in sucursales_ids:
            raise HTTPException(status_code=403, detail="Sin acceso a esta sucursal")
        filtro_sucursales = [sucursal_id]
    else:
        filtro_sucursales = sucursales_ids

    query_usuarios = db.query(Usuario).filter(Usuario.activo == True)
    query_marcajes = db.query(Marcaje).filter(sqlfunc.date(Marcaje.fecha_hora) == hoy)
    query_justif = db.query(Marcaje).filter(
        Marcaje.estado_justificacion == EstadoJustificacion.PENDIENTE,
        Marcaje.es_especial == True,
    )
    query_entradas = db.query(Marcaje).filter(
        sqlfunc.date(Marcaje.fecha_hora) == hoy,
        Marcaje.tipo == TipoMarcaje.ENTRADA,
    )
    query_tardanzas = db.query(sqlfunc.count(Marcaje.id)).filter(
        sqlfunc.date(Marcaje.fecha_hora) == hoy,
        Marcaje.tipo == TipoMarcaje.ENTRADA,
        Marcaje.minutos_tardanza > 0,
    )

    if filtro_sucursales:
        query_usuarios = query_usuarios.filter(Usuario.sucursal_id.in_(filtro_sucursales))
        query_marcajes = query_marcajes.filter(Marcaje.sucursal_id.in_(filtro_sucursales))
        query_justif = query_justif.filter(Marcaje.sucursal_id.in_(filtro_sucursales))
        query_entradas = query_entradas.filter(Marcaje.sucursal_id.in_(filtro_sucursales))
        query_tardanzas = query_tardanzas.filter(Marcaje.sucursal_id.in_(filtro_sucursales))
    elif not admin.get("es_superadmin"):
        # Admin sin sucursales asignadas
        return templates.TemplateResponse(
            request,
            "admin_dashboard.html",
            {
                "admin": admin,
                "total_usuarios": 0,
                "marcajes_hoy": 0,
                "entradas_hoy": 0,
                "justif_pendientes": 0,
                "tardanzas_hoy": 0,
                "hoy": hoy.strftime("%d/%m/%Y"),
                "sucursales": [],
                "sucursal_sel": None,
            },
        )

    total_usuarios = query_usuarios.count()
    marcajes_hoy = query_marcajes.count()
    justif_pendientes = query_justif.count()
    entradas_hoy = query_entradas.count()
    tardanzas_hoy = query_tardanzas.scalar() or 0

    return templates.TemplateResponse(
        request,
        "admin_dashboard.html",
        {
            "admin": admin,
            "total_usuarios": total_usuarios,
            "marcajes_hoy": marcajes_hoy,
            "entradas_hoy": entradas_hoy,
            "justif_pendientes": justif_pendientes,
            "tardanzas_hoy": tardanzas_hoy,
            "hoy": hoy.strftime("%d/%m/%Y"),
            "sucursales": sucursales_disponibles,
            "sucursal_sel": sucursal_id,
        },
    )


# ══════════════════════════════════════════
# GESTIÓN DE SUCURSALES
# ══════════════════════════════════════════

@router.get("/admin/sucursales", response_class=HTMLResponse)
async def sucursales_page(
    request: Request,
    admin=Depends(requiere_superadmin),
    db: Session = Depends(get_db),
):
    sucursales = db.query(Sucursal).order_by(Sucursal.id).all()
    return templates.TemplateResponse(
        request, "admin_sucursales.html", {"admin": admin, "sucursales": sucursales}
    )


@router.post("/admin/sucursales/crear")
async def crear_sucursal(
    request: Request,
    nombre: str = Form(...),
    codigo: str = Form(...),
    direccion: str = Form(None),
    zona_horaria: str = Form("America/El_Salvador"),
    admin=Depends(requiere_superadmin),
    db: Session = Depends(get_db),
):
    codigo_clean = codigo.strip().upper()
    existe = db.query(Sucursal).filter(Sucursal.codigo == codigo_clean).first()
    if existe:
        raise HTTPException(400, "El código de sucursal ya existe")

    s = Sucursal(
        nombre=nombre.strip(),
        codigo=codigo_clean,
        direccion=direccion.strip() if direccion else None,
        zona_horaria=zona_horaria.strip(),
    )
    db.add(s)
    db.commit()

    _log(
        db,
        admin,
        TipoAccion.CREAR_SUCURSAL,
        {"sucursal_id": s.id, "codigo": codigo_clean, "nombre": s.nombre},
        _ip(request),
    )
    return {"ok": True, "id": s.id}


@router.put("/admin/sucursales/{sucursal_id}/editar")
async def editar_sucursal(
    sucursal_id: int,
    request: Request,
    nombre: str = Form(...),
    direccion: str = Form(None),
    zona_horaria: str = Form("America/El_Salvador"),
    admin=Depends(requiere_superadmin),
    db: Session = Depends(get_db),
):
    s = db.query(Sucursal).filter(Sucursal.id == sucursal_id).first()
    if not s:
        raise HTTPException(404, "Sucursal no encontrada")

    s.nombre = nombre.strip()
    s.direccion = direccion.strip() if direccion else None
    s.zona_horaria = zona_horaria.strip()
    db.commit()

    _log(
        db,
        admin,
        TipoAccion.EDITAR_SUCURSAL,
        {"sucursal_id": sucursal_id, "nombre": s.nombre},
        _ip(request),
    )
    return {"ok": True}


@router.put("/admin/sucursales/{sucursal_id}/estado")
async def cambiar_estado_sucursal(
    sucursal_id: int,
    request: Request,
    activa: bool = Form(...),
    admin=Depends(requiere_superadmin),
    db: Session = Depends(get_db),
):
    s = db.query(Sucursal).filter(Sucursal.id == sucursal_id).first()
    if not s:
        raise HTTPException(404, "Sucursal no encontrada")

    s.activa = activa
    db.commit()

    accion = TipoAccion.ACTIVAR_SUCURSAL if activa else TipoAccion.DESACTIVAR_SUCURSAL
    _log(db, admin, accion, {"sucursal_id": sucursal_id, "nombre": s.nombre}, _ip(request))

    return {"ok": True}


# ══════════════════════════════════════════
# USUARIOS (CRUD)
# ══════════════════════════════════════════

@router.get("/admin/usuarios", response_class=HTMLResponse)
async def lista_usuarios(
    request: Request,
    sucursal_id: int = None,
    db: Session = Depends(get_db),
    admin=Depends(obtener_admin_actual),
):
    sucursales_disponibles = _obtener_lista_sucursales_admin(admin, db)
    sucursales_ids = [s.id for s in sucursales_disponibles]

    query = db.query(Usuario)
    if sucursal_id:
        if sucursal_id not in sucursales_ids:
            raise HTTPException(status_code=403, detail="Sin acceso a esta sucursal")
        query = query.filter(Usuario.sucursal_id == sucursal_id)
    elif sucursales_ids:
        query = query.filter(Usuario.sucursal_id.in_(sucursales_ids))
    elif not admin.get("es_superadmin"):
        query = query.filter(False)

    usuarios = query.order_by(Usuario.nombre).all()

    return templates.TemplateResponse(
        request,
        "admin_usuarios.html",
        {
            "admin": admin,
            "usuarios": usuarios,
            "sucursales": sucursales_disponibles,
            "sucursal_sel": sucursal_id,
        },
    )


@router.post("/admin/usuarios/crear")
async def crear_admin_usuario(
    request: Request,
    nombre: str = Form(...),
    apellido: str = Form(...),
    cargo: str = Form(...),
    departamento: str = Form(None),
    email: str = Form(...),
    sucursal_id: int = Form(None),
    turno_entrada: str = Form(None),
    turno_salida: str = Form(None),
    minutos_almuerzo: int = Form(60),
    horas_laborales_dia: int = Form(8),
    db: Session = Depends(get_db),
    admin=Depends(obtener_admin_actual),
):
    if sucursal_id and not admin.get("es_superadmin") and sucursal_id not in admin.get("sucursales_ids", []):
        raise HTTPException(403, "No tiene permisos para asignar usuarios a esta sucursal")

    existe = db.query(Usuario).filter(Usuario.email == email).first()
    if existe:
        raise HTTPException(400, "Email ya registrado")

    t_entrada = datetime.strptime(turno_entrada, "%H:%M").time() if turno_entrada else None
    t_salida = datetime.strptime(turno_salida, "%H:%M").time() if turno_salida else None

    u = Usuario(
        nombre=nombre,
        apellido=apellido,
        cargo=cargo,
        departamento=departamento,
        email=email,
        sucursal_id=sucursal_id,
        turno_entrada=t_entrada,
        turno_salida=t_salida,
        minutos_almuerzo=minutos_almuerzo,
        horas_laborales_dia=horas_laborales_dia,
    )
    db.add(u)
    db.commit()

    _log(
        db,
        admin,
        TipoAccion.CREAR_USUARIO,
        {"usuario_id": u.id, "email": email, "cargo": cargo, "sucursal_id": sucursal_id},
        _ip(request),
    )

    return {"ok": True, "id": u.id}


@router.put("/admin/usuarios/{uid}/horario")
async def actualizar_horario(
    uid: int,
    request: Request,
    turno_entrada: str = Form(None),
    turno_salida: str = Form(None),
    minutos_almuerzo: int = Form(60),
    horas_laborales_dia: int = Form(8),
    db: Session = Depends(get_db),
    admin=Depends(obtener_admin_actual),
):
    u = exigir_acceso_usuario(db, admin, uid)

    u.turno_entrada = datetime.strptime(turno_entrada, "%H:%M").time() if turno_entrada else None
    u.turno_salida = datetime.strptime(turno_salida, "%H:%M").time() if turno_salida else None
    u.minutos_almuerzo = minutos_almuerzo
    u.horas_laborales_dia = horas_laborales_dia
    db.commit()

    _log(
        db,
        admin,
        TipoAccion.ACTUALIZAR_HORARIO,
        {
            "usuario_id": uid,
            "turno_entrada": turno_entrada,
            "turno_salida": turno_salida,
        },
        _ip(request),
    )

    return {"ok": True}


@router.put("/admin/usuarios/{uid}/desactivar")
async def desactivar_usuario(
    uid: int,
    request: Request,
    db: Session = Depends(get_db),
    admin=Depends(obtener_admin_actual),
):
    u = exigir_acceso_usuario(db, admin, uid)
    u.activo = False
    db.commit()

    _log(
        db,
        admin,
        TipoAccion.DESACTIVAR_USUARIO,
        {"usuario_id": uid, "email": u.email},
        _ip(request),
    )

    return {"ok": True}


# ══════════════════════════════════════════
# ADMINS (solo superadmin)
# ══════════════════════════════════════════

@router.get("/admin/admins", response_class=HTMLResponse)
@router.get("/admins", response_class=HTMLResponse)
async def admins_page(
    request: Request,
    admin=Depends(requiere_superadmin),
    db: Session = Depends(get_db),
):
    admins = db.query(Administrador).order_by(Administrador.id).all()
    sucursales = db.query(Sucursal).filter(Sucursal.activa == True).all()
    return templates.TemplateResponse(
        request, "admin_admins.html", {"admin": admin, "admins": admins, "sucursales": sucursales}
    )


@router.post("/admin/admins/crear")
@router.post("/admins/crear")
async def admins_crear(
    request: Request,
    nombre: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    es_superadmin: str = Form("false"),
    sucursales_ids: list[int] = Form([]),
    admin=Depends(requiere_superadmin),
    db: Session = Depends(get_db),
):
    email_clean = email.strip().lower()
    if len(password) < 8:
        raise HTTPException(400, "La contraseña debe tener al menos 8 caracteres")

    existe = db.query(Administrador).filter(sqlfunc.lower(Administrador.email) == email_clean).first()
    if existe:
        raise HTTPException(409, "Ya existe un administrador con ese email")

    es_super_bool = es_superadmin.lower() == "true"
    a = Administrador(
        nombre=nombre.strip(),
        email=email_clean,
        password_hash=hashear_password(password),
    )
    if hasattr(a, "es_superadmin"):
        a.es_superadmin = es_super_bool

    if sucursales_ids and hasattr(a, "sucursales"):
        sucursales_obj = db.query(Sucursal).filter(Sucursal.id.in_(sucursales_ids)).all()
        a.sucursales = sucursales_obj

    db.add(a)
    db.commit()

    _log(
        db,
        admin,
        TipoAccion.CREAR_ADMINISTRADOR,
        {"email": email_clean, "es_superadmin": es_super_bool, "sucursales_assigned": sucursales_ids},
        _ip(request),
    )
    return {"ok": True}


@router.put("/admin/admins/{admin_id}/sucursales")
async def admins_reasignar_sucursales(
    admin_id: int,
    request: Request,
    sucursales_ids: list[int] = Form([]),
    admin=Depends(requiere_superadmin),
    db: Session = Depends(get_db),
):
    target_admin = db.query(Administrador).filter(Administrador.id == admin_id).first()
    if not target_admin:
        raise HTTPException(404, "Administrador no encontrado")

    sucursales_objs = db.query(Sucursal).filter(Sucursal.id.in_(sucursales_ids)).all()
    target_admin.sucursales = sucursales_objs
    db.commit()

    _log(
        db,
        admin,
        TipoAccion.ASIGNAR_SUCURSALES_ADMIN,
        {"target_admin_id": admin_id, "sucursales_assigned": sucursales_ids},
        _ip(request),
    )
    return {"ok": True}


@router.put("/admin/admins/{admin_id}/password")
@router.put("/admins/{admin_id}/password")
async def admins_password(
    admin_id: int,
    request: Request,
    password: str = Form(...),
    admin=Depends(requiere_superadmin),
    db: Session = Depends(get_db),
):
    if len(password) < 8:
        raise HTTPException(400, "La contraseña debe tener al menos 8 caracteres")

    target_admin = db.query(Administrador).filter(Administrador.id == admin_id).first()
    if not target_admin:
        raise HTTPException(404, "Administrador no encontrado")

    target_admin.password_hash = hashear_password(password)
    db.commit()

    _log(
        db,
        admin,
        TipoAccion.CAMBIAR_PASSWORD_ADMIN,
        {"target_admin_id": admin_id, "email": target_admin.email},
        _ip(request),
    )
    return {"ok": True}


@router.delete("/admin/admins/{admin_id}")
@router.delete("/admins/{admin_id}")
async def admins_eliminar(
    admin_id: int,
    request: Request,
    admin=Depends(requiere_superadmin),
    db: Session = Depends(get_db),
):
    if admin_id == admin["id"]:
        raise HTTPException(400, "No puedes eliminar tu propia cuenta")

    target_admin = db.query(Administrador).filter(Administrador.id == admin_id).first()
    if not target_admin:
        raise HTTPException(404, "Administrador no encontrado")

    email_borrado = target_admin.email
    db.delete(target_admin)
    db.commit()

    _log(
        db,
        admin,
        TipoAccion.ELIMINAR_ADMIN,
        {"deleted_admin_id": admin_id, "email": email_borrado},
        _ip(request),
    )
    return {"ok": True}


# ══════════════════════════════════════════
# LOGS DE AUDITORÍA (solo superadmin)
# ══════════════════════════════════════════

@router.get("/admin/auditoria", response_class=HTMLResponse)
async def auditoria_page(
    request: Request,
    admin=Depends(requiere_superadmin),
):
    return templates.TemplateResponse(
        request, "admin_auditoria.html", {"admin": admin}
    )


@router.get("/admin/auditoria/data")
async def ver_auditoria(
    pagina: int = 1,
    por_pagina: int = 50,
    db: Session = Depends(get_db),
    admin=Depends(requiere_superadmin),
):
    offset = (pagina - 1) * por_pagina
    total = db.query(LogAuditoria).count()
    logs = (
        db.query(LogAuditoria)
        .order_by(LogAuditoria.fecha_hora.desc())
        .offset(offset)
        .limit(por_pagina)
        .all()
    )

    return {
        "total": total,
        "pagina": pagina,
        "logs": [
            {
                "id": l.id,
                "admin_email": l.admin_email,
                "accion": l.accion.value if hasattr(l.accion, "value") else str(l.accion),
                "detalle": l.detalle,
                "ip": l.ip,
                "fecha_hora": l.fecha_hora,
            }
            for l in logs
        ],
    }


# ══════════════════════════════════════════
# JUSTIFICACIONES
# ══════════════════════════════════════════

@router.get("/admin/justificaciones", response_class=HTMLResponse)
async def justificaciones(
    request: Request,
    sucursal_id: int = None,
    db: Session = Depends(get_db),
    admin=Depends(obtener_admin_actual),
):
    sucursales_disponibles = _obtener_lista_sucursales_admin(admin, db)
    sucursales_ids = [s.id for s in sucursales_disponibles]

    query = (
        db.query(Marcaje, Usuario)
        .join(Usuario, Marcaje.usuario_id == Usuario.id)
        .filter(
            Marcaje.es_especial == True,
            Marcaje.estado_justificacion == EstadoJustificacion.PENDIENTE,
        )
    )

    if sucursal_id:
        if sucursal_id not in sucursales_ids:
            raise HTTPException(status_code=403, detail="Sin acceso a esta sucursal")
        query = query.filter(Marcaje.sucursal_id == sucursal_id)
    elif sucursales_ids:
        query = query.filter(Marcaje.sucursal_id.in_(sucursales_ids))
    elif not admin.get("es_superadmin"):
        query = query.filter(False)

    pendientes = query.order_by(Marcaje.fecha_hora.desc()).all()

    return templates.TemplateResponse(
        request,
        "admin_justificaciones.html",
        {
            "admin": admin,
            "pendientes": pendientes,
            "sucursales": sucursales_disponibles,
            "sucursal_sel": sucursal_id,
        },
    )


@router.post("/admin/justificaciones/{marcaje_id}/resolver")
async def resolver_justificacion(
    marcaje_id: int,
    request: Request,
    estado: str = Form(...),
    comentario: str = Form(None),
    db: Session = Depends(get_db),
    admin=Depends(obtener_admin_actual),
):
    m = exigir_acceso_marcaje(db, admin, marcaje_id)

    m.estado_justificacion = EstadoJustificacion(estado)
    m.comentario_admin = comentario
    db.commit()

    _log(
        db,
        admin,
        TipoAccion.RESOLVER_JUSTIFICACION,
        {
            "marcaje_id": marcaje_id,
            "estado": estado,
            "usuario_id": m.usuario_id,
        },
        _ip(request),
    )

    return {"ok": True}


# ══════════════════════════════════════════
# REPORTES
# ══════════════════════════════════════════

@router.get("/admin/reportes", response_class=HTMLResponse)
async def reportes(
    request: Request,
    sucursal_id: int = None,
    db: Session = Depends(get_db),
    admin=Depends(obtener_admin_actual),
):
    sucursales_disponibles = _obtener_lista_sucursales_admin(admin, db)
    sucursales_ids = [s.id for s in sucursales_disponibles]

    query = db.query(Usuario).filter(Usuario.activo == True)
    if sucursal_id:
        if sucursal_id not in sucursales_ids:
            raise HTTPException(status_code=403, detail="Sin acceso a esta sucursal")
        query = query.filter(Usuario.sucursal_id == sucursal_id)
    elif sucursales_ids:
        query = query.filter(Usuario.sucursal_id.in_(sucursales_ids))
    elif not admin.get("es_superadmin"):
        query = query.filter(False)

    usuarios = query.order_by(Usuario.nombre).all()
    return templates.TemplateResponse(
        request,
        "admin_reportes.html",
        {
            "admin": admin,
            "usuarios": usuarios,
            "sucursales": sucursales_disponibles,
            "sucursal_sel": sucursal_id,
        },
    )


def _datos_reporte(
    usuario_id: int, fecha_inicio: date, fecha_fin: date, db: Session, admin: dict
):
    usuario = exigir_acceso_usuario(db, admin, usuario_id)
    tz_str = usuario.sucursal.zona_horaria if usuario.sucursal else "America/El_Salvador"

    marcajes = (
        db.query(Marcaje)
        .filter(
            Marcaje.usuario_id == usuario_id,
            sqlfunc.date(Marcaje.fecha_hora) >= fecha_inicio,
            sqlfunc.date(Marcaje.fecha_hora) <= fecha_fin,
        )
        .order_by(Marcaje.fecha_hora.asc())
        .all()
    )

    # Agrupar marcajes por día
    dias = defaultdict(list)
    for m in marcajes:
        dias[m.fecha_hora.date()].append(m)

    filas = []
    total_normales = 0.0
    total_extras = 0.0
    total_tardanza = 0

    for dia in sorted(dias.keys()):
        m_lista = dias[dia]
        resumen = calcular_resumen_diario(usuario, m_lista, tz_str)

        total_normales += resumen["horas_normales"]
        total_extras += resumen["horas_extra"]
        total_tardanza += resumen["minutos_tardanza"]

        filas.append({
            "fecha": dia.strftime("%d/%m/%Y"),
            "entrada": resumen["entrada_real"] or "--:--:--",
            "salida": resumen["salida_real"] or "--:--:--",
            "horas_normales": resumen["horas_normales"],
            "horas_extra": resumen["horas_extra"],
            "minutos_tardanza": resumen["minutos_tardanza"],
            "estado": resumen["estado"]
        })

    return {
        "usuario": usuario,
        "filas": filas,
        "totales": {
            "horas_normales": round(total_normales, 2),
            "horas_extra": round(total_extras, 2),
            "total_horas": round(total_normales + total_extras, 2),
            "minutos_tardanza": total_tardanza,
        },
    }


@router.get("/admin/reportes/json")
async def reporte_json(
    usuario_id: int,
    fecha_inicio: date,
    fecha_fin: date,
    db: Session = Depends(get_db),
    admin=Depends(obtener_admin_actual),
):
    datos = _datos_reporte(usuario_id, fecha_inicio, fecha_fin, db, admin)
    return {
        "usuario": f"{datos['usuario'].nombre} {datos['usuario'].apellido}",
        "cargo": datos['usuario'].cargo,
        "sucursal": datos['usuario'].sucursal.nombre if datos['usuario'].sucursal else "Sin sucursal",
        "rango": f"{fecha_inicio.strftime('%d/%m/%Y')} - {fecha_fin.strftime('%d/%m/%Y')}",
        "detalles": datos["filas"],
        "totales": datos["totales"],
    }
