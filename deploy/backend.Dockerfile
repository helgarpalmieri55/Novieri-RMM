FROM python:3.12-slim AS base

WORKDIR /srv/app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# msitools: the Group Policy MSI is built per request inside this container
# (docs/superpowers/specs/2026-09-30-gpo-msi-and-subnet-push-design.md §2.2,
# app/services/msi_build.py). Debian packages wixl separately from msitools;
# both are needed (wixl builds, msibuild marks ARM64, msiinfo reads it back).
# check-msitools.sh fails the build if any is missing or wixl is older than
# 0.102, so an image that cannot make the MSI never ships.
COPY deploy/check-msitools.sh /usr/local/bin/check-msitools.sh
RUN apt-get update \
    && apt-get install -y --no-install-recommends wixl msitools \
    && rm -rf /var/lib/apt/lists/* \
    && /bin/sh /usr/local/bin/check-msitools.sh

COPY backend/pyproject.toml ./
RUN pip install --upgrade pip && pip install .

COPY backend/app ./app
COPY backend/migrations ./migrations
COPY backend/alembic.ini ./

# These four directories are named-volume mount points (its-ca,
# its-gateway-tls, its-secrets, beat-schedule). Docker seeds a *fresh* named
# volume from the image path it is mounted over, ownership included -- so
# creating them here as `its` is what makes them writable by the unprivileged
# runtime user. A mount point that does not exist in the image is created
# root-owned instead, and the process cannot write its gateway cert or read a
# key an operator dropped in.
#
# `beat` was added after Celery beat was found DEAD on a live deployment: with
# no --schedule it wrote `celerybeat-schedule` into WORKDIR (/srv/app,
# root-owned), died with EACCES, and every periodic sweep silently never ran
# while the container still reported healthy. See
# backend/tests/test_worker_beat_schedule.py.
RUN useradd --system --uid 1001 its \
    && mkdir -p /var/lib/its/ca /var/lib/its/tls /var/lib/its/secrets /var/lib/its/beat \
    && chown -R its /var/lib/its \
    && chmod 700 /var/lib/its/secrets
USER its

EXPOSE 8000 8443
# --no-proxy-headers: see deploy/docker-compose.yml (api.command) and
# backend/app/core/client_address.py -- only the app reads X-Forwarded-For.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
