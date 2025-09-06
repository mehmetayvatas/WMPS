ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:latest
FROM ${BUILD_FROM}

# System packages
RUN apk add --no-cache python3 py3-pip

# Virtual environment
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"
RUN python -m pip install --no-cache-dir --upgrade pip setuptools wheel

WORKDIR /opt/wmps

# 1) Install dependencies first (better layer caching)
COPY wmps/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 2) Copy application code
COPY wmps/ ./

# Entrypoint
COPY run.sh /run.sh
RUN chmod +x /run.sh

EXPOSE 8000
CMD ["/run.sh"]
