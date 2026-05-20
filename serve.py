"""
drip.serve
==========

Combined FastAPI server that runs both the facilitator and the mock
emitter in a single process. Two ports:

    8090 — facilitator (verify + settle endpoints)
    8091 — mock emitter (x402 challenge + signal delivery)

Run with:
    poetry run python serve.py

Or via uvicorn directly:
    poetry run uvicorn serve:app_emitter --host 0.0.0.0 --port 8091
    poetry run uvicorn serve:app_facilitator --host 0.0.0.0 --port 8090

For production on the droplet (under PM2), use:
    pm2 start "poetry run python serve.py" --name drip-mock
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI

# Load .env before importing the routers (they read env at import time)
load_dotenv()

# Imports must happen AFTER load_dotenv()
from facilitator import router as facilitator_router  # noqa: E402
from mock_emitter import router as emitter_router, simulator  # noqa: E402

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Apps
# ----------------------------------------------------------------------------

@asynccontextmanager
async def emitter_lifespan(app: FastAPI):
    """Start cascade simulator on app boot, stop on shutdown."""
    simulator.start()
    logger.info("Mock emitter ready on port 8091")
    try:
        yield
    finally:
        await simulator.stop()


app_facilitator = FastAPI(title="Drip Facilitator")
app_facilitator.include_router(facilitator_router, prefix="/facilitator")


@app_facilitator.get("/")
async def root_facilitator():
    return {"service": "drip-facilitator", "endpoints": ["/facilitator/health", "/facilitator/verify", "/facilitator/settle"]}


app_emitter = FastAPI(title="Drip Mock Emitter", lifespan=emitter_lifespan)
app_emitter.include_router(emitter_router)


@app_emitter.get("/")
async def root_emitter():
    return {"service": "drip-mock-emitter", "endpoints": ["/health", "/signals/latest"]}


# ----------------------------------------------------------------------------
# Run both on different ports in the same event loop
# ----------------------------------------------------------------------------

async def _serve() -> None:
    config_fac = uvicorn.Config(
        app_facilitator,
        host="0.0.0.0",
        port=8090,
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
    )
    config_em = uvicorn.Config(
        app_emitter,
        host="0.0.0.0",
        port=8091,
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
    )
    server_fac = uvicorn.Server(config_fac)
    server_em = uvicorn.Server(config_em)

    logger.info("Starting facilitator on :8090 and emitter on :8091")
    await asyncio.gather(server_fac.serve(), server_em.serve())


if __name__ == "__main__":
    asyncio.run(_serve())