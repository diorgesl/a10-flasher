#!/usr/bin/env bash
# Build manual da imagem do portal para hosts onde o perfil seccomp
# default do Docker 28 não carrega no kernel (errno 524) — o BuildKit
# morre em todo RUN e o builder legado não existe mais na 28.
#
# Reproduz os passos do Dockerfile com `docker run/exec` +
# `docker commit`, tudo com --security-opt seccomp=unconfined.
# Resultado: a mesma imagem, sem BuildKit.
#
# Uso (no servidor, da raiz do projeto):
#   bash deploy/docker/build-unconfined.sh
#   sudo docker compose -f deploy/docker/docker-compose.yml up -d --no-build
#
# O nome da imagem precisa bater com o que o compose espera
# (`docker compose -f deploy/docker/docker-compose.yml config --images`);
# ajuste o primeiro argumento se necessário.
set -euo pipefail

IMAGE="${1:-docker-portal}"
BASE="python:3.13-slim"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
CONTAINER="a10-flasher-build-$$"

cleanup() { sudo docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "== criando container base ($BASE) sem seccomp..."
sudo docker run -d --name "$CONTAINER" \
  --security-opt seccomp=unconfined \
  -w /opt/a10-flasher \
  "$BASE" sleep infinity

echo "== instalando dependências..."
sudo docker cp "$ROOT/requirements-portal.txt" \
  "$CONTAINER:/opt/a10-flasher/requirements-portal.txt"
sudo docker exec "$CONTAINER" \
  pip install --no-cache-dir -r requirements-portal.txt

echo "== copiando código..."
sudo docker cp "$ROOT/a10flash" "$CONTAINER:/opt/a10-flasher/"

echo "== usuário sem privilégios..."
sudo docker exec "$CONTAINER" sh -c \
  'useradd -r -u 10001 a10flash \
   && mkdir -p logs \
   && chown -R a10flash:a10flash /opt/a10-flasher'

echo "== commit da imagem '$IMAGE'..."
sudo docker commit \
  --change 'ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORTAL_HOST=0.0.0.0 PORTAL_PORT=8080' \
  --change 'USER a10flash' \
  --change 'WORKDIR /opt/a10-flasher' \
  --change 'EXPOSE 8080' \
  --change 'CMD ["python", "-m", "a10flash.portal", "--config", "/opt/a10-flasher/config.yaml"]' \
  "$CONTAINER" "$IMAGE"

echo
echo "imagem '$IMAGE' criada. Agora:"
echo "  sudo docker compose -f deploy/docker/docker-compose.yml up -d --no-build"
