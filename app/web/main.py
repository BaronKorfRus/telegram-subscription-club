from fastapi import FastAPI

from app.web.routes_health import router as health_router
from app.web.routes_tbank import router as tbank_router

app = FastAPI(title="Club subscription bot")
app.include_router(health_router)
app.include_router(tbank_router)
