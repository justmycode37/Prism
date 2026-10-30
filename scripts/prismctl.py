#!/usr/bin/env python3
"""Control or install the current user's local Prism cascade service."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import secrets
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
STATE = Path(os.environ.get("PRISM_STATE_DIR", str(ROOT / ".prism-runtime"))).expanduser().resolve()
TOKEN = Path(os.environ.get("PRISM_CONTROL_TOKEN_FILE", str(STATE / "control.token"))).expanduser().resolve()
LABEL = "studio.prism.runtime"
SERVICE_FILE = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
BASE_URL = "http://127.0.0.1:8787"


def prepare_token() -> Path:
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    STATE.chmod(0o700)
    TOKEN.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not TOKEN.exists():
        descriptor = os.open(TOKEN, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(secrets.token_urlsafe(32))
    TOKEN.chmod(0o600)
    if not TOKEN.read_text().strip():
        raise ValueError("The local control token is empty.")
    return TOKEN


def install_service() -> None:
    if sys.platform != "darwin":
        raise RuntimeError("Login-service installation requires macOS.")
    existing_link = False
    if TOKEN.is_file() and SERVICE_FILE.is_file():
        try:
            previous_job = plistlib.loads(SERVICE_FILE.read_bytes())
            existing_link = previous_job.get("EnvironmentVariables", {}).get("PRISM_CONTROL_TOKEN_FILE") == str(TOKEN)
        except (OSError, ValueError, plistlib.InvalidFileException):
            pass
    token_path = prepare_token()
    if not existing_link:
        swift = shutil.which("swift")
        if swift is None:
            raise RuntimeError("Swift is required to connect the console's Keychain control token.")
        linked = subprocess.run(
            [swift, str(ROOT / "scripts" / "PrismCredentialTool.swift"), "studio.prism.control"],
            input=token_path.read_bytes().strip(), capture_output=True, timeout=60,
        )
        if linked.returncode:
            raise RuntimeError("Could not store the local control token in the macOS Keychain.")
    job = {
        "Label": LABEL,
        "ProgramArguments": [str(Path(sys.executable).resolve()), "-m", "prism_runtime"],
        "WorkingDirectory": str(ROOT / "backend"),
        "EnvironmentVariables": {
            "PRISM_STATE_DIR": str(STATE),
            "PRISM_CONTROL_TOKEN_FILE": str(TOKEN),
            "PRISM_OPENAI_KEY_FILE": os.environ.get("PRISM_OPENAI_KEY_FILE", str(STATE / "secrets" / "openai.key")),
            "PRISM_BYBIT_TESTNET_CREDENTIALS_FILE": os.environ.get("PRISM_BYBIT_TESTNET_CREDENTIALS_FILE", str(STATE / "secrets" / "bybit-testnet.json")),
            "PRISM_BYBIT_MAINNET_CREDENTIALS_FILE": os.environ.get("PRISM_BYBIT_MAINNET_CREDENTIALS_FILE", str(STATE / "secrets" / "bybit-mainnet.json")),
            "PRISM_BYBIT_MAINNET_ENABLED": "1" if os.environ.get("PRISM_BYBIT_MAINNET_ENABLED") == "1" else "0",
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONUNBUFFERED": "1",
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "ProcessType": "Background",
        "Umask": 0o077,
        "StandardOutPath": str(STATE / "runtime.stdout.log"),
        "StandardErrorPath": str(STATE / "runtime.stderr.log"),
    }
    SERVICE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if SERVICE_FILE.exists():
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    with SERVICE_FILE.open("wb") as handle:
        plistlib.dump(job, handle)
    SERVICE_FILE.chmod(0o600)
    result = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(SERVICE_FILE)], capture_output=True)
    if result.returncode:
        raise RuntimeError(f"macOS could not start the service (exit {result.returncode}).")
    print("Prism cascade service installed; a fresh cascade database starts paused.")


def request(path: str, payload: dict | None = None) -> dict:
    token = TOKEN.read_text().strip()
    if not token:
        raise ValueError("The local control token is empty.")
    call = urllib.request.Request(
        BASE_URL + path,
        data=None if payload is None else json.dumps(payload, allow_nan=False).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    # Local control never traverses an environment HTTP proxy or redirects a token.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(call, timeout=15) as response:
        return json.load(response)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("install", "uninstall", "status", "start", "stop", "pause", "resume", "run", "configure", "register", "calendar", "review", "development-tape"))
    parser.add_argument("--config-file", type=Path, help="JSON object to POST as a partial configuration update.")
    parser.add_argument("--input-file", type=Path, help="Explicit strategy registration or sourced calendar JSON object.")
    parser.add_argument("--strategy-id", help="Exact strategy or experiment version to review.")
    arguments = parser.parse_args()
    try:
        if arguments.command == "install":
            install_service()
            return 0
        if arguments.command == "uninstall":
            if sys.platform != "darwin":
                raise RuntimeError("Login-service removal requires macOS.")
            subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
            SERVICE_FILE.unlink(missing_ok=True)
            print("Service removed; local data and credentials retained.")
            return 0
        if arguments.command == "status":
            state = request("/v1/state")
            result = {key: state.get(key) for key in ("runtime", "budget", "cascade", "operations")}
        elif arguments.command in {"register", "calendar", "development-tape"}:
            if arguments.input_file is None:
                parser.error(arguments.command + " requires --input-file PATH")
            body = json.loads(arguments.input_file.read_text())
            if not isinstance(body, dict):
                raise ValueError("Input must contain a JSON object")
            endpoint = {"register": "/v1/strategy", "calendar": "/v1/calendar", "development-tape": "/v1/development-tape"}
            result = request(endpoint[arguments.command], body)
        elif arguments.command == "review":
            if not arguments.strategy_id:
                parser.error("review requires --strategy-id VERSION")
            result = request("/v1/review", {"strategy_id": arguments.strategy_id})
        elif arguments.command == "configure":
            if arguments.config_file is None:
                parser.error("configure requires --config-file PATH")
            changes = json.loads(arguments.config_file.read_text())
            if not isinstance(changes, dict):
                raise ValueError("The configuration file must contain a JSON object.")
            result = request("/v1/config", changes)
        else:
            action = "run_cycle" if arguments.command == "run" else arguments.command
            result = request("/v1/control", {"action": action})
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except urllib.error.HTTPError as error:
        print(f'Prism: local API returned HTTP {error.code}; check the runtime and control-token link.', file=sys.stderr)
        return 1
    except RuntimeError as error:
        print(f'Prism: {error}', file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired:
        print('Prism: macOS credential-linking operation timed out.', file=sys.stderr)
        return 1
    except (OSError, ValueError, urllib.error.URLError) as error:
        print(f"Prism: {type(error).__name__}; check the local runtime and configuration.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
