"""이 모듈은 레스토랑 관련 데이터 스키마를 정의합니다.

Pydantic BaseModel을 사용하여 데이터 유효성 검사를 수행합니다.
"""

from datetime import datetime
from typing import Dict, Literal, Optional, Self, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.utils.times import get_datetime_by_string


EstablishmentType: TypeAlias = Literal[
    "student",
    "fixed_menu_restaurant",
    "fixed_korean_buffet",
    "variable_korean_buffet",
]

ESTABLISHMENT_TYPE_DESCRIPTION = (
    "식당 유형("
    "student=교내 학생식당|"
    "fixed_menu_restaurant=고정메뉴일반식당|"
    "fixed_korean_buffet=고정메뉴형 한식뷔페|"
    "variable_korean_buffet=메뉴 변경형 한식뷔페)"
)

BUFFET_ESTABLISHMENT_TYPES = {
    "fixed_korean_buffet",
    "variable_korean_buffet",
}


class TimeRange(BaseModel):
    """시간 범위를 나타내는 클래스입니다.

    Attributes:
        start (str | datetime): 시작 시간 ("HH:MM" 형식 또는 datetime 객체)
        end (str | datetime): 종료 시간 ("HH:MM" 형식 또는 datetime 객체)
    """

    start: str | datetime  # "HH:MM" 형식
    end: str | datetime  # "HH:MM" 형식

    def to_datetime(self):
        """start와 end 속성을 문자열에서 datetime 객체로 변환합니다."""
        if isinstance(self.start, str):
            self.start = get_datetime_by_string(self.start)
        if isinstance(self.end, str):
            self.end = get_datetime_by_string(self.end)

    def to_string(self):
        """start와 end 속성을 datetime 객체에서 문자열로 변환합니다."""
        if isinstance(self.start, datetime):
            self.start = self.start.strftime("%H:%M")
        if isinstance(self.end, datetime):
            self.end = self.end.strftime("%H:%M")


class Location(BaseModel):
    """레스토랑의 위치를 나타내는 클래스입니다.

    Attributes:
        is_campus (bool): 캠퍼스 내 위치 여부
        building (Optional[str]): 건물 이름
        map_links (Optional[Dict[str, str]]): 네이버, 카카오 지도 링크
        latitude (Optional[float]): 위도
        longitude (Optional[float]): 경도
    """

    is_campus: bool
    building: Optional[str] = None
    map_links: Optional[Dict[str, str]] = None  # 네이버, 카카오 지도 링크
    latitude: Optional[float] = None
    longitude: Optional[float] = None


class RestaurantSchema(BaseModel):
    """레스토랑의 기본 정보를 나타내는 클래스입니다.

    Attributes:
        name (str): 레스토랑 이름
        establishment_type (EstablishmentType): 레스토랑 유형
        price (Optional[int]): 1인분 가격(원)
        location (Optional[Location]): 위치 정보
        opening_time (Optional[TimeRange]): 영업 시간
        break_time (Optional[TimeRange]): 휴식 시간
        breakfast_time (Optional[TimeRange]): 아침 식사 시간
        lunch_time (Optional[TimeRange]): 점심 시간
        dinner_time (Optional[TimeRange]): 저녁 시간
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    establishment_type: EstablishmentType
    price: Optional[int] = Field(default=None, gt=0)
    location: Optional[Location] = None
    opening_time: Optional[TimeRange] = None
    break_time: Optional[TimeRange] = None
    breakfast_time: Optional[TimeRange] = None
    lunch_time: Optional[TimeRange] = None
    dinner_time: Optional[TimeRange] = None


class RestaurantResponse(RestaurantSchema):
    """GET /restaurants/{id} 및 /restaurants 엔드포인트 응답 바디를 나타내는 클래스입니다.

    Attributes:
        id (int): 레스토랑 ID
        owner (Optional[int]): 소유자 ID
    """

    id: int
    owner: Optional[int] = None
    owner_user_id: Optional[str] = None
    is_active: bool = True


class RestaurantStatusUpdateRequest(BaseModel):
    """PATCH /restaurants/{id}/status 요청 바디를 나타내는 클래스입니다."""

    is_active: bool


class RestaurantManagerRequest(BaseModel):
    """POST /restaurants/{id}/managers 요청 바디를 나타내는 클래스입니다."""

    user_id: str


class RestaurantManagerResponse(BaseModel):
    """식당 관리자 응답 바디를 나타내는 클래스입니다."""

    user_id: str


class RestaurantRequest(RestaurantSchema):
    """POST /restaurants/requests 엔드포인트 요청 바디를 나타내는 클래스입니다."""

    @model_validator(mode="after")
    def validate_price_for_establishment_type(self) -> Self:
        """한식 뷔페 유형에는 1인분 가격을 필수로 강제합니다."""
        if self.establishment_type in BUFFET_ESTABLISHMENT_TYPES and self.price is None:
            raise ValueError(
                "fixed_korean_buffet 및 variable_korean_buffet 식당은 price 필드가 필수입니다."
            )
        return self


class RestaurantCreateRequest(RestaurantRequest):
    """POST /restaurants/ 엔드포인트 요청 바디를 나타내는 클래스입니다."""

    owner_user_id: str


class RestaurantUpdateRequest(RestaurantRequest):
    """PATCH /restaurants/{id} 엔드포인트 요청 바디를 나타내는 클래스입니다."""

    owner_user_id: Optional[str] = None


class RestaurantSubmission(RestaurantSchema):
    """레스토랑 제출 정보를 나타내는 클래스입니다.

    Attributes:
        status (Literal["pending", "approved", "rejected"]): 제출 상태
        submitter (int): 제출자 ID
        submitted_time (datetime): 제출 시간
        id (int): 제출 ID
        reviewed_time (Optional[datetime]): 검토 시간
        reviewer (Optional[int]): 검토자 ID
        rejection_message (Optional[str]): 거절 메시지
    """

    status: Literal["pending", "approved", "rejected"] = "pending"
    submitter: int
    submitted_time: datetime
    id: int
    reviewed_time: Optional[datetime] = None
    reviewer: Optional[int] = None
    rejection_message: Optional[str] = None


class SubmissionResponse(BaseModel):
    """제출 응답을 나타내는 클래스입니다.

    Attributes:
        status (str): 제출 상태
        request_id (int): 요청 ID
        message (Optional[str]): 응답 메시지
    """

    status: str = "pending"
    request_id: int
    message: Optional[str] = "등록 요청이 성공적으로 접수되었습니다."


class ApproverResponse(BaseModel):
    """승인 응답을 나타내는 클래스입니다.

    Attributes:
        status (str): 승인 상태
        restaurant_id (int): 레스토랑 ID
        message (Optional[str]): 응답 메시지
    """

    status: str = "approved"
    restaurant_id: int
    message: Optional[str] = "등록 요청이 승인되었습니다."


class UserSchema(BaseModel):
    """사용자 정보를 나타내는 클래스입니다.

    Attributes:
        id (int): 사용자 ID
        name (str): 사용자 이름
        email (str): 사용자 이메일
        global_admin (bool) = 전역 관리자 여부
        service_account (bool) = 서비스 API 계정 여부
        created_at (datetime): 생성 시간
    """

    id: int
    name: str
    email: str
    global_admin: bool = False
    service_account: bool = False
    created_at: datetime

    class Config:
        """정의되지 않은 필드도 허용합니다."""

        extra = "allow"


class RejectRestaurantRequest(BaseModel):
    """레스토랑 요청 거절 메시지를 나타내는 클래스입니다.

    Attributes:
        message (str): 거절 메시지
    """

    message: str
