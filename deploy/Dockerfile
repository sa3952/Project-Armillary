# syntax=docker/dockerfile:1.7

FROM python:3.13.14-slim-trixie@sha256:bf503bb2243c5aad0aa951544dd60d165f992646441d35dea90893703fc26251 AS builder

ARG VCS_REF
ARG PUBLICATION_STATUS
ARG TARGETPLATFORM

ENV PIP_DISABLE_PIP_VERSION_CHECK="1" \
    PIP_NO_CACHE_DIR="1" \
    PYTHONDONTWRITEBYTECODE="1"

WORKDIR /build

RUN apt-get update \
    && apt-get install --yes --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY deploy/requirements.lock /build/requirements.lock
COPY deploy/build-requirements.lock /build/build-requirements.lock
COPY scripts/publication/verify_linux_source_build.py /build/verify_linux_source_build.py
COPY scripts/verification/capture_build_evidence.py /build/capture_build_evidence.py
COPY third_party/pyswisseph/pyswisseph-2.10.3.2.tar.gz /source/pyswisseph-2.10.3.2.tar.gz

RUN python -m venv /opt/venv \
    && mkdir --parents /wheels /release \
    && python -m pip install \
        --disable-pip-version-check \
        --require-hashes \
        --no-deps \
        --requirement /build/build-requirements.lock \
    && python -m pip wheel \
        --disable-pip-version-check \
        --require-hashes \
        --no-build-isolation \
        --no-binary=pyswisseph \
        --wheel-dir /wheels \
        --requirement /build/requirements.lock \
    && python /build/verify_linux_source_build.py \
        --source-dir /source \
        --wheel-dir /wheels \
        --receipt /release/pyswisseph-linux-source-build.json \
    && find /wheels -type f -name '*.whl' -exec \
        /opt/venv/bin/python -m pip install \
        --disable-pip-version-check \
        --no-index \
        --no-deps \
        {} + \
    && /opt/venv/bin/python -c \
        'import platform,sys,swisseph; ok=platform.python_version()=="3.13.14" and swisseph.version=="2.10.03"; sys.exit(0 if ok else "Python or Swiss Ephemeris version mismatch")' \
    && /opt/venv/bin/python -m pip uninstall --yes pip

RUN --mount=type=secret,id=private_alpha_probe,required=true \
    python -c \
    'import hashlib,json,sys; from pathlib import Path; probe=Path("/run/secrets/private_alpha_probe"); data=probe.read_bytes() if probe.is_file() else b""; data or sys.exit("missing or empty BuildKit probe"); Path("/release/buildkit-probe-consumed.json").write_text(json.dumps({"consumed":True,"nonempty":True,"secret_sha256":hashlib.sha256(data).hexdigest()},sort_keys=True)+"\n",encoding="ascii")'

RUN --mount=type=bind,target=/build-context,readonly \
    python /build/capture_build_evidence.py \
        --context-root /build-context \
        --output-dir /release \
        --source-revision "${VCS_REF}" \
        --target-platform "${TARGETPLATFORM}" \
        --venv-root /opt/venv

FROM python:3.13.14-slim-trixie@sha256:bf503bb2243c5aad0aa951544dd60d165f992646441d35dea90893703fc26251

ARG VCS_REF
ARG PUBLICATION_STATUS
RUN case "${VCS_REF}" in \
      ????????????????????????????????????????) \
        printf '%s' "${VCS_REF}" | grep -Eq '^[0-9a-f]{40}$' ;; \
      *) false ;; \
    esac \
    && case "${PUBLICATION_STATUS}" in \
      provisional_unpublished|published_anonymously_reachable) true ;; \
      *) false ;; \
    esac
LABEL org.opencontainers.image.title="Classical Astrology Private Alpha" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.source="public Git exact revision and release source archive" \
      org.projectarmillary.publication.status="${PUBLICATION_STATUS}" \
      org.classical-astrology.frontend.mode="external-release-v1"

ENV CLASSICAL_ASTROLOGY_PROFILE="private_alpha" \
    CLASSICAL_ASTROLOGY_REQUIRE_FRONTEND_RELEASE="1" \
    CLASSICAL_ASTROLOGY_SOURCE_REVISION="${VCS_REF}" \
    PATH="/opt/venv/bin:${PATH}" \
    PYTHONFAULTHANDLER="1" \
    PYTHONUNBUFFERED="1" \
    PYTHONDONTWRITEBYTECODE="1"

WORKDIR /app

RUN python -m pip uninstall --yes pip \
    && groupadd --gid 10001 astrology \
    && useradd \
        --uid 10001 \
        --gid 10001 \
        --no-create-home \
        --home-dir /nonexistent \
        --shell /usr/sbin/nologin \
        astrology \
    && rm --recursive --force /var/lib/apt/lists/* \
    && find / -xdev -type f -perm /6000 -exec chmod a-s {} +

COPY --from=builder /opt/venv /opt/venv
COPY backend/app /app/backend/app
COPY backend/ephe /app/backend/ephe
COPY backend/place_data /app/backend/place_data
COPY deploy/ephemeris.sha256 /app/deploy/ephemeris.sha256
COPY deploy/frontend-contract.json /app/deploy/frontend-contract.json
COPY deploy/entrypoint.sh /app/deploy/entrypoint.sh
COPY deploy/container_healthcheck.py /app/deploy/container_healthcheck.py
COPY --from=builder /release /usr/local/share/project-armillary/build-evidence
COPY scripts/verification/verify_ephemeris_integrity.py /app/scripts/verification/verify_ephemeris_integrity.py
COPY scripts/verification/verify_place_catalog.py /app/scripts/verification/verify_place_catalog.py
COPY LICENSE /app/LICENSE

RUN --mount=type=bind,target=/build-context,readonly \
    if [ -f /build-context/THIRD_PARTY_NOTICES.md ]; then \
        install -m 0444 /build-context/THIRD_PARTY_NOTICES.md \
            /app/THIRD_PARTY_NOTICES.md; \
    elif [ -f /build-context/publication/public_overlay/THIRD_PARTY_NOTICES.md ]; then \
        install -m 0444 \
            /build-context/publication/public_overlay/THIRD_PARTY_NOTICES.md \
            /app/THIRD_PARTY_NOTICES.md; \
    else \
        echo "missing THIRD_PARTY_NOTICES.md in supported build context" >&2; \
        exit 1; \
    fi

RUN chmod 0555 /app/deploy/entrypoint.sh \
    /app/deploy/container_healthcheck.py \
    /app/scripts/verification/verify_ephemeris_integrity.py \
    /app/scripts/verification/verify_place_catalog.py \
    && find /app /opt/venv -type d -exec chmod 0555 {} + \
    && find /app /opt/venv /usr/local/share/project-armillary -type f -exec chmod go-w {} + \
    && find /usr/local/share/project-armillary -type d -exec chmod 0555 {} +

USER 10001:10001
EXPOSE 8000

HEALTHCHECK --interval=5s --timeout=3s --start-period=20s --retries=12 \
    CMD ["/opt/venv/bin/python", "/app/deploy/container_healthcheck.py"]

ENTRYPOINT ["/app/deploy/entrypoint.sh"]
CMD ["/opt/venv/bin/python", "-m", "uvicorn", "app.main:create_app", "--factory", "--app-dir", "/app/backend", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--loop", "asyncio", "--http", "h11", "--ws", "none", "--no-access-log", "--timeout-worker-healthcheck", "5", "--timeout-graceful-shutdown", "10", "--backlog", "32"]
