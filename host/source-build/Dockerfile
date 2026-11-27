ARG BASELINE=docker.io/library/node:24.22.3-bookworm
FROM docker.io/library/node:24.22.3-bookworm@sha256:49f7adaf9f1574bc45c20ea317139b39a59d7e758d0e93745e112fd85cb42623 AS builder
RUN apt-get update && apt-get install -y --no-install-recommends musl-tools && rm -rf /var/lib/apt/lists/*
WORKDIR /src
ADD source.tar /src/
COPY pack.sh /builder/
ARG REVISION
ENV DSH_CLIENT_COMMIT_HASH=$REVISION CI=true ELECTRON_SKIP_BINARY_DOWNLOAD=1 PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
RUN --mount=type=cache,id=dsh-portable-source-pnpm,target=/pnpm-store,sharing=locked bash /builder/pack.sh
COPY product.mjs /builder/
RUN node /builder/product.mjs

FROM ${BASELINE}
ARG REVISION
ARG ORIGINAL_BASE
RUN --network=none --mount=type=bind,from=builder,source=/product,target=/dsh-build-product \
    --mount=type=bind,source=apply-layer.mjs,target=/dsh-apply-layer.mjs node /dsh-apply-layer.mjs
LABEL io.dsh-upgrade.source-recipe="official-npm-layout-v1" io.dsh-upgrade.original-base="$ORIGINAL_BASE" \
      org.opencontainers.image.source="https://github.com/deepseek-ai/deepseek-harness" org.opencontainers.image.revision="$REVISION"
