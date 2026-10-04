import cv2
import time
from deepface import DeepFace

def test_camara():
    print("Iniciando cámara...")
    cap = cv2.VideoCapture(0)

    if not cap.isOpened():
        print("ERROR: No se pudo abrir la cámara")
        return

    print("Cámara OK. Tienes 3 segundos para ponerte frente a la cámara...")
    time.sleep(3)
    print("Capturando frame...")
    
    ret, frame = cap.read()
    cap.release()

    if not ret:
        print("ERROR: No se pudo capturar imagen")
        return

    print("Frame capturado OK")

    # Guardar imagen de prueba
    cv2.imwrite("test_foto.jpg", frame)
    print("Foto guardada como test_foto.jpg")

    # Intentar detectar rostro
    try:
        resultado = DeepFace.analyze(
            img_path="test_foto.jpg",
            actions=["emotion"],
            enforce_detection=True
        )
        print("✓ Rostro detectado!")
        print(f"  Emoción dominante: {resultado[0]['dominant_emotion']}")
        print("\n¡DeepFace funciona correctamente!")
    except Exception as e:
        print(f"No se detectó rostro o error: {e}")

if __name__ == "__main__":
    test_camara()