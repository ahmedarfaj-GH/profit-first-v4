from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app import db
from app.auth import require_api_key
from app.validation import ENTITY_ID_PATTERN

router = APIRouter(tags=["entities"])


# The API key is platform-wide: callers name the owning organization explicitly.
class EntityIn(BaseModel):
    org_id: str = Field(min_length=1, max_length=64)
    id: str = Field(pattern=ENTITY_ID_PATTERN)
    name: str = Field(min_length=1, max_length=200)
    type: str = Field(default="franchisee", min_length=1, max_length=50)
    parent_id: Optional[str] = None
    currency: str = Field(default="SAR", pattern=r"^[A-Za-z]{3}$")


@router.post("/entities", dependencies=[Depends(require_api_key)])
def create_entity(entity: EntityIn):
    if not db.get_organization(entity.org_id):
        raise HTTPException(status_code=400, detail=f"Unknown org_id: {entity.org_id}")
    if entity.parent_id and not db.get_entity(entity.parent_id, org_id=entity.org_id):
        raise HTTPException(status_code=400, detail=f"Unknown parent_id: {entity.parent_id}")
    if db.would_create_cycle(entity.id, entity.parent_id, org_id=entity.org_id):
        raise HTTPException(status_code=400, detail="An entity cannot be its own ancestor")
    if not db.upsert_entity(entity.id, entity.name, entity.type, entity.parent_id, entity.currency.upper(),
                            org_id=entity.org_id):
        raise HTTPException(status_code=409, detail="This entity id belongs to another organization")
    return {"status": "ok", "entity_id": entity.id}


@router.get("/entities/{entity_id}/network-summary", dependencies=[Depends(require_api_key)])
def network_summary(entity_id: str):
    result = db.build_network_summary(entity_id, org_id=None)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Unknown entity_id: {entity_id}")
    return result
