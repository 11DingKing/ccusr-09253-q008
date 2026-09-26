"""服务端业务模块。"""

from __future__ import annotations

from fastapi import FastAPI

from .routers import router
from .routers_exchange import router as exchange_router

app = FastAPI(
    title="Practice Hours Guard",
    version="0.1.0",
    description=(
        "Event-sourced practice-hours compliance service. Check-ins, mentor "
        "confirmations and leave corrections are append-only; compliance is "
        "derived by replay and can be frozen into an immutable snapshot. "
        "Cross-institution exchange batches carry signed foreign events that "
        "map into the local curriculum with versioned recognition rules."
    ),
)

app.include_router(router)
app.include_router(exchange_router)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}
