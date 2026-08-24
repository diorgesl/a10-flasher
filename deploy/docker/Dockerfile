# a10-flasher portal — imagem Docker
# Build context: raiz do projeto (../.. a partir de deploy/docker)
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORTAL_HOST=0.0.0.0 \
    PORTAL_PORT=8080

WORKDIR /opt/a10-flasher

COPY requirements-portal.txt .
RUN pip install --no-cache-dir -r requirements-portal.txt

COPY a10flash/ a10flash/

# usuário sem privilégios
RUN useradd -r -u 10001 a10flash \
    && mkdir -p logs \
    && chown -R a10flash:a10flash /opt/a10-flasher
USER a10flash

EXPOSE 8080

# --config aponta para um caminho opcional: se o arquivo não existir no
# container, o portal usa apenas as env vars PORTAL_HOST/PORTAL_PORT/PORTAL_TOKEN
CMD ["python", "-m", "a10flash.portal", "--config", "/opt/a10-flasher/config.yaml"]
