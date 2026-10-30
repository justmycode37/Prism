"""Read the existing private runtime credential without exposing it in state."""
from __future__ import annotations
import os
import json
from pathlib import Path
import stat
import subprocess
import sys
from .config import state_directory


def get_bybit_credentials(mode):
    """Lazy, environment-specific host credentials; paper never reads a secret.

    A mainnet credential is never reused for testnet. The JSON file contains
    api_key, api_secret and account_id; no model or state response receives it.
    """
    if mode == 'paper':
        return None
    if mode not in {'testnet', 'mainnet'}:
        raise ValueError('Unknown Bybit environment')
    explicit = os.environ.get('PRISM_BYBIT_' + mode.upper() + '_CREDENTIALS_FILE')
    path = Path(explicit) if explicit else state_directory() / 'secrets' / ('bybit-' + mode + '.json')
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, 'r') as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise RuntimeError('Bybit credential must be a private, user-owned regular file')
        raw = handle.read(8193)
        if len(raw) > 8192:
            raise RuntimeError('Bybit credential exceeds supported size')
        value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != {'api_key', 'api_secret', 'account_id'} or any(not isinstance(v, str) or not v.strip() for v in value.values()):
        raise RuntimeError('Bybit credential requires api_key, api_secret, account_id')
    return {k: v.strip() for k, v in value.items()}


def get_openai_key():
    explicit = os.environ.get('PRISM_OPENAI_KEY_FILE')
    if not explicit and os.environ.get('OPENAI_API_KEY', '').strip():
        return os.environ['OPENAI_API_KEY'].strip()
    path = Path(explicit) if explicit else state_directory() / 'secrets' / 'openai.key'
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise RuntimeError('Credential file must be a private, user-owned regular file.')
        value = path.read_text().strip()
        return value or None
    except FileNotFoundError:
        pass
    if not explicit and sys.platform == 'darwin':
        try:
            result = subprocess.run(['/usr/bin/security', 'find-generic-password', '-s', 'studio.prism.openai', '-a', 'local', '-w'], capture_output=True, text=True, timeout=3, check=False)
            return result.stdout.strip() or None if result.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            pass
    return None


def get_monid_key():
    """Load the optional host-only connector key; never include it in model state."""
    explicit = os.environ.get('PRISM_MONID_KEY_FILE')
    if not explicit and os.environ.get('MONID_API_KEY', '').strip():
        return os.environ['MONID_API_KEY'].strip()
    path = Path(explicit) if explicit else state_directory() / 'secrets' / 'monid.key'
    try:
        # Do not follow symlinks, including one swapped in between checking and
        # opening the path. Only this user's private regular files are accepted.
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        return None
    except OSError as error:
        raise RuntimeError('Monid credential must be a private regular file.') from error
    with os.fdopen(descriptor, 'r') as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise RuntimeError('Monid credential must be a private, user-owned regular file.')
        value = handle.read(4097).strip()
        if len(value) > 4096:
            raise RuntimeError('Monid credential exceeds the supported size.')
        return value or None
