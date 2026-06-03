#!/usr/bin/env bash
set -euo pipefail

finish() {
  return 0 2>/dev/null || exit 0
}

BOOT_TIMEOUT_SECONDS="${ZARA_OLLAMA_BOOT_TIMEOUT_SECONDS:-90}"
STRICT_BOOTSTRAP="${ZARA_OLLAMA_BOOTSTRAP_STRICT:-0}"
PREFER_LOCAL="${ZARA_PREFER_LOCAL_OLLAMA:-1}"
ORIGINAL_OLLAMA_HOST="${OLLAMA_HOST:-}"

soft_fail() {
  echo "$1"
  if [[ "$STRICT_BOOTSTRAP" == "1" ]]; then
    exit 1
  fi
  finish
}

if [[ "${PREFER_LOCAL}" == "1" && "${ZARA_ENABLE_LOCAL_OLLAMA:-1}" == "1" ]]; then
  unset OLLAMA_HOST
fi

if [[ -n "${OLLAMA_HOST:-}" ]]; then
  echo "Using configured OLLAMA_HOST=${OLLAMA_HOST}"
  finish
fi

if [[ "${ZARA_ENABLE_LOCAL_OLLAMA:-1}" != "1" ]]; then
  echo "Local Ollama bootstrap disabled"
  finish
fi

if ! command -v ollama >/dev/null 2>&1; then
  soft_fail "Ollama binary is not installed in this runtime; continuing without local Ollama bootstrap"
fi

export OLLAMA_HOST="http://127.0.0.1:11434"
export OLLAMA_LLM_LIBRARY="${OLLAMA_LLM_LIBRARY:-cpu}"
export OLLAMA_NUM_PARALLEL="${OLLAMA_NUM_PARALLEL:-1}"
export OLLAMA_MAX_LOADED_MODELS="${OLLAMA_MAX_LOADED_MODELS:-1}"
export OLLAMA_MODELS="${OLLAMA_MODELS:-/root/.ollama/models}"

mkdir -p /root/.ollama
ollama serve >/tmp/ollama.log 2>&1 &

for _ in $(seq 1 "$BOOT_TIMEOUT_SECONDS"); do
  if ollama list >/tmp/ollama-list.log 2>&1; then
    break
  fi
  sleep 1
done

if ! ollama list >/tmp/ollama-list.log 2>&1; then
  echo "Ollama did not start successfully"
  cat /tmp/ollama.log || true
  if [[ -n "${ORIGINAL_OLLAMA_HOST}" ]]; then
    export OLLAMA_HOST="${ORIGINAL_OLLAMA_HOST}"
  fi
  soft_fail "Continuing without local Ollama bootstrap"
fi

OLLAMA_MODELS="$(
python3 - <<'PY'
import os

seen = set()
sources = [
    os.environ.get("ZARA_BOOTSTRAP_MODELS", ""),
    os.environ.get("ZARA_OLLAMA_MODELS", ""),
    os.environ.get("ZARA_PRIMARY_MODEL", ""),
    os.environ.get("ZARA_MODEL_DIRECTOR", ""),
    os.environ.get("ZARA_MODEL_TREND", ""),
    os.environ.get("ZARA_MODEL_CREATOR", ""),
    os.environ.get("ZARA_MODEL_REPHRASE", ""),
    os.environ.get("ZARA_MODEL_SELECTOR", ""),
    os.environ.get("ZARA_MODEL_CODING", ""),
    os.environ.get("ZARA_MODEL_SUMMARY", ""),
    os.environ.get("ZARA_MODEL_QUICK", ""),
]
for raw in sources:
    for item in raw.replace("\n", ",").split(","):
        model = item.strip()
        if not model or model in seen:
            continue
        seen.add(model)
        print(model)
PY
)"

if [[ -z "${OLLAMA_MODELS}" ]]; then
  OLLAMA_MODELS="tinyllama"
fi

printf '%s\n' "$OLLAMA_MODELS" |
while IFS= read -r model; do
  [[ -z "$model" ]] && continue
  echo "Ensuring Ollama model $model"
  if ! ollama pull "$model"; then
    if [[ -n "${ORIGINAL_OLLAMA_HOST}" ]]; then
      export OLLAMA_HOST="${ORIGINAL_OLLAMA_HOST}"
    fi
    soft_fail "Ollama model pull failed for $model; continuing without full local model bootstrap"
  fi
done

ollama list
finish
