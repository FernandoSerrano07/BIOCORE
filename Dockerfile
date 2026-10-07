# 1. La imagen base SIEMPRE debe ir primero
FROM python:3.11-slim

# 2. Instalar paquetes de sistema para OpenCV
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    && rm -rf /var/lib/apt/lists/*

# 3. Directorio de trabajo
WORKDIR /code

# 4. Copiar e instalar requerimientos
COPY requirements.txt /code/requirements.txt
RUN pip install --no-cache-dir --upgrade -r /code/requirements.txt

# 5. Copiar el resto del proyecto
COPY . /code

# 6. Configurar PYTHONPATH para que Python encuentre la carpeta backend y database
ENV PYTHONPATH=/code/backend:/code

# 7. Exponer el puerto
EXPOSE 8000

# 8. Comando de arranque (debe ir al final de todo)
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
