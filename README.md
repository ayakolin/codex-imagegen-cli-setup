# codex-imagegen-cli-setup

Make **Codex CLI** generate images automatically when it reaches the model through an **API-key proxy** (e.g. CLIProxyAPI), without typing "use the CLI" every time.

## Why this exists

Newer Codex moved image generation into a client-side skill + a ChatGPT "apps" tool (`image_gen__imagegen`). That built-in tool is **only registered when Codex authenticates with ChatGPT OAuth** — over a plain API-key proxy connection it is absent, so the model can only fall back to the imagegen skill's CLI (`scripts/image_gen.py`), which calls the OpenAI-compatible `/v1/images/generations` endpoint (model `gpt-image-2`). That endpoint works fine with an API key.

This setup makes that fallback **automatic and self-contained**.

## What the script does (idempotent)

1. Points pip at the **Tsinghua PyPI mirror** (user-level `pip.conf` + venv-local config) and creates a shared Python venv with the `openai` SDK at `$CODEX_HOME/imagegen-venv` (so every project reuses it instead of building a throwaway `.venv`).
2. Writes a standing instruction into `$CODEX_HOME/AGENTS.md` so plain "make me an image" requests go straight to the CLI fallback — no confirmation prompt.
3. Adds a `[shell_environment_policy]` block to `config.toml` that injects `OPENAI_BASE_URL` / `OPENAI_API_KEY` into the CLI subprocess.

It contains **no secrets**. At runtime it resolves credentials automatically from Codex's own files:

- **API key** ← `$CODEX_HOME/auth.json` (field `OPENAI_API_KEY`)
- **Base URL** ← the active `model_provider`'s `base_url` in `$CODEX_HOME/config.toml`

If either can't be found it falls back to env vars (`CPA_API_KEY`/`CPA_BASE_URL` or `OPENAI_API_KEY`/`OPENAI_BASE_URL`) and finally an interactive prompt. Resolved values are written only into your local `config.toml` (which you should never commit).

## Prerequisite

Your Codex **chat provider** (a custom `model_providers.*` pointing at the proxy with API-key auth) must already be configured. This script only adds the image-generation wiring on top of it.

## Usage

On a machine already logged in to Codex against the proxy, just run it — it reads the key and base URL from Codex's own files:

```bash
./setup-codex-imagegen.py
# or: python3 setup-codex-imagegen.py
```

To override (e.g. before logging in, or for automation):

```bash
CPA_BASE_URL="https://YOUR-PROXY-HOST/v1" \
CPA_API_KEY="sk-your-proxy-key" \
./setup-codex-imagegen.py
```

Partial modes (no Codex credentials needed):

```bash
# Only create the shared venv, switch its pip to Tsinghua, install openai
./setup-codex-imagegen.py --venv-only

# Only write user-level pip.conf -> Tsinghua PyPI
./setup-codex-imagegen.py --pip-mirror

# Force reinstall packages; skip writing user-level pip.conf
./setup-codex-imagegen.py --venv-only --force-reinstall --no-user-pip
```

Override the mirror (defaults to Tsinghua):

```bash
PIP_INDEX_URL="https://mirrors.aliyun.com/pypi/simple/" \
PIP_TRUSTED_HOST="mirrors.aliyun.com" \
./setup-codex-imagegen.py --venv-only
```

Then restart Codex and just ask: `draw a small orange cat on white and save it`.

## Platform notes

- **Linux / macOS**: venv interpreter is `imagegen-venv/bin/python`.
- **Windows (Git Bash / MSYS)**: the script detects it and uses `imagegen-venv/Scripts/python.exe`.
- Requires `python3`/`python` on PATH and network access for the first `pip install openai`.
- Default pip index: `https://pypi.tuna.tsinghua.edu.cn/simple`.

## Security

- Never commit `~/.codex/config.toml` or `~/.codex/auth.json` — they hold the key.
- This repo is intended to be **private**. Even so, keep it secret-free: only the script and this README belong here.
