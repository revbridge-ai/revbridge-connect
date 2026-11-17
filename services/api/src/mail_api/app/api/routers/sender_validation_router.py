"""
Sender validation router.

Utility endpoint for other services to check that a from_address can be used
by the authenticated user, without having to send a real email to find out.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from mail_api.shared.domain_validation import (
    get_user_domain_name_async,
    validate_authorized_sender,
)
from connect_lib.auth.hybrid import get_current_user_hybrid

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/me", tags=["me"])


@router.get(
    "/validate-sender",
    summary="Validate from_address",
    description=(
        "Returns 200 if `email` belongs to the authenticated user's domain "
        "or is an approved sender. Returns 400 otherwise. Does not send email."
    ),
)
async def validate_sender(
    email: str = Query(..., description="From address to validate"),
    current_user: dict = Depends(get_current_user_hybrid),
):
    is_valid = await validate_authorized_sender(email, current_user)
    if not is_valid:
        try:
            principal = await get_user_domain_name_async(current_user)
        except HTTPException:
            principal = None
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"from_address '{email}' does not belong to the user's domain"
                + (f" ({principal})" if principal else "")
                + " and is not registered in /approved-senders."
            ),
        )
    return {"email": email, "valid": True}
