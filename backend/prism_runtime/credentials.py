"""Read the existing private runtime credential without exposing it in state."""
from __future__ import annotations
import os
from pathlib import Path
import stat
import subprocess
import sys
from .config import state_directory


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
