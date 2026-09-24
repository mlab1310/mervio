# Image conteneur de Mervio (Mission 004.3.8). Voir docs/CONTAINER.md.
#
# Une seule image pour les trois usages, qui appellent tous les points d'entree EXISTANTS:
#   worker      mervio worker                          (CMD par defaut)
#   admin       mervio admin ...                       (docker compose run admin ...)
#   migrations  python -m mervio.persistence upgrade   (etape explicite, jamais au demarrage)
#
# Reproductibilite: image Python epinglee par digest (meme version que la CI), dependances
# depuis requirements-runtime.lock (sous-ensemble exact de requirements.lock), wheels
# uniquement, setuptools de construction epingle (requirements-build.lock).
# Securite: utilisateur non-root numerique, aucun secret, aucun port, aucun outil de test,
# pas de pip dans l'environnement d'execution. La connexion vient de l'environnement
# (MERVIO_DATABASE_URL ou MERVIO_DATABASE_URL_FILE) au lancement, jamais du build.

ARG PYTHON_IMAGE=python:3.11.16-slim-bookworm@sha256:528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84

# --- construction ------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# Environnement d'execution SANS pip: pip de l'image de base l'alimente via --python.
COPY requirements-build.lock requirements-runtime.lock ./
RUN python -m venv --without-pip /opt/venv \
    && python -m pip install --no-deps --only-binary=:all: -r requirements-build.lock \
    && python -m pip --python /opt/venv/bin/python install --no-deps --only-binary=:all: \
        -r requirements-runtime.lock

# Le paquet seul (src/ et pyproject.toml): ni tests, ni scripts, ni donnees.
COPY pyproject.toml ./
COPY src ./src
RUN python -m pip wheel --no-deps --no-build-isolation --wheel-dir /build/dist . \
    && python -m pip --python /opt/venv/bin/python install --no-deps --no-index /build/dist/mervio-*.whl \
    && python -m pip --python /opt/venv/bin/python check \
    && /opt/venv/bin/python -m compileall -q /opt/venv/lib

# --- execution ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

LABEL org.opencontainers.image.title="mervio" \
      org.opencontainers.image.description="Mervio worker and operator CLI (development and CI image)"

# Utilisateur systeme sans shell ni mot de passe. UID/GID fixes et numeriques: un orchestrateur
# peut verifier runAsNonRoot sans lire /etc/passwd.
RUN groupadd --system --gid 10001 mervio \
    && useradd --system --uid 10001 --gid 10001 --home-dir /var/lib/mervio --no-create-home \
        --shell /usr/sbin/nologin mervio \
    && install -d -o 10001 -g 10001 -m 0750 /var/lib/mervio /var/lib/mervio/data /var/lib/mervio/objects /run/mervio

COPY --from=build /opt/venv /opt/venv

# Logs JSON du produit sur stderr, sans tampon: `docker logs` les recoit ligne a ligne.
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MERVIO_WORKER_HEALTH_FILE=/run/mervio/worker-health.json

WORKDIR /var/lib/mervio
USER 10001:10001

# Sante par fichier (observability.health), sans base ni reseau. Aucun serveur HTTP, aucun port.
# Liveness: STARTING, READY, BUSY, DRAINING et DEGRADED (dans sa tolerance) sont vivants;
# STOPPED, FORCED, fichier absent ou ancien ne le sont pas.
HEALTHCHECK --interval=15s --timeout=10s --start-period=90s --retries=3 \
    CMD ["mervio", "worker", "healthcheck"]

# Forme exec: Python recoit SIGTERM directement (le worker installe ses gestionnaires, 004.3.5).
STOPSIGNAL SIGTERM
ENTRYPOINT ["mervio"]
CMD ["worker"]
