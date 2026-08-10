"""ASGI entrypoint."""

from risk_ledger.api import create_app

app = create_app()
