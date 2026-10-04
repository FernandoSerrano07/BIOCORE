from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from database.connection import engine, get_db, init_extensions
from database import models
from api.routes import router
from api.admin_routes import router as admin_router
from biometric.detector import precalentar_modelo, construir_indice_faiss
from sqlalchemy.orm import Session
import os

# Desactivar aceleración/mensajes innecesarios de TensorFlow
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"
os.environ["CUDA_VISIBLE_DEVICES"] = "-1"  # Forzar uso exclusivo de CPU en modo liviano

import tensorflow as tf

# Limitar asignación de memoria dinámica
tf.config.set_soft_device_placement(True)

models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="BIOCORE", description="Sistema biométrico de asistencia")

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

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
        print(f"⚠️  Extensiones: {e}")

    # 2. Calentar Facenet
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, precalentar_modelo)

    # 3. Construir índice FAISS con todos los usuarios existentes
    db: Session = next(get_db())
    try:
        usuarios = (
            db.query(models.Usuario)
            .filter(models.Usuario.activo == True, models.Usuario.encoding_facial != None)
            .all()
        )
        await loop.run_in_executor(None, construir_indice_faiss, usuarios)
    finally:
        db.close()


@app.get("/registro", response_class=HTMLResponse)
async def pagina_registro(request: Request):
    return templates.TemplateResponse(request, "registro.html")


@app.get("/", response_class=HTMLResponse)
async def inicio(request: Request):
    return templates.TemplateResponse(request, "marcaje.html")
