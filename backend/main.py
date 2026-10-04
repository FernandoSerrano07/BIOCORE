import os

# 1. Configuración de entorno para optimización de RAM y TensorFlow (Debe ir al inicio)
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"
os.environ["CUDA_VISIBLE_DEVICES"] = "-1"  # Forzar uso exclusivo de CPU en modo liviano

import tensorflow as tf
tf.config.set_soft_device_placement(True)

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from database.connection import engine, get_db, init_extensions
from database import models
from api.routes import router
from api.admin_routes import router as admin_router
from biometric.detector import construir_indice_faiss  # Se omite precalentar_modelo en startup
from sqlalchemy.orm import Session

# Crear tablas en la base de datos si no existen
models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="BIOCORE", description="Sistema biométrico de asistencia")

# Archivos estáticos y plantillas
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Incluir rutas de la API y administración
app.include_router(router, prefix="/api")
app.include_router(admin_router)


@app.on_event("startup")
async def startup_event():
    import asyncio

    # 1. Activar extensiones de PostgreSQL
    try:
        init_extensions(engine)
        print("✅ Extensiones PostgreSQL activadas.")
    except Exception as e:
        print(f"⚠️ Extensiones: {e}")

    # 2. Precalentamiento de Facenet desactivado para ahorrar RAM en el plan Free
    # El modelo se cargará automáticamente de forma liviana con la primera petición biométrica.
    # loop = asyncio.get_event_loop()
    # await loop.run_in_executor(None, precalentar_modelo)

    # 3. Construir índice FAISS ligero con los encodings existentes en la BD
    loop = asyncio.get_event_loop()
    db: Session = next(get_db())
    try:
        usuarios = (
            db.query(models.Usuario)
            .filter(models.Usuario.activo == True, models.Usuario.encoding_facial != None)
            .all()
        )
        if usuarios:
            await loop.run_in_executor(None, construir_indice_faiss, usuarios)
            print("✅ Índice FAISS inicializado correctamente.")
        else:
            print("ℹ️ No hay usuarios registrados con rostros aún.")
    except Exception as e:
        print(f"⚠️ Error al construir índice FAISS: {e}")
    finally:
        db.close()


@app.get("/registro", response_class=HTMLResponse)
async def pagina_registro(request: Request):
    return templates.TemplateResponse(request, "registro.html")


@app.get("/", response_class=HTMLResponse)
async def inicio(request: Request):
    return templates.TemplateResponse(request, "marcaje.html")
