"""User authorization helpers."""

from app.schemas.users import AdminUserSchema
from app.models.user import User


async def check_admin_user(user: User) -> AdminUserSchema:
    """Return admin flags extracted from the already-validated JWT claims."""
    return AdminUserSchema(
        id=user.id,
        user_id=user.user_id,
        global_admin=bool(getattr(user, "auth_global_admin", False)),
        meal_admin=bool(getattr(user, "auth_meal_admin", False)),
        created_at=user.created_at,
    )
