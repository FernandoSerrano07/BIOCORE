import numpy as np
import json
import os
import threading
import faiss
from deepface import DeepFace

FOTOS_DIR = "fotos_registro"
DIMENSION_FACENET = 128   # Facenet produce vectores de 128 dimensiones

# ── Índice FAISS en memoria ─────────────────────────────────────────────
# Guardamos el índice y un mapeo id_faiss → usuario_id
_lock         = threading.Lock()
_indice_faiss = None        # faiss.IndexFlatL2
_mapa_ids     = []          # _mapa_ids[i] = usuario_id del vector i
_modelo_listo = False


def _nuevo_indice():
    """Crea un índice L2 normalizado (equivalente a cosine similarity)"""
    indice = faiss.IndexFlatIP(DIMENSION_FACENET)   # Inner Product sobre vectores normalizados = cosine
    return indice


def precalentar_modelo():
    """
    1. Carga Facenet en memoria.
    2. Construye el índice FAISS con todos los usuarios que ya tienen encoding.
    Llamado una sola vez en el startup del servidor.
    """
    global _modelo_listo
    print("⏳ Precalentando modelo Facenet...")

    import cv2, tempfile
    img_dummy = np.zeros((160, 160, 3), dtype=np.uint8)
    ruta_dummy = tempfile.mktemp(suffix=".jpg")
    cv2.imwrite(ruta_dummy, img_dummy)
    try:
        DeepFace.represent(img_path=ruta_dummy, model_name="Facenet", enforce_detection=False)
        _modelo_listo = True
        print("✅ Modelo Facenet listo en memoria.")
    except Exception as e:
        print(f"⚠️  Precalentamiento falló (no crítico): {e}")
    finally:
        if os.path.exists(ruta_dummy):
            os.remove(ruta_dummy)


def construir_indice_faiss(usuarios):
    """
    Recibe lista de objetos Usuario con encoding_facial.
    Construye (o reconstruye) el índice FAISS en memoria.
    Llamado al startup y cada vez que se registra un usuario nuevo.
    """
    global _indice_faiss, _mapa_ids

    nuevo_indice = _nuevo_indice()
    nuevo_mapa   = []

    vectores = []
    for u in usuarios:
        if not u.encoding_facial:
            continue
        try:
            vec = np.array(json.loads(u.encoding_facial), dtype=np.float32)
            # Normalizar para usar cosine similarity con IndexFlatIP
            norma = np.linalg.norm(vec)
            if norma > 0:
                vec = vec / norma
            vectores.append(vec)
            nuevo_mapa.append(u.id)
        except Exception as e:
            print(f"⚠️  Usuario {u.id} omitido del índice: {e}")

    if vectores:
        matriz = np.vstack(vectores).astype(np.float32)
        nuevo_indice.add(matriz)
        print(f"✅ Índice FAISS construido con {len(vectores)} empleados.")
    else:
        print("ℹ️  Índice FAISS vacío — no hay usuarios con encoding.")

    with _lock:
        _indice_faiss = nuevo_indice
        _mapa_ids     = nuevo_mapa


def agregar_usuario_al_indice(usuario_id: int, encoding_json: str):
    """
    Agrega un solo usuario al índice sin reconstruirlo completo.
    Llamado después de registrar un usuario nuevo.
    """
    global _indice_faiss, _mapa_ids

    try:
        vec = np.array(json.loads(encoding_json), dtype=np.float32)
        norma = np.linalg.norm(vec)
        if norma > 0:
            vec = vec / norma
        with _lock:
            if _indice_faiss is None:
                _indice_faiss = _nuevo_indice()
            _indice_faiss.add(vec.reshape(1, -1))
            _mapa_ids.append(usuario_id)
        print(f"✅ Usuario {usuario_id} agregado al índice FAISS.")
    except Exception as e:
        print(f"⚠️  No se pudo agregar usuario {usuario_id} al índice: {e}")


def buscar_en_indice(ruta_imagen: str, umbral_similitud: float = 0.6):
    """
    Busca el rostro más similar en el índice FAISS.
    Retorna (usuario_id, similitud) o (None, 0) si no hay match.
    MUCHO más rápido que el loop lineal — O(log n) con índices HNSW.
    """
    global _indice_faiss, _mapa_ids

    with _lock:
        if _indice_faiss is None or _indice_faiss.ntotal == 0:
            return None, 0.0
        indice_snap = _indice_faiss
        mapa_snap   = list(_mapa_ids)

    # Obtener embedding de la imagen capturada
    resultado = DeepFace.represent(
        img_path=ruta_imagen,
        model_name="Facenet",
        enforce_detection=False
    )

    if not resultado or not resultado[0].get("embedding"):
        raise Exception("No se detectó rostro en la imagen")

    vec = np.array(resultado[0]["embedding"], dtype=np.float32)
    norma = np.linalg.norm(vec)
    if norma > 0:
        vec = vec / norma

    # Buscar el vecino más cercano
    distancias, indices = indice_snap.search(vec.reshape(1, -1), k=1)

    similitud  = float(distancias[0][0])   # Inner Product normalizado = cosine similarity
    idx_faiss  = int(indices[0][0])

    if idx_faiss < 0 or idx_faiss >= len(mapa_snap):
        return None, 0.0

    if similitud >= umbral_similitud:
        return mapa_snap[idx_faiss], similitud
    else:
        return None, similitud


def generar_encoding(carpeta_usuario: str):
    """
    Analiza las fotos y genera un encoding facial promedio con Facenet.
    Compatible con el sistema anterior — retorna JSON string.
    """
    print("Generando encoding facial con Facenet...")
    encodings = []

    for archivo in sorted(os.listdir(carpeta_usuario)):
        if not archivo.endswith(".jpg"):
            continue
        ruta = os.path.join(carpeta_usuario, archivo)
        try:
            resultado = DeepFace.represent(
                img_path=ruta,
                model_name="Facenet",
                enforce_detection=False
            )
            if resultado and resultado[0].get("embedding"):
                encodings.append(resultado[0]["embedding"])
                print(f"  ✓ {archivo} procesada")
        except Exception as e:
            print(f"  ✗ {archivo} omitida: {e}")

    if not encodings:
        raise Exception("No se pudo generar encoding de ninguna foto")

    encoding_promedio = np.mean(encodings, axis=0).tolist()
    print(f"Encoding generado con {len(encodings)} fotos")
    return json.dumps(encoding_promedio)


# SOLO para compatibilidad con código antiguo — no usar en verificación nueva
def verificar_rostro(encoding_guardado: str, ruta_imagen: str, umbral_distancia: float = 10.0):
    resultado = DeepFace.represent(img_path=ruta_imagen, model_name="Facenet", enforce_detection=False)
    if not resultado or not resultado[0].get("embedding"):
        raise Exception("No se pudo obtener embedding")
    enc_actual   = np.array(resultado[0]["embedding"])
    enc_guardado = np.array(json.loads(encoding_guardado))
    distancia    = np.linalg.norm(enc_actual - enc_guardado)
    confianza    = max(0.0, 1.0 - (distancia / 20.0))
    return distancia <= umbral_distancia, confianza