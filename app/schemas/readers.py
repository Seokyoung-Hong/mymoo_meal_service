"""Restaurant BLE reader registration and config QR schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.base import Timestamp as Tsp


Timestamp = Annotated[datetime, Tsp]

TIMEOUT_MIN_MS = 1000
TIMEOUT_MAX_MS = 20000

TimeoutMs = Annotated[int, Field(ge=TIMEOUT_MIN_MS, le=TIMEOUT_MAX_MS)]


class ReaderCreate(BaseModel):
    """Register a reader for a restaurant, or relabel one already registered."""

    bt_mac: str
    label: str | None = None
    # relay v2 리더 서명 공개키(Ed25519 32바이트, hex 64자). 설치 시 USB로 뽑아 넣는다.
    # 생략하면 기존 값을 유지한다.
    pubkey: Annotated[str, Field(pattern=r"^[0-9a-fA-F]{64}$")] | None = None


class ReaderResponse(BaseModel):
    """Registered reader row."""

    id: int
    bt_mac: str
    label: str | None = None
    pubkey: str | None = None
    last_config_ctr: int
    last_config_at: Timestamp | None = None
    created_at: Timestamp


class ReaderConfigCreate(BaseModel):
    """Typed reader settings the server assembles into a signed config QR.

    필드 선언 순서가 그대로 payload 줄 순서다. 폰/브라우저가 준 자유 텍스트에는 서명하지
    않으므로 ``extra="forbid"``로 임의 필드(``key`` 등)를 422로 막는다.
    """

    model_config = ConfigDict(extra="forbid")

    ssid: str | None = None
    password: str | None = None
    mode: Literal["auto", "ble"] | None = None
    rid: int | None = None
    t_adv: TimeoutMs | None = None
    t_req: TimeoutMs | None = None
    t_rsp: TimeoutMs | None = None

    @field_validator("ssid", "password")
    @classmethod
    def reject_newlines(cls, value: str | None) -> str | None:
        """Reject newlines that would inject extra lines into the signed payload.

        Args:
            value (str | None): 검증할 ssid 또는 password 값.

        Returns:
            str | None: 개행이 없는 원래 값.

        Raises:
            ValueError: 값에 캐리지 리턴이나 라인 피드가 들어 있을 때.
        """
        if value is not None and ("\n" in value or "\r" in value):
            raise ValueError("ssid/password에는 개행을 넣을 수 없습니다.")
        return value


def _reject_newlines(value: str | None) -> str | None:
    if value is not None and ("\n" in value or "\r" in value):
        raise ValueError("ssid/password에는 개행을 넣을 수 없습니다.")
    return value


class ReaderSetupWifi(BaseModel):
    """Wi-Fi settings signed into the setup QR payload."""

    model_config = ConfigDict(extra="forbid")

    ssid: Annotated[str, Field(min_length=1, max_length=64)]
    password: Annotated[str, Field(max_length=64)] = ""

    _no_newlines = field_validator("ssid", "password")(_reject_newlines)


class ReaderSetupCreate(BaseModel):
    """Registration QR scanned from an unset reader's OLED (kiosk_c6 SETUP_QR.md)."""

    model_config = ConfigDict(extra="forbid")

    reg: Annotated[str, Field(max_length=128)]
    label: str | None = None
    wifi: ReaderSetupWifi | None = None


class ReaderSetupResponse(BaseModel):
    """Server-signed setup QR for the phone to show to the reader."""

    reader_id: int
    bt_mac: str
    setup_qr: str


class ReaderConfigQrResponse(BaseModel):
    """Signed config QR string with the counter and payload it commits to."""

    qr: str
    ctr: int
    payload: str
