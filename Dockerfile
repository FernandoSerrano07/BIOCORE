# Usar imagen oficial liviana de Python 3.11
FROM python:3.11-slim

# Instalar dependencias del sistema necesarias para OpenCV y compilaciones
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    && rm -rf /var/lib/apt/lists/*

# Crear directorio de trabajo
WORKDIR /code

# Copiar archivo de requerimientos e instalar dependencias
COPY requirements.txt /code/requirements.txt
RUN pip install --no-cache-dir --upgrade -r /code/requirements.txt

# Copiar todo el código de la aplicación
COPY . /code

# Otorgar permisos de escritura para que DeepFace pueda guardar modelos descargados
RUN chmod -R 777 /code

# Exponer el puerto 7860 (puerto por defecto de Hugging Face Spaces)
EXPOSE 7860

# Comando para iniciar FastAPI con Uvicorn en el puerto 7860
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "7860"]
