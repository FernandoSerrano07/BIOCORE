# crear_admin.py — ejecutar UNA SOLA VEZ desde la carpeta backend/, luego borrar
# Uso: python crear_admin.py
import sys
import os
from dotenv import load_dotenv

load_dotenv()

from database.connection import SessionLocal
from database.models import Administrador
from passlib.context import CryptContext

pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

nombre   = input("BIOCORE: ").strip()
email    = input("Email del admin: serranosystems1@gamil.com ").strip()
password = input("Contraseña: admin ").strip()
superadmin = input("¿Es superadmin? (s/n): si" \
" ").strip().lower() == "s"

if not nombre or not email or not password:
    print("❌ Todos los campos son obligatorios.")
    sys.exit(1)


db = SessionLocal()

existe = db.query(Administrador).filter(Administrador.email == email).first()
if existe:
    print(f"❌ Ya existe un admin con el email {email}")
    db.close()
    sys.exit(1)

admin = Administrador(
    nombre        = nombre,
    email         = email,
    password_hash = pwd.hash(password),
    es_superadmin = superadmin,
)
db.add(admin)
db.commit()

print(f"✅ Admin creado correctamente:")
print(f"   Nombre: {admin.nombre}")
print(f"   Email:  {admin.email}")
print(f"   Rol:    {'Superadmin' if admin.es_superadmin else 'Administrador'}")
print()
print("⚠️  Borra este archivo ahora que ya lo usaste.")
db.close()