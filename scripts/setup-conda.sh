#!/usr/bin/env bash
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_PREFIX="${1:-$PROJECT_ROOT/.conda}"
CONDA_PKGS_DIR="$PROJECT_ROOT/.conda-pkgs"
PNPM_STORE="$PROJECT_ROOT/.pnpm-store"

export CONDA_PKGS_DIRS="$CONDA_PKGS_DIR"
export NPM_CONFIG_USERCONFIG="$PROJECT_ROOT/.npmrc"
mkdir -p "$CONDA_PKGS_DIR"
cd "$PROJECT_ROOT"

echo "Creating/updating ClarifyDeck conda env at $ENV_PREFIX"
echo "Using project-local conda package cache at $CONDA_PKGS_DIR"
conda env update --prefix "$ENV_PREFIX" --file "$PROJECT_ROOT/environment.yml" --prune
if [[ -x "$ENV_PREFIX/bin/pnpm" ]]; then
  "$ENV_PREFIX/bin/pnpm" install --frozen-lockfile --store-dir "$PNPM_STORE"
elif [[ -x "$ENV_PREFIX/Scripts/pnpm.cmd" ]]; then
  "$ENV_PREFIX/Scripts/pnpm.cmd" install --frozen-lockfile --store-dir "$PNPM_STORE"
else
  echo "pnpm was not found inside $ENV_PREFIX" >&2
  exit 1
fi
