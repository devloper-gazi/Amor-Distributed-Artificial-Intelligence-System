"""
Chat folder persistence API.

Folders are shared across chat modes (research/thinking/coding) and are scoped
to the same lightweight ownership model as sessions (X-User-Id if present,
otherwise X-Client-Id).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

from ..infrastructure.chat_store import chat_store


router = APIRouter(prefix="/api/folders", tags=["folders"])


def _require_client_id(x_client_id: Optional[str]) -> str:
    if not x_client_id or not x_client_id.strip():
        raise HTTPException(status_code=400, detail="Missing X-Client-Id header")
    return x_client_id.strip()


def _dt_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class FolderCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)


class FolderRenameRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)

class FolderUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    pinned: Optional[bool] = None


class FolderSummary(BaseModel):
    id: str
    name: str
    created_at: datetime
    updated_at: datetime
    pinned: bool = False


class FolderListResponse(BaseModel):
    folders: List[FolderSummary]


@router.get("", response_model=FolderListResponse)
async def list_folders(
    limit: int = Query(200, ge=1, le=200),
    offset: int = Query(0, ge=0),
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    client_id = _require_client_id(x_client_id)
    folders = await chat_store.list_folders(
        client_id=client_id,
        user_id=x_user_id,
        limit=limit,
        offset=offset,
    )

    return FolderListResponse(
        folders=[
            FolderSummary(
                id=f["id"],
                name=f.get("name") or "",
                created_at=_dt_utc(f["created_at"]),
                updated_at=_dt_utc(f["updated_at"]),
                pinned=bool(f.get("pinned", False)),
            )
            for f in folders
        ]
    )


@router.post("", response_model=FolderSummary)
async def create_folder(
    request: FolderCreateRequest,
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    client_id = _require_client_id(x_client_id)
    try:
        folder = await chat_store.create_folder(
            client_id=client_id,
            user_id=x_user_id,
            name=request.name,
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid folder name")

    return FolderSummary(
        id=folder["id"],
        name=folder["name"],
        created_at=_dt_utc(folder["created_at"]),
        updated_at=_dt_utc(folder["updated_at"]),
        pinned=bool(folder.get("pinned", False)),
    )


@router.patch("/{folder_id}")
async def update_folder(
    folder_id: str,
    request: FolderUpdateRequest,
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    client_id = _require_client_id(x_client_id)
    try:
        fields_set = getattr(request, "model_fields_set", getattr(request, "__fields_set__", set()))
        updates = {}
        if "name" in fields_set:
            updates["name"] = request.name
        if "pinned" in fields_set:
            updates["pinned"] = bool(request.pinned)

        await chat_store.update_folder(
            client_id=client_id,
            user_id=x_user_id,
            folder_id=folder_id,
            **updates,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Folder not found")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid folder name")

    return {"ok": True}


@router.delete("/{folder_id}")
async def delete_folder(
    folder_id: str,
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    client_id = _require_client_id(x_client_id)
    await chat_store.delete_folder(
        client_id=client_id,
        user_id=x_user_id,
        folder_id=folder_id,
    )
    return {"ok": True}

