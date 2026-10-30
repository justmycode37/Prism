"""Launch Prism's independent event services and authenticated local UI API."""
from __future__ import annotations

import fcntl
import os
import signal
import sys
import threading

from .config import state_directory, token_file
from .orchestrator import Runtime
from .server import create_server
from .store import Store


def main() -> int:
    os.umask(0o077)
    directory = state_directory()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = (directory / "cascade.lock").open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        print("A Prism cascade already owns this state directory.", file=sys.stderr)
        return 1

    store = runtime = server = scheduler = None
    try:
        token = token_file().read_text().strip()
        if not token:
            raise ValueError("The private local control-token file is empty.")
        store = Store(directory / "cascade.sqlite3")
        runtime = Runtime(store)
        # Bind successfully before a scheduler can perform any external work.
        server = create_server(runtime, token)

        def stop_server(_signum, _frame):
            threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGINT, stop_server)
        signal.signal(signal.SIGTERM, stop_server)
        scheduler = threading.Thread(target=runtime.run_forever, name="prism-cascade", daemon=True)
        scheduler.start()
        print("Prism API listening on http://127.0.0.1:8787 (" + store.config()['bybit']['mode'] +
              "; explicit operator execution gates apply).", flush=True)
        server.serve_forever(poll_interval=0.25)
        return 0
    except (OSError, ValueError) as error:
        print(f"Prism cascade could not start ({type(error).__name__}); check local configuration.", file=sys.stderr)
        return 1
    finally:
        if runtime is not None:
            runtime.shutdown()
        if scheduler is not None:
            # Keep ownership until bounded in-flight requests have settled.
            scheduler.join()
        if server is not None:
            server.server_close()
        if store is not None and (scheduler is None or not scheduler.is_alive()):
            store.close()
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
