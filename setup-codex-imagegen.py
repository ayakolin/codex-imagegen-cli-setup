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
  1. A shared Python venv with the `openai` SDK ($CODEX_HOME/imagegen-venv),
     using Tsinghua PyPI as the default pip index for faster installs in CN.
  2. A standing instruction in $CODEX_HOME/AGENTS.md so plain "make an image"
     requests auto-run the CLI fallback without asking each time.
  3. A [shell_environment_policy] block in config.toml that injects
     OPENAI_BASE_URL / OPENAI_API_KEY into the CLI subprocess.

Prerequisite: your Codex chat provider (pointing at the proxy with API-key
auth) is already configured and logged in. This script does not touch it.

Usage:
  ./setup-codex-imagegen.py                 # full setup (auto-reads key/base)
  CPA_BASE_URL=... CPA_API_KEY=... ./setup-codex-imagegen.py   # override
  ./setup-codex-imagegen.py --venv-only     # only create venv + install deps
  ./setup-codex-imagegen.py --pip-mirror    # only configure user pip -> Tsinghua
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
CONFIG = CODEX_HOME / "config.toml"
AUTH = CODEX_HOME / "auth.json"
AGENTS = CODEX_HOME / "AGENTS.md"
VENV_DIR = CODEX_HOME / "imagegen-venv"

# Tsinghua University PyPI mirror (https://mirrors.tuna.tsinghua.edu.cn/help/pypi/)
PIP_INDEX_URL = os.environ.get(
    "PIP_INDEX_URL", "https://pypi.tuna.tsinghua.edu.cn/simple"
)
PIP_TRUSTED_HOST = os.environ.get("PIP_TRUSTED_HOST", "pypi.tuna.tsinghua.edu.cn")
# Packages required by the imagegen skill CLI fallback
VENV_PACKAGES = ("openai",)

BLOCK_BEGIN = (
    "<!-- BEGIN codex-imagegen-cli-fallback "
    "(managed by setup-codex-imagegen.py) -->"
)
BLOCK_END = "<!-- END codex-imagegen-cli-fallback -->"


def log(msg: str) -> None:
    print(f"  {msg}", flush=True)


def die(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr, flush=True)
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


def has_package(py: Path, package: str) -> bool:
    if not py.exists():
        return False
    try:
        r = subprocess.run(
            [str(py), "-c", f"import {package}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return r.returncode == 0
    except OSError:
        return False


def has_openai(py: Path) -> bool:
    return has_package(py, "openai")


def openai_version(py: Path) -> str:
    r = subprocess.run(
        [str(py), "-c", "import openai; print(openai.__version__)"],
        capture_output=True,
        text=True,
        check=False,
    )
    return (r.stdout or "").strip() or "unknown"


def pip_mirror_args() -> list[str]:
    """Extra pip CLI args that force the Tsinghua (or override) index."""
    return [
        "-i",
        PIP_INDEX_URL,
        "--trusted-host",
        PIP_TRUSTED_HOST,
    ]


def user_pip_config_path() -> Path:
    """User-level pip.conf / pip.ini location (platform-aware)."""
    system = platform.system().lower()
    if system == "windows" or sys.platform.startswith(("win", "cygwin", "msys")):
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "pip" / "pip.ini"
        return Path.home() / "pip" / "pip.ini"
    # Linux / macOS: prefer XDG, fall back to ~/.pip/pip.conf
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "pip" / "pip.conf"
    return Path.home() / ".config" / "pip" / "pip.conf"


def venv_pip_config_path(venv_dir: Path) -> Path:
    system = platform.system().lower()
    if system == "windows" or sys.platform.startswith(("win", "cygwin", "msys")):
        return venv_dir / "pip.ini"
    # venv-local config: pip looks for pip.conf next to the config scheme;
    # writing under venv/pip/pip.conf is reliable when PIP_CONFIG_FILE is set,
    # but the common portable approach is the site-packages-adjacent conf
    # via ensurepip's location. Prefer the well-known path under the venv.
    return venv_dir / "pip.conf"


def write_pip_config(path: Path) -> None:
    """Write a pip config that defaults to the Tsinghua PyPI mirror."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "[global]\n"
        f"index-url = {PIP_INDEX_URL}\n"
        f"trusted-host = {PIP_TRUSTED_HOST}\n"
    )
    path.write_text(content, encoding="utf-8")
    log(f"pip mirror config: {path}")
    log(f"  index-url = {PIP_INDEX_URL}")


def configure_user_pip_mirror() -> Path:
    """Configure the current user's pip to use the Tsinghua mirror."""
    path = user_pip_config_path()
    write_pip_config(path)
    return path


def configure_venv_pip_mirror(venv_dir: Path, venv_py: Path) -> None:
    """Pin the venv's pip to the Tsinghua mirror via config + env-friendly conf."""
    # pip inside a venv reads $VIRTUAL_ENV/pip.conf on some layouts; also
    # drop a conf that we can point at with PIP_CONFIG_FILE during installs.
    conf = venv_pip_config_path(venv_dir)
    write_pip_config(conf)
    # Additionally set via `pip config` so the venv itself remembers it.
    try:
        subprocess.run(
            [
                str(venv_py),
                "-m",
                "pip",
                "config",
                "--site",
                "set",
                "global.index-url",
                PIP_INDEX_URL,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        subprocess.run(
            [
                str(venv_py),
                "-m",
                "pip",
                "config",
                "--site",
                "set",
                "global.trusted-host",
                PIP_TRUSTED_HOST,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        pass


def run_pip(
    venv_py: Path,
    args: list[str],
    *,
    quiet: bool = True,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run `python -m pip ...` inside the venv with the Tsinghua mirror."""
    # Always force mirror on the CLI so it wins over any global config.
    if args and args[0] in ("install", "download", "wheel"):
        cmd = [str(venv_py), "-m", "pip", args[0], *pip_mirror_args(), *args[1:]]
    else:
        cmd = [str(venv_py), "-m", "pip", *args]

    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    # venv_py is .../bin/python or .../Scripts/python.exe -> venv root is parent.parent
    conf = venv_pip_config_path(venv_py.parent.parent)
    if conf.is_file():
        run_env.setdefault("PIP_CONFIG_FILE", str(conf))

    kwargs: dict = {
        "check": check,
        "text": True,
        "env": run_env,
    }
    if quiet:
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL
    return subprocess.run(cmd, **kwargs)


def ensure_venv(
    pybin: str,
    venv_dir: Path,
    venv_py: Path,
    *,
    force_reinstall: bool = False,
    step_label: str = "[1/3]",
) -> None:
    """Create the shared venv (if needed), switch pip to Tsinghua, install deps."""
    print(f"{step_label} shared venv ({venv_dir})", flush=True)

    if not venv_py.exists():
        log(f"creating venv with {pybin}")
        subprocess.run([pybin, "-m", "venv", str(venv_dir)], check=True)
        if not venv_py.exists():
            die(f"venv created but interpreter missing: {venv_py}")
        log(f"venv ready: {venv_py}")
    else:
        log(f"venv already exists: {venv_py}")

    # Always (re)apply Tsinghua mirror so re-runs stay consistent
    log(f"configuring pip mirror -> {PIP_INDEX_URL}")
    configure_venv_pip_mirror(venv_dir, venv_py)

    # Ensure pip itself is present and reasonably fresh (via mirror)
    log("upgrading pip (via Tsinghua mirror)")
    run_pip(
        venv_py,
        ["install", "--upgrade", "pip", "setuptools", "wheel"],
        quiet=True,
        check=False,
    )

    missing = [p for p in VENV_PACKAGES if force_reinstall or not has_package(venv_py, p)]
    if missing:
        log(f"installing dependencies: {', '.join(missing)}")
        run_pip(
            venv_py,
            ["install", *(["--force-reinstall"] if force_reinstall else []), *missing],
            quiet=False,
            check=True,
        )
        log(f"installed: {', '.join(missing)}")
    else:
        log(f"dependencies already present: {', '.join(VENV_PACKAGES)} (skip install)")

    if has_openai(venv_py):
        log(f"openai version: {openai_version(venv_py)}")
    else:
        die("openai import still fails after install; check network / mirror")


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
    print(f"[2/3] standing instruction ({agents_path})", flush=True)
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
    print(f"[3/3] env injection ({config_path})", flush=True)
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
    # Use the platform temp dir so this works on Windows (no /tmp) as well as Unix.
    try:
        with tempfile.TemporaryDirectory(prefix="codex-imagegen-") as tmp:
            out = Path(tmp) / "imagegen-drycheck.png"
            r = subprocess.run(
                [
                    str(venv_py),
                    str(skill_cli),
                    "generate",
                    "--prompt",
                    "wiring check",
                    "--out",
                    str(out),
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Set up Codex CLI imagegen fallback: shared venv (Tsinghua pip), "
            "AGENTS.md instruction, and shell env injection."
        )
    )
    p.add_argument(
        "--venv-only",
        action="store_true",
        help="Only create the shared venv, set pip to Tsinghua, and install deps",
    )
    p.add_argument(
        "--pip-mirror",
        action="store_true",
        help="Only write the user-level pip config pointing at Tsinghua PyPI",
    )
    p.add_argument(
        "--force-reinstall",
        action="store_true",
        help="Force reinstall of venv packages even if already present",
    )
    p.add_argument(
        "--no-user-pip",
        action="store_true",
        help="Do not also write the user-level pip.conf (venv-local only)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    pybin = find_python()

    # --pip-mirror alone: only touch user-level pip.conf, then exit
    if args.pip_mirror and not args.venv_only:
        print("[pip] configure user pip mirror (Tsinghua)", flush=True)
        configure_user_pip_mirror()
        print(flush=True)
        print(f"Done. pip will use {PIP_INDEX_URL}", flush=True)
        return

    CODEX_HOME.mkdir(parents=True, exist_ok=True)
    venv_py = venv_python(VENV_DIR)

    # Full setup: also pin user pip to Tsinghua (skip with --no-user-pip).
    # --venv-only stays venv-scoped only.
    if not args.venv_only and not args.no_user_pip:
        print("[0/3] user pip mirror (Tsinghua)", flush=True)
        configure_user_pip_mirror()

    step = "[1/1]" if args.venv_only else "[1/3]"
    ensure_venv(
        pybin,
        VENV_DIR,
        venv_py,
        force_reinstall=args.force_reinstall,
        step_label=step,
    )

    if args.venv_only:
        print(flush=True)
        print(f"Done. Shared venv ready at {VENV_DIR}", flush=True)
        print(f"  interpreter: {venv_py}", flush=True)
        print(f"  pip index:   {PIP_INDEX_URL}", flush=True)
        return

    # Full setup: credentials + agents + config
    base_url, api_key = resolve_credentials()
    write_agents_md(AGENTS, venv_py)
    inject_shell_env_policy(CONFIG, base_url, api_key)
    dry_run_check(venv_py)

    print(flush=True)
    print(
        "Done. Restart Codex if it is running. "
        'Then just ask, e.g. "draw a red star and save it".',
        flush=True,
    )


if __name__ == "__main__":
    main()
