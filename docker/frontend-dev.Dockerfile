# Node toolchain for `bin/dev frontend`.
#
# This is deliberately *not* plain `node:22-slim`: the image's own `superset-node`
# stage installs `build-essential python3 zstd` via `docker/apt-install.sh`, because
# webpack's config pulls in `simple-zstd` (a native wrapper that shells out to the
# `zstd` binary) and node-gyp needs python3. Without them the build dies with
#
#   [webpack-cli] Error: Can not access zstd! Is it installed?
#
# The node major matches `superset-frontend/package.json` engines (^22.22.0); the
# build container used to be node 20 and warned EBADENGINE.
#
# Built automatically by `docker compose run` when the image is missing
# (`bin/dev frontend` does exactly that).

FROM node:22-trixie-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        python3 \
        zstd \
    && rm -rf /var/lib/apt/lists/*

# Webpack's config touches these at require-time; keep them inert.
ENV PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true \
    CYPRESS_INSTALL_BINARY=0 \
    NODE_OPTIONS=--max_old_space_size=8192

WORKDIR /app/superset-frontend
