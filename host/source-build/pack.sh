#!/bin/bash
set -euo pipefail
manager=$(node -p 'require("./package.json").packageManager')
[[ $manager =~ ^pnpm@([0-9]+\.[0-9]+\.[0-9]+)(\+sha(224|256|384|512)\.[[:xdigit:]]+)?$ ]]
npm install --global --no-fund --no-audit --registry=https://registry.npmjs.org/ "pnpm@${BASH_REMATCH[1]}"
export PNPM_CONFIG_STORE_DIR=/pnpm-store
pnpm install --frozen-lockfile --child-concurrency=2
NODE_OPTIONS=--max-old-space-size=4096 pnpm run build:artifacts --profile official --serial
pnpm run release:pack --family dsh --out /packs/dsh --concurrency 6
pnpm run release:pack --family vendor --out /packs/vendor --concurrency 6
mkdir -p /packs/native
pnpm --dir native/system run build:ts
pnpm --dir native/system run build:native
pnpm --dir native/system/packages/entry pack --pack-destination /packs/native
arch=$(node -p 'process.arch')
case "$arch" in arm64|x64) ;; *) exit 1;; esac
pnpm --dir "native/system/packages/linux-$arch" pack --pack-destination /packs/native
