#!/bin/zsh
set -eu
cd -- "${0:A:h}"
mkdir -p .local-data .local-state
exec python3 -m memory_service.server --init --port 4191 \
  --vault "demo=${PWD}/.local-data/demo-vault" \
  --vault "empty=${PWD}/.local-data/empty-vault" \
  --model-runtime "${PWD}/.local-state/models" \
  --steward-runtime "${PWD}/.local-state/steward"
