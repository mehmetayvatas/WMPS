# --- Base -------------------------------------------------------
ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:latest
FROM ${BUILD_FROM}

# Logların anında görünmesi ve .pyc yazmaması
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:${PATH}"

# --- Sistem paketleri ------------------------------------------
# evdev ve bazı wheel'lar için compile gerekli -> build deps kur, iş bitince sil
RUN apk add --no-cache python3 py3-pip \
 && apk add --no-cache --virtual .build-deps \
        build-base linux-headers python3-dev

# --- Virtualenv -------------------------------------------------
RUN python3 -m venv /opt/venv \
 && python -m pip install --no-cache-dir --upgrade pip setuptools wheel

WORKDIR /opt/wmps

# --- Bağımlılıklar (layer cache için önce requirements) --------
COPY wmps/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Build deps artık gereksiz
RUN apk del .build-deps

# --- Uygulama kodu ----------------------------------------------
COPY wmps/ ./

# --- Entrypoint -------------------------------------------------
COPY run.sh /run.sh
RUN chmod +x /run.sh

EXPOSE 8000
CMD ["/run.sh"]
