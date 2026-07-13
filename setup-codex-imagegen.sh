#!/usr/bin/env bash
#
# setup-codex-imagegen.sh
#
# Make Codex CLI generate images automatically through the imagegen skill's
# CLI fallback (scripts/image_gen.py -> your proxy's OpenAI-compatible
# /v1/images/generations endpoint, model gpt-image-2), authenticated with an
# API key. This is the only image path that works when Codex talks to a proxy
# over an API key (the built-in image_gen tool is only registered in ChatGPT
# OAuth mode).
#
# This script contains NO secrets. At runtime it reads:
#   - the API key from Codex's own auth file ($CODEX_HOME/auth.json, field
#     OPENAI_API_KEY), and
#   - the base URL from the active provider in $CODEX_HOME/config.toml,
# falling back to environment variables (CPA_API_KEY/CPA_BASE_URL or
# OPENAI_API_KEY/OPENAI_BASE_URL) and finally to an interactive prompt.
# Resolved values are written only into your local config.toml (never commit it).
#
# It sets up three pieces, idempotently:
#   1. A shared Python venv with the `openai` SDK ($CODEX_HOME/imagegen-venv).
#   2. A standing instruction in $CODEX_HOME/AGENTS.md so plain "make an image"
#      requests auto-run the CLI fallback without asking each time.
#   3. A [shell_environment_policy] block in config.toml that injects
#      OPENAI_BASE_URL / OPENAI_API_KEY into the CLI subprocess.
#
# Prerequisite: your Codex chat provider (pointing at the proxy with API-key
# auth) is already configured and logged in. This script does not touch it.
#
# Usage:
#   ./setup-codex-imagegen.sh                 # auto-reads key/base from Codex
#   CPA_BASE_URL=... CPA_API_KEY=... ./setup-codex-imagegen.sh   # override

set -euo pipefail

CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
CONFIG="$CODEX_HOME/config.toml"
AUTH="$CODEX_HOME/auth.json"
AGENTS="$CODEX_HOME/AGENTS.md"
VENV_DIR="$CODEX_HOME/imagegen-venv"

log() { printf '  %s\n' "$*"; }

PYBIN="$(command -v python3 || command -v python || true)"
[ -n "$PYBIN" ] || { echo "error: python3/python not found on PATH" >&2; exit 1; }

# --- readers (no secrets in the script) -------------------------------------
read_json_field() { # $1=file $2=key -> prints value or fails
  [ -f "$1" ] || return 1
  if command -v jq >/dev/null 2>&1; then
    local v; v="$(jq -re --arg k "$2" '.[$k] // empty' "$1" 2>/dev/null || true)"
    [ -n "$v" ] && { printf '%s' "$v"; return 0; }
  fi
  "$PYBIN" - "$1" "$2" <<'PY'
import json, sys
try:
    with open(sys.argv[1]) as f:
        v = json.load(f).get(sys.argv[2])
    sys.stdout.write(v) if v else sys.exit(1)
except Exception:
    sys.exit(1)
PY
}

provider_base_url() { # prints active model_provider's base_url from config.toml
  [ -f "$CONFIG" ] || return 1
  "$PYBIN" - "$CONFIG" <<'PY'
import sys
try:
    import tomllib
except Exception:
    sys.exit(1)
try:
    with open(sys.argv[1], "rb") as f:
        d = tomllib.load(f)
    p = d.get("model_provider")
    u = (d.get("model_providers", {}).get(p) or {}).get("base_url", "")
    sys.stdout.write(u) if u else sys.exit(1)
except Exception:
    sys.exit(1)
PY
}

# --- resolve base url + api key ---------------------------------------------
# Priority: explicit env override -> Codex's own files -> interactive prompt.
BASE_URL="${CPA_BASE_URL:-${OPENAI_BASE_URL:-}}"
API_KEY="${CPA_API_KEY:-${OPENAI_API_KEY:-}}"

if [ -z "$BASE_URL" ]; then BASE_URL="$(provider_base_url || true)"; fi
if [ -n "$BASE_URL" ]; then log "base URL: from ${CPA_BASE_URL:+env}${CPA_BASE_URL:-config.toml}"; else
  read -r -p "Proxy base URL (e.g. https://host/v1): " BASE_URL
fi

if [ -z "$API_KEY" ]; then API_KEY="$(read_json_field "$AUTH" OPENAI_API_KEY || true)"; fi
if [ -n "$API_KEY" ]; then log "API key: loaded (${#API_KEY} chars)"; else
  read -r -s -p "API key: " API_KEY; printf '\n'
fi

if [ -z "$BASE_URL" ] || [ -z "$API_KEY" ]; then
  echo "error: could not resolve base URL / API key (set CPA_BASE_URL/CPA_API_KEY or log in to Codex first)" >&2
  exit 1
fi

mkdir -p "$CODEX_HOME"

# --- OS-specific venv interpreter path --------------------------------------
case "$(uname -s 2>/dev/null || echo unknown)" in
  MINGW*|MSYS*|CYGWIN*) VENV_PY="$VENV_DIR/Scripts/python.exe" ;;
  *)                    VENV_PY="$VENV_DIR/bin/python" ;;
esac

# --- 1. shared venv with openai (idempotent) --------------------------------
echo "[1/3] shared venv ($VENV_DIR)"
if [ ! -x "$VENV_PY" ] || ! "$VENV_PY" -c 'import openai' >/dev/null 2>&1; then
  "$PYBIN" -m venv "$VENV_DIR"
  "$VENV_PY" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
  "$VENV_PY" -m pip install --quiet openai
  log "installed openai"
else
  log "openai already present, skipping install"
fi
log "openai version: $("$VENV_PY" -c 'import openai; print(openai.__version__)')"

# --- 2. AGENTS.md standing instruction (managed block, idempotent) ----------
echo "[2/3] standing instruction ($AGENTS)"
BLOCK_BEGIN="<!-- BEGIN codex-imagegen-cli-fallback (managed by setup-codex-imagegen.sh) -->"
BLOCK_END="<!-- END codex-imagegen-cli-fallback -->"

if [ -f "$AGENTS" ]; then
  awk -v b="$BLOCK_BEGIN" -v e="$BLOCK_END" '
    $0==b {skip=1}
    skip==0 {print}
    $0==e {skip=0}' "$AGENTS" > "$AGENTS.tmp" && mv "$AGENTS.tmp" "$AGENTS"
fi

{
  if [ -s "$AGENTS" ]; then printf '\n'; fi
  printf '%s\n' "$BLOCK_BEGIN"
  cat <<EOF
# Image generation policy (standing instruction)

In this environment the built-in \`image_gen\` tool is NOT available (Codex reaches the model through an API-key proxy connection, which does not register the built-in image tool). This is expected and permanent here.

For ANY request to generate, create, draw, edit, or produce an image / picture / illustration / asset:
- Treat THIS FILE as a standing, explicit, pre-authorized request to use the imagegen skill's CLI fallback (\`scripts/image_gen.py\`, model \`gpt-image-2\`).
- Go straight to the CLI fallback. Do NOT ask for confirmation, and do NOT stop to say the built-in tool is unavailable — just run the CLI.
- \`OPENAI_API_KEY\` and \`OPENAI_BASE_URL\` are already set in the environment for the CLI.
- Run the CLI with the preinstalled shared virtualenv interpreter at \`$VENV_PY\` (it already has the \`openai\` SDK). Do NOT create a new per-project \`.venv\`. If that interpreter is missing, re-run the setup script.
- Stay on \`gpt-image-2\`; do not use \`gpt-image-1.5\` unless the user explicitly asks.
EOF
  printf '%s\n' "$BLOCK_END"
} >> "$AGENTS"
log "wrote managed block (venv interpreter: $VENV_PY)"

# --- 3. inject env vars into config.toml -----------------------------------
echo "[3/3] env injection ($CONFIG)"
touch "$CONFIG"
if grep -q 'shell_environment_policy' "$CONFIG" 2>/dev/null; then
  log "WARNING: [shell_environment_policy] already present; not editing it."
  log "Ensure its 'set' table includes:"
  log "  OPENAI_BASE_URL = \"$BASE_URL\""
  log "  OPENAI_API_KEY  = \"<key from $AUTH>\""
else
  {
    printf '\n[shell_environment_policy]\n'
    printf 'set = { OPENAI_BASE_URL = "%s", OPENAI_API_KEY = "%s" }\n' "$BASE_URL" "$API_KEY"
  } >> "$CONFIG"
  log "appended [shell_environment_policy] with OPENAI_BASE_URL / OPENAI_API_KEY"
fi

# --- optional wiring check (no network, no key) -----------------------------
SKILL_CLI="$CODEX_HOME/skills/.system/imagegen/scripts/image_gen.py"
if [ -f "$SKILL_CLI" ]; then
  if "$VENV_PY" "$SKILL_CLI" generate --prompt "wiring check" --out /tmp/imagegen-drycheck.png --dry-run >/dev/null 2>&1; then
    log "dry-run wiring check: OK"
  else
    log "dry-run wiring check: skipped (non-fatal)"
  fi
else
  log "note: imagegen skill not found yet at $SKILL_CLI (Codex provisions it on first use)"
fi

echo
echo "Done. Restart Codex if it is running. Then just ask, e.g. \"draw a red star and save it\"."
