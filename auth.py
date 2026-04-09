from __future__ import annotations

import getpass
import json
import os
from typing import Optional

try:
    from cryptography.fernet import Fernet
    _CRYPTO_AVAILABLE = True
except ImportError:
    _CRYPTO_AVAILABLE = False

_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".vaultspeed-compare")
_CRED_FILE = os.path.join(_CONFIG_DIR, "credentials.enc")
_KEY_FILE = os.path.join(_CONFIG_DIR, "key.key")

DEFAULT_BASE_URL = "https://app.vaultspeed.com/api"


# ---------------------------------------------------------------------------
# Encryption helpers
# ---------------------------------------------------------------------------

def _ensure_key() -> bytes:
    os.makedirs(_CONFIG_DIR, exist_ok=True)
    if os.path.exists(_KEY_FILE):
        with open(_KEY_FILE, "rb") as f:
            return f.read()
    if not _CRYPTO_AVAILABLE:
        raise RuntimeError("cryptography package is required for credential storage.")
    key = Fernet.generate_key()
    with open(_KEY_FILE, "wb") as f:
        f.write(key)
    try:
        os.chmod(_KEY_FILE, 0o600)
    except Exception:
        pass
    return key


def _save_profiles(profiles: dict) -> None:
    if not _CRYPTO_AVAILABLE:
        return
    key = _ensure_key()
    fernet = Fernet(key)
    data = json.dumps(profiles).encode()
    token = fernet.encrypt(data)
    os.makedirs(_CONFIG_DIR, exist_ok=True)
    with open(_CRED_FILE, "wb") as f:
        f.write(token)
    try:
        os.chmod(_CRED_FILE, 0o600)
    except Exception:
        pass


def _load_profiles() -> dict:
    if not _CRYPTO_AVAILABLE or not os.path.exists(_CRED_FILE):
        return {}
    try:
        key = _ensure_key()
        fernet = Fernet(key)
        with open(_CRED_FILE, "rb") as f:
            token = f.read()
        data = fernet.decrypt(token)
        return json.loads(data.decode())
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_profiles() -> list[str]:
    return list(_load_profiles().keys())


def get_profile(name: str) -> Optional[dict]:
    return _load_profiles().get(name)


def save_profile(name: str, base_url: str, username: str, password: str) -> None:
    profiles = _load_profiles()
    profiles[name] = {"base_url": base_url, "username": username, "password": password}
    _save_profiles(profiles)


def delete_profile(name: str) -> bool:
    profiles = _load_profiles()
    if name not in profiles:
        return False
    del profiles[name]
    _save_profiles(profiles)
    return True


def prompt_credentials(existing_profiles: list[str]) -> tuple[str, str, str, Optional[str]]:
    """
    Interactively prompt for credentials.

    Returns (base_url, username, password, profile_name_to_save).
    profile_name_to_save is None when the user declines to save.
    """
    base_url = input(f"VaultSpeed API base URL [{DEFAULT_BASE_URL}]: ").strip() or DEFAULT_BASE_URL
    username = input("Username (email): ").strip()
    password = getpass.getpass("Password: ")

    save_ans = input("Save these credentials for future use? [Y/n]: ").strip().lower()
    profile_name: Optional[str] = None
    if save_ans in ("", "y", "yes"):
        default_name = username.split("@")[0] if "@" in username else username
        profile_name = input(f"Profile name [{default_name}]: ").strip() or default_name
        if profile_name in existing_profiles:
            overwrite = input(f"Profile '{profile_name}' already exists. Overwrite? [y/N]: ").strip().lower()
            if overwrite != "y":
                profile_name = None

    return base_url, username, password, profile_name
