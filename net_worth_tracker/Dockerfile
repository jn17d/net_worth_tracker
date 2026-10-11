# Home Assistant's per-arch base images (amd64-base-python, …) are frozen;
# current base images are published as multi-arch manifests on GHCR for
# amd64 and arm64. Referencing the image directly here replaces the
# deprecated build.yaml — BuildKit resolves the right architecture at
# build time.
ARG BUILD_FROM=ghcr.io/home-assistant/base-python:3.12-alpine3.24
FROM ${BUILD_FROM}

# The app is pure Python 3 standard library — nothing to install.

COPY run.sh /
RUN chmod a+x /run.sh

COPY app /app

WORKDIR /app
CMD [ "/run.sh" ]
