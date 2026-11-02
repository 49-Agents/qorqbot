"""Run one engine service process; Trusted clients call its authenticated API."""

import fcntl
import ipaddress
import os

import uvicorn

from .fastmail import Fastmail
from .http import create_app
from .models import Settings
from .service import EmailService
from .store import Store


def main():
    host = os.environ.get("EMAIL_HOST", "127.0.0.1")
    ip = ipaddress.ip_address(host)
    if not ip.is_loopback and ip not in ipaddress.ip_network("100.64.0.0/10"):
        raise ValueError("Bind the engine only to loopback or Tailscale")
    settings = Settings.from_env()
    store = Store(settings.database)
    lock = os.open(str(settings.database) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        provider = Fastmail(settings)
        app = create_app(EmailService(settings, store, provider))
        uvicorn.run(app, host=host, port=int(os.environ.get("EMAIL_PORT", "8835")),
            access_log=False, proxy_headers=False)
    finally:
        os.close(lock)


if __name__ == "__main__":
    main()
