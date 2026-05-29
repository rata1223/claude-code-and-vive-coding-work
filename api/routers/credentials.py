import httpx
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from api.crypto import decrypt, encrypt
from api.database import get_db
from api.deps import get_current_user
from api.models import Credential, User
from api.schemas import CredentialCreate, CredentialOut, Resp

router = APIRouter(prefix="/api/credentials", tags=["credentials"])


def _credential_to_dict(cred: Credential) -> dict:
    return {
        "id": cred.id,
        "name": cred.name,
        "exchange_id": cred.exchange_id,
        "env": cred.env,
        "created_at": cred.created_at.isoformat() if cred.created_at else None,
        # Return masked versions of all sensitive fields — never return plaintext secrets
        "app_key": "****" if cred.app_key_enc else None,
        "account_no": "****" if cred.account_no_enc else None,
        "hts_id": "****" if cred.hts_id_enc else None,
    }


@router.get("/list")
def list_credentials(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    creds = db.query(Credential).filter(Credential.user_id == current_user.id).all()
    return Resp.ok({"items": [_credential_to_dict(c) for c in creds]})


@router.get("/get")
def get_credential(
    id: int = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    cred = (
        db.query(Credential)
        .filter(Credential.id == id, Credential.user_id == current_user.id)
        .first()
    )
    if not cred:
        return Resp.err("Credential not found", code=-1)
    return Resp.ok(_credential_to_dict(cred))


@router.post("/create")
def create_credential(
    body: CredentialCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    cred = Credential(
        user_id=current_user.id,
        name=body.name,
        exchange_id=body.exchange_id,
        env=body.env,
        app_key_enc=encrypt(body.app_key),
        app_secret_enc=encrypt(body.app_secret),
        account_no_enc=encrypt(body.account_no),
        hts_id_enc=encrypt(body.hts_id),
        api_key_enc=encrypt(body.api_key),
    )
    db.add(cred)
    db.commit()
    db.refresh(cred)
    return Resp.ok(_credential_to_dict(cred))


@router.delete("/delete")
def delete_credential(
    id: int = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    cred = (
        db.query(Credential)
        .filter(Credential.id == id, Credential.user_id == current_user.id)
        .first()
    )
    if not cred:
        return Resp.err("Credential not found", code=-1)
    db.delete(cred)
    db.commit()
    return Resp.ok(None, "Deleted")


@router.get("/egress-ip")
async def egress_ip():
    """Return outbound IP that KIS API sees."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get("https://api.ipify.org?format=json")
            ip = r.json().get("ip", "unknown")
    except Exception:
        ip = "unknown"
    return Resp.ok({"ip": ip})
