# codex-imagegen-cli-setup

Make **Codex CLI** generate images automatically when it reaches the model through an **API-key proxy** (e.g. CLIProxyAPI), without typing "use the CLI" every time.

## Why this exists

Newer Codex moved image generation into a client-side skill + a ChatGPT "apps" tool (`image_gen__imagegen`). That built-in tool is **only registered when Codex authenticates with ChatGPT OAuth** — over a plain API-key proxy connection it is absent, so the model can only fall back to the imagegen skill's CLI (`scripts/image_gen.py`), which calls the OpenAI-compatible `/v1/images/generations` endpoint (model `gpt-image-2`). That endpoint works fine with an API key.

This setup makes that fallback **automatic and self-contained**.

## What the script does (idempotent)

1. Creates a shared Python venv with the `openai` SDK at `$CODEX_HOME/imagegen-venv` (so every project reuses it instead of building a throwaway `.venv`).
2. Writes a standing instruction into `$CODEX_HOME/AGENTS.md` so plain "make me an image" requests go straight to the CLI fallback — no confirmation prompt.
3. Adds a `[shell_environment_policy]` block to `config.toml` that injects `OPENAI_BASE_URL` / `OPENAI_API_KEY` into the CLI subprocess.

It contains **no secrets**: the base URL and API key are read at runtime and written only into your local `~/.codex/config.toml` (which you should never commit).

## Prerequisite

Your Codex **chat provider** (a custom `model_providers.*` pointing at the proxy with API-key auth) must already be configured. This script only adds the image-generation wiring on top of it.

## Usage

Non-interactive (recommended for automation):

```bash
CPA_BASE_URL="https://YOUR-PROXY-HOST/v1" \
CPA_API_KEY="sk-your-proxy-key" \
./setup-codex-imagegen.sh
```

Interactive (prompts for both; the key is not echoed):

```bash
./setup-codex-imagegen.sh
```

Then restart Codex and just ask: `draw a small orange cat on white and save it`.

## Platform notes

- **Linux / macOS**: venv interpreter is `imagegen-venv/bin/python`.
- **Windows (Git Bash / MSYS)**: the script detects it and uses `imagegen-venv/Scripts/python.exe`.
- Requires `python3`/`python` on PATH and network access for the first `pip install openai`.

## Security

- Never commit `~/.codex/config.toml` or `~/.codex/auth.json` — they hold the key.
- This repo is intended to be **private**. Even so, keep it secret-free: only the script and this README belong here.
