"""
Environment & NVIDIA NIM Configuration Loader for AEGIS-CONTROL.
Loads `.env` into `os.environ` (only for keys not already set) and validates
`NVIDIA_API_KEY` + `AEGIS_LLM_MODEL`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


DEFAULT_NIM_MODEL = "nvidia/nemotron-3.5-lightning-30b-a3b"
DEFAULT_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def load_dotenv(env_path: Optional[Path | str] = None) -> dict[str, str]:
    """
    Parse a `.env` file and populate `os.environ` ONLY for keys that are not
    already set in `os.environ`. Returns a dict of loaded key-value pairs.
    """
    path = Path(env_path) if env_path is not None else DEFAULT_ENV_PATH
    loaded: dict[str, str] = {}
    if not path.is_file():
        return loaded

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key and key not in os.environ:
            os.environ[key] = val
            loaded[key] = val
    return loaded


def require_nim_config(
    env_path: Optional[Path | str] = None,
) -> tuple[str, str]:
    """
    Ensure `NVIDIA_API_KEY` is present and non-empty (loading `.env` first if unset)
    and return `(api_key, model_name)`. Raises a clear banner-style RuntimeError
    if `NVIDIA_API_KEY` is missing or empty.
    """
    load_dotenv(env_path)
    api_key = os.environ.get("NVIDIA_API_KEY", "").strip()
    model = os.environ.get("AEGIS_LLM_MODEL", DEFAULT_NIM_MODEL).strip() or DEFAULT_NIM_MODEL
    if not api_key:
        banner = (
            "\n"
            "========================================================================\n"
            "  AEGIS-CONTROL CONFIGURATION ERROR: MISSING NVIDIA_API_KEY\n"
            "========================================================================\n"
            "  Live LLM Fleet execution requires a valid NVIDIA NIM API key.\n"
            "  Set NVIDIA_API_KEY in `/home/ubuntu/aegis-ps01/.env` or your shell:\n"
            "      NVIDIA_API_KEY=nvapi-...\n"
            "      AEGIS_LLM_MODEL=nvidia/nemotron-3.5-lightning-30b-a3b\n"
            "========================================================================\n"
        )
        raise RuntimeError(banner)
    return api_key, model
