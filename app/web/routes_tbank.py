from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from app.db import SessionLocal
from app.services.payments import handle_notification

router = APIRouter()


@router.post("/tbank/notify", response_class=PlainTextResponse)
def tbank_notify(payload: dict) -> PlainTextResponse:
    with SessionLocal() as session:
        event = handle_notification(session, payload)
        # Событие (в том числе с плохой подписью) сохраняем всегда.
        session.commit()
        if not event.signature_ok:
            raise HTTPException(status_code=403, detail="invalid signature")
    return PlainTextResponse("OK")
