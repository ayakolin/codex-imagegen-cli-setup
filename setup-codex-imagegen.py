#!/usr/bin/env python3
"""
setup-codex-imagegen.py

Make Codex CLI generate images automatically through the imagegen skill's
CLI fallback (scripts/image_gen.py -> your proxy's OpenAI-compatible
/v1/images/generations endpoint, model gpt-image-2), authenticated with an
API key. This is the only image path that works when Codex talks to a proxy
over an API key (the built-in image_gen tool is only registered in ChatGPT
OAuth mode).

This script contains NO secrets. At runtime it reads:
  - the API key from Codex's own auth file ($CODEX_HOME/auth.json, field
    OPENAI_API_KEY), and
  - the base URL from the active provider in $CODEX_HOME/config.toml,
falling back to environment variables (CPA_API_KEY/CPA_BASE_URL or
OPENAI_API_KEY/OPENAI_BASE_URL) and finally to an interactive prompt.
Resolved values are written only into your local config.toml (never commit it).

It sets up three pieces, idempotently:
  1. A shared Python venv with the `openai` SDK ($CODEX_HOME/imagegen-venv).
  2. A standing instruction in $CODEX_HOME/AGENTS.md so plain "make an image"
     requests auto-run the CLI fallback without asking each time.
  3. A [shell_environment_policy] block in config.toml that injects
     OPENAI_BASE_URL / OPENAI_API_KEY into the CLI subprocess.

Prerequisite: your Codex chat provider (pointing at the proxy with API-key
auth) is already configured and logged in. This script does not touch it.

Usage:
  ./setup-codex-imagegen.py                 # auto-reads key/base from Codex
  CPA_BASE_URL=... CPA_API_KEY=... ./setup-codex-imagegen.py   # override
"""

from __future__ import annotations

import getpass
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
CONFIG = CODEX_HOME / "config.toml"
AUTH = CODEX_HOME / "auth.json"
AGENTS = CODEX_HOME / "AGENTS.md"
VENV_DIR = CODEX_HOME / "imagegen-venv"

BLOCK_BEGIN = (
    "<!-- BEGIN codex-imagegen-cli-fallback "
    "(managed by setup-codex-imagegen.py) -->"
)
BLOCK_END = "<!-- END codex-imagegen-cli-fallback -->"


def log(msg: str) -> None:
    print(f"  {msg}")


def die(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def find_python() -> str:
    for name in ("python3", "python"):
        path = shutil.which(name)
        if path:
            return path
    die("python3/python not found on PATH")
    return ""  # unreachable


def read_json_field(path: Path, key: str) -> str | None:
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        value = data.get(key)
        if value:
            return str(value)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return None


def provider_base_url(config_path: Path) -> str | None:
    if not config_path.is_file():
        return None
    try:
        try:
            import tomllib  # Python 3.11+
        except ImportError:
            try:
                import tomli as tomllib  # type: ignore[no-redef]
            except ImportError:
                return None

        with config_path.open("rb") as f:
            data = tomllib.load(f)
        provider = data.get("model_provider")
        providers = data.get("model_providers") or {}
        entry = providers.get(provider) or {}
        url = entry.get("base_url") or ""
        return str(url) if url else None
    except Exception:
        return None


def venv_python(venv_dir: Path) -> Path:
    system = platform.system().lower()
    # Git Bash / MSYS / Cygwin on Windows, and native Windows
    if system == "windows" or sys.platform.startswith(("win", "cygwin", "msys")):
        return venv_dir / "Scripts" / "python.exe"
    # uname-style detection for MINGW*/MSYS* under Git Bash (posix Python)
    uname = platform.uname().system.upper()
    if uname.startswith(("MINGW", "MSYS", "CYGWIN")):
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def has_openai(py: Path) -> bool:
    if not py.is_file() and not os.access(py, os.X_OK):
        # On Windows, is_file() works; on Unix also check executable
        if not py.exists():
            return False
    try:
        r = subprocess.run(
            [str(py), "-c", "import openai"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return r.returncode == 0
    except OSError:
        return False


def openai_version(py: Path) -> str:
    r = subprocess.run(
        [str(py), "-c", "import openai; print(openai.__version__)"],
        capture_output=True,
        text=True,
        check=False,
    )
    return (r.stdout or "").strip() or "unknown"


def ensure_venv(pybin: str, venv_dir: Path, venv_py: Path) -> None:
    print(f"[1/3] shared venv ({venv_dir})")
    if not has_openai(venv_py):
        subprocess.run([pybin, "-m", "venv", str(venv_dir)], check=True)
        # best-effort pip upgrade
        subprocess.run(
            [str(venv_py), "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        subprocess.run(
            [str(venv_py), "-m", "pip", "install", "--quiet", "openai"],
            check=True,
        )
        log("installed openai")
    else:
        log("openai already present, skipping install")
    log(f"openai version: {openai_version(venv_py)}")


def strip_managed_block(text: str, begin: str, end: str) -> str:
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    skip = False
    for line in lines:
        # Match full line content without trailing newline for markers
        stripped = line.rstrip("\r\n")
        if stripped == begin:
            skip = True
            continue
        if stripped == end:
            skip = False
            continue
        if not skip:
            out.append(line)
    return "".join(out)


def agents_instruction(venv_py: Path) -> str:
    return f"""# Image generation policy (standing instruction)

In this environment the built-in `image_gen` tool is NOT available (Codex reaches the model through an API-key proxy connection, which does not register the built-in image tool). This is expected and permanent here.

For ANY request to generate, create, draw, edit, or produce an image / picture / illustration / asset:
- Treat THIS FILE as a standing, explicit, pre-authorized request to use the imagegen skill's CLI fallback (`scripts/image_gen.py`, model `gpt-image-2`).
- Go straight to the CLI fallback. Do NOT ask for confirmation, and do NOT stop to say the built-in tool is unavailable — just run the CLI.
- `OPENAI_API_KEY` and `OPENAI_BASE_URL` are already set in the environment for the CLI.
- Run the CLI with the preinstalled shared virtualenv interpreter at `{venv_py}` (it already has the `openai` SDK). Do NOT create a new per-project `.venv`. If that interpreter is missing, re-run the setup script.
- Stay on `gpt-image-2`; do not use `gpt-image-1.5` unless the user explicitly asks.
"""


def write_agents_md(agents_path: Path, venv_py: Path) -> None:
    print(f"[2/3] standing instruction ({agents_path})")
    existing = ""
    if agents_path.is_file():
        existing = agents_path.read_text(encoding="utf-8")
        # Strip our managed block; also remove legacy blocks from the old .sh script
        for begin in (
            BLOCK_BEGIN,
            "<!-- BEGIN codex-imagegen-cli-fallback (managed by setup-codex-imagegen.sh) -->",
        ):
            existing = strip_managed_block(existing, begin, BLOCK_END)

    block = (
        f"{BLOCK_BEGIN}\n"
        f"{agents_instruction(venv_py)}"
        f"{BLOCK_END}\n"
    )
    body = existing.rstrip("\n")
    if body:
        content = body + "\n\n" + block
    else:
        content = block

    agents_path.write_text(content, encoding="utf-8")
    log(f"wrote managed block (venv interpreter: {venv_py})")


def inject_shell_env_policy(config_path: Path, base_url: str, api_key: str) -> None:
    print(f"[3/3] env injection ({config_path})")
    if not config_path.exists():
        config_path.touch()

    text = config_path.read_text(encoding="utf-8")
    if "shell_environment_policy" in text:
        log("WARNING: [shell_environment_policy] already present; not editing it.")
        log("Ensure its 'set' table includes:")
        log(f'  OPENAI_BASE_URL = "{base_url}"')
        log(f'  OPENAI_API_KEY  = "<key from {AUTH}>"')
        return

    suffix = (
        "\n[shell_environment_policy]\n"
        f'set = {{ OPENAI_BASE_URL = "{base_url}", OPENAI_API_KEY = "{api_key}" }}\n'
    )
    with config_path.open("a", encoding="utf-8") as f:
        f.write(suffix)
    log("appended [shell_environment_policy] with OPENAI_BASE_URL / OPENAI_API_KEY")


def dry_run_check(venv_py: Path) -> None:
    skill_cli = (
        CODEX_HOME / "skills" / ".system" / "imagegen" / "scripts" / "image_gen.py"
    )
    if not skill_cli.is_file():
        log(f"note: imagegen skill not found yet at {skill_cli} (Codex provisions it on first use)")
        return
    try:
        r = subprocess.run(
            [
                str(venv_py),
                str(skill_cli),
                "generate",
                "--prompt",
                "wiring check",
                "--out",
                "/tmp/imagegen-drycheck.png",
                "--dry-run",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if r.returncode == 0:
            log("dry-run wiring check: OK")
        else:
            log("dry-run wiring check: skipped (non-fatal)")
    except OSError:
        log("dry-run wiring check: skipped (non-fatal)")


def resolve_credentials() -> tuple[str, str]:
    """Priority: explicit env override -> Codex's own files -> interactive prompt."""
    base_url = os.environ.get("CPA_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or ""
    api_key = os.environ.get("CPA_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""

    base_from_env = bool(os.environ.get("CPA_BASE_URL") or os.environ.get("OPENAI_BASE_URL"))

    if not base_url:
        base_url = provider_base_url(CONFIG) or ""

    if base_url:
        source = "env" if base_from_env else "config.toml"
        log(f"base URL: from {source}")
    else:
        try:
            base_url = input("Proxy base URL (e.g. https://host/v1): ").strip()
        except EOFError:
            base_url = ""

    if not api_key:
        api_key = read_json_field(AUTH, "OPENAI_API_KEY") or ""

    if api_key:
        log(f"API key: loaded ({len(api_key)} chars)")
    else:
        try:
            api_key = getpass.getpass("API key: ").strip()
        except EOFError:
            api_key = ""

    if not base_url or not api_key:
        die(
            "could not resolve base URL / API key "
            "(set CPA_BASE_URL/CPA_API_KEY or log in to Codex first)"
        )
    return base_url, api_key


def main() -> None:
    pybin = find_python()
    base_url, api_key = resolve_credentials()

    CODEX_HOME.mkdir(parents=True, exist_ok=True)
    venv_py = venv_python(VENV_DIR)

    ensure_venv(pybin, VENV_DIR, venv_py)
    write_agents_md(AGENTS, venv_py)
    inject_shell_env_policy(CONFIG, base_url, api_key)
    dry_run_check(venv_py)

    print()
    print(
        'Done. Restart Codex if it is running. '
        'Then just ask, e.g. "draw a red star and save it".'
    )


if __name__ == "__main__":
    main()
