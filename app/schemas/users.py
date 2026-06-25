from datetime import datetime
from pydantic import BaseModel, ConfigDict


class UserRead(BaseModel):
    """사용자 등록 응답 모델입니다.

    Attributes:
        user_id (str): 사용자의 Keycloak ID
    """

    user_id: str


class UserCreate(BaseModel):
    user_id: str


class UserSchema(BaseModel):
    """사용자 정보를 나타내는 클래스입니다.

    Attributes:
        id (int): 사용자 db PK
        user_id (str): 사용자 keycloak id
        created_at (datetime): 사용자 생성 일시
    """

    model_config = ConfigDict(
        from_attributes=True,
        extra="allow",
    )
    id: int
    user_id: str
    created_at: datetime


class AdminUserSchema(UserSchema):
    """관리자 사용자 정보를 나타내는 클래스입니다.

    Attributes:
        global_admin (bool): 사용자가 글로벌 관리자 권한을 가지고 있는지 여부
        meal_admin (bool): 사용자가 식사 관리자 권한을 가지고 있는지 여부
        is_admin (bool): 사용자가 관리자 권한을 가지고 있는지 여부
    """

    global_admin: bool
    meal_admin: bool

    @property
    def is_admin(self) -> bool:
        """사용자가 관리자 권한을 가지고 있는지 여부를 반환합니다."""
        return self.global_admin or self.meal_admin
