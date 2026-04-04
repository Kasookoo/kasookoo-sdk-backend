"""
Associated Numbers API - organization-scoped inbound number mapping.
"""
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.auth import get_organization_id
from app.models.models import AssociatedNumberCreate, AssociatedNumberResponse, AssociatedNumberUpdate
from app.services.associated_number_service import associated_number_service
from app.utils.performance_monitor import monitor

router = APIRouter()


@router.get("/associated-numbers", response_model=List[AssociatedNumberResponse])
@monitor(name="api.associated_numbers.list")
async def list_associated_numbers(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    organization_id: str = Depends(get_organization_id),
):
    return [
        AssociatedNumberResponse(**doc)
        for doc in associated_number_service.list(
            organization_id=organization_id,
            skip=skip,
            limit=limit,
        )
    ]


@router.post("/associated-numbers", response_model=AssociatedNumberResponse, status_code=status.HTTP_201_CREATED)
@monitor(name="api.associated_numbers.create")
async def create_associated_number(
    body: AssociatedNumberCreate,
    organization_id: str = Depends(get_organization_id),
):
    try:
        doc = associated_number_service.create(
            organization_id=organization_id,
            phone_number=body.phone_number,
            user_id=body.user_id,
            label=body.label,
            is_active=body.is_active,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    return AssociatedNumberResponse(**doc)


@router.get("/associated-numbers/{associated_number_id}", response_model=AssociatedNumberResponse)
@monitor(name="api.associated_numbers.get")
async def get_associated_number(
    associated_number_id: str,
    organization_id: str = Depends(get_organization_id),
):
    doc = associated_number_service.get_by_id(associated_number_id, organization_id=organization_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Associated number not found")
    return AssociatedNumberResponse(**doc)


@router.put("/associated-numbers/{associated_number_id}", response_model=AssociatedNumberResponse)
@monitor(name="api.associated_numbers.update")
async def update_associated_number(
    associated_number_id: str,
    body: AssociatedNumberUpdate,
    organization_id: str = Depends(get_organization_id),
):
    updates = body.model_dump(exclude_unset=True)
    try:
        doc = associated_number_service.update(
            associated_number_id,
            organization_id=organization_id,
            updates=updates,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    if not doc:
        raise HTTPException(status_code=404, detail="Associated number not found")
    return AssociatedNumberResponse(**doc)


@router.delete("/associated-numbers/{associated_number_id}", status_code=status.HTTP_204_NO_CONTENT)
@monitor(name="api.associated_numbers.delete")
async def delete_associated_number(
    associated_number_id: str,
    organization_id: str = Depends(get_organization_id),
):
    deleted = associated_number_service.delete(associated_number_id, organization_id=organization_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Associated number not found")
    return None
