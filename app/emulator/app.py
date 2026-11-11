"""Эмулятор провайдеров: `uvicorn app.emulator.app:app --port 8001`.

/paymaster-a/...  — PayMaster, эквайер A
/paymaster-b/...  — PayMaster, эквайер B
/pally/...        — Pally
"""

import logging

from fastapi import FastAPI

from app.emulator.config import get_emulator_settings
from app.emulator.pally import AMOUNT_SCENARIOS, create_pally_app
from app.emulator.paymaster import TEST_CARDS, create_paymaster_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def create_app() -> FastAPI:
    settings = get_emulator_settings()
    app = FastAPI(title="Payment providers emulator")
    app.mount("/paymaster-a", create_paymaster_app("paymaster_a", settings.paymaster_a_token))
    app.mount("/paymaster-b", create_paymaster_app("paymaster_b", settings.paymaster_b_token))
    app.mount("/pally", create_pally_app(settings.pally_token))

    @app.get("/")
    async def scenarios() -> dict[str, object]:
        return {"paymaster_test_cards": TEST_CARDS, "pally_amount_cents": AMOUNT_SCENARIOS}

    return app


app = create_app()
