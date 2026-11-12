"""BLE 중계 v2 바이트 포맷과 암호 — docs/ble-relay-v2.md.

DB를 모르는 순수 함수만 둔다. 결제 처리는 ``app/routers/worker.py``의 ``POST /relay``가 한다.

- 서버 서명: Ed25519. 리더는 서버 공개키만 가진다(QR·응답 검증).
- 요청 암호화: 리더가 eph X25519로 서버 공개키와 키를 합의해 ChaCha20-Poly1305로 감싼다.
  중계 폰은 내용을 읽을 수도 바꿀 수도 없다.
- 리더 인증: 기기마다 Ed25519 키쌍. 공개키는 설치 시 서버에 등록한다.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct
from dataclasses import dataclass
from uuid import UUID

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

VERSION = 2
KID = 1
QR_PREFIX = "SK2"

QR_BODY_LEN = 34
SIG_LEN = 64
QR_SIGNED_LEN = QR_BODY_LEN + SIG_LEN
HEADER_LEN = 34
TAG_LEN = 16
RNONCE_LEN = 16
OFFLINE_RECORD_LEN = 36
OFFLINE_MAX = 7  # 요청 전체가 BLE 속성 최대 512바이트 안에 들어가야 한다.
INNER_FIXED_LEN = 4 + RNONCE_LEN + 4 + QR_SIGNED_LEN + 1
REQUEST_MIN_LEN = HEADER_LEN + INNER_FIXED_LEN + SIG_LEN + TAG_LEN  # 237
RESPONSE_PLAIN_LEN = 74
AEAD_NONCE_LEN = 12
RESPONSE_LEN = AEAD_NONCE_LEN + RESPONSE_PLAIN_LEN + SIG_LEN + TAG_LEN  # 166

CTX_QR = b"SK2qr"
CTX_REQ = b"SK2req"
CTX_RSP = b"SK2rsp"
CTX_OFFLINE = b"SK2off"
CTX_DEVICE = b"SK2dev"  # 리더 도전값에 폰이 서명할 때(서버는 쓰지 않음, 문서·테스트용)
CTX_SETUP = b"SK2setup"
REG_PREFIX = "SK2R:"
SETUP_PREFIX = "SK2S"
REG_LEN = 6 + 32
SETUP_PAYLOAD_MAX = 255
DEVICE_CHALLENGE = 0x03
OFFLINE_PREFIX = "SK2O"
HKDF_INFO = b"SK2 relay v2"
_ZERO_NONCE = bytes(12)


class RelayV2Error(ValueError):
    """복호화·형식 오류. 엔드포인트는 400으로 돌려준다."""


@dataclass(frozen=True)
class ServerKeys:
    """서버 서명키(Ed25519)와 암호키(X25519). 둘 다 ``RELAY_TOKEN_SECRET``에서 유도한다."""

    sign: Ed25519PrivateKey
    enc: X25519PrivateKey

    @classmethod
    def from_secret(cls, secret: bytes) -> ServerKeys:
        """같은 비밀이면 언제나 같은 키쌍. 재시작해도 리더에 심은 공개키가 유지된다."""

        def seed(label: bytes) -> bytes:
            return hmac.new(secret, label, hashlib.sha256).digest()

        return cls(
            sign=Ed25519PrivateKey.from_private_bytes(seed(b"SK2sign")),
            enc=X25519PrivateKey.from_private_bytes(seed(b"SK2enc")),
        )

    @property
    def sign_pub(self) -> bytes:
        """리더에 주입하는 서명 공개키(32바이트)."""
        return raw_public(self.sign.public_key())

    @property
    def enc_pub(self) -> bytes:
        """리더에 주입하는 암호 공개키(32바이트)."""
        return raw_public(self.enc.public_key())


@dataclass(frozen=True)
class Ticket:
    """QR에 담긴 서명된 이용권."""

    ticket_id: UUID
    qn: bytes
    iat: int
    exp: int


@dataclass(frozen=True)
class OfflineRecord:
    """리더가 서버에 닿지 못할 때 RTC로 승인하고 남긴 기록(36바이트)."""

    ticket_id: UUID
    iat: int
    qn: bytes
    used_at: int
    seq: int


@dataclass(frozen=True)
class Request:
    """복호화한 요청. ``signed``는 리더 서명 대상, ``signature``는 그 서명이다."""

    reader_id: int
    rnonce: bytes
    rtc_time: int
    qr_signed: bytes
    offline: tuple[OfflineRecord, ...]
    signed: bytes
    signature: bytes
    k_rsp: bytes


@dataclass(frozen=True)
class Response:
    """응답 평문 필드. 거절이면 금액·txid는 0."""

    status: int
    rnonce: bytes
    reader_id: int
    ticket_id: bytes
    qn: bytes
    server_time: int
    txid: int = 0
    meal_price: int = 0
    ticket_applied: int = 0
    cash: int = 0
    ack_seq: int = 0


def raw_public(key: Ed25519PublicKey | X25519PublicKey) -> bytes:
    """공개키 원시 32바이트."""
    return key.public_bytes(Encoding.Raw, PublicFormat.Raw)


QN_ONLINE = 0x02
QN_OFFLINE = 0x03


def ticket_qn(
    secret: bytes, ticket_id: UUID, iat: int, domain: int = QN_ONLINE
) -> bytes:
    """QR nonce. 서버가 저장하지 않고도 오프라인 기록의 진위를 다시 계산할 수 있다.

    ``domain``으로 온라인 QR(0x02)과 오프라인 토큰(0x03)을 가른다. 기록에는 종류가 없으므로
    서버는 두 값을 모두 계산해 어느 쪽인지 알아낸다.
    """
    mac = hmac.new(
        secret,
        bytes([domain]) + ticket_id.bytes + struct.pack("<I", iat),
        hashlib.sha256,
    )
    return mac.digest()[:8]


def _signed_ticket(  # noqa: PLR0913
    keys: ServerKeys,
    secret: bytes,
    ticket_id: UUID,
    iat: int,
    ttl: int,
    *,
    domain: int,
    context: bytes,
    extra: bytes = b"",
) -> bytes:
    body = (
        struct.pack("<BB", VERSION, KID)
        + ticket_id.bytes
        + ticket_qn(secret, ticket_id, iat, domain)
        + struct.pack("<II", iat, iat + ttl)
        + extra
    )
    return body + keys.sign.sign(context + body)


def make_qr(
    keys: ServerKeys, secret: bytes, ticket_id: UUID, iat: int, ttl: int
) -> str:
    """``SK2.<b64url(body‖sig)>``. 앱이 뒤에 ``.<ble_id>``를 붙여 QR을 그린다."""
    signed = _signed_ticket(
        keys, secret, ticket_id, iat, ttl, domain=QN_ONLINE, context=CTX_QR
    )
    return f"{QR_PREFIX}.{b64url(signed)}"


def make_offline_tokens(  # noqa: PLR0913
    keys: ServerKeys,
    secret: bytes,
    ticket_id: UUID,
    now: int,
    ttl: int,
    count: int,
    device_pub: bytes,
) -> list[str]:
    """오프라인 전용 1회용 토큰 ``SK2O.<b64url(body66‖sig)>`` (docs/ble-relay-v2.md §6-1·6-2).

    body는 QR body 34B 뒤에 근로자 폰의 기기 공개키 32B를 붙인 66B다. 리더는 그 키로 폰에게
    도전값 서명을 받아야 승인하므로, 토큰 스크린샷만으로는 쓸 수 없다.

    창(``ttl``초) 단위로 결정적이다. 같은 창에서 다시 받아도 같은 토큰이 나오므로, 근로자가 동시에
    쥘 수 있는 토큰은 현재 창과 직전 창을 합쳐 최대 ``2 × count``장이다.
    ``iat``는 창 시작에서 1초씩 앞당겨 토큰마다 qn이 다르게 한다.
    """
    base = now - now % ttl
    return [
        f"{OFFLINE_PREFIX}."
        + b64url(
            _signed_ticket(
                keys,
                secret,
                ticket_id,
                base - index,
                ttl + index,
                domain=QN_OFFLINE,
                context=CTX_OFFLINE,
                extra=device_pub,
            )
        )
        for index in range(count)
    ]


def read_qr(keys: ServerKeys, qr_signed: bytes) -> Ticket | None:
    """서명이 맞으면 Ticket, 아니면 None. 만료 판단은 호출자가 한다."""
    body, sig = qr_signed[:QR_BODY_LEN], qr_signed[QR_BODY_LEN:]
    if body[0] != VERSION or body[1] != KID:
        return None
    try:
        keys.sign.public_key().verify(sig, CTX_QR + body)
    except InvalidSignature:
        return None
    iat, exp = struct.unpack("<II", body[26:34])
    return Ticket(ticket_id=UUID(bytes=body[2:18]), qn=body[18:26], iat=iat, exp=exp)


def _session_keys(shared: bytes, eph_pub: bytes, enc_pub: bytes) -> tuple[bytes, bytes]:
    okm = HKDF(
        algorithm=SHA256(), length=64, salt=eph_pub + enc_pub, info=HKDF_INFO
    ).derive(shared)
    return okm[:32], okm[32:]


def open_request(keys: ServerKeys, req: bytes) -> Request:
    """요청을 복호화해 필드를 꺼낸다. 리더 서명 검증은 등록된 공개키로 호출자가 한다."""
    extra = len(req) - REQUEST_MIN_LEN
    if (
        extra < 0
        or extra % OFFLINE_RECORD_LEN
        or extra // OFFLINE_RECORD_LEN > OFFLINE_MAX
    ):
        raise RelayV2Error(f"v2 요청 길이 오류: {len(req)}")
    header = req[:HEADER_LEN]
    if header[0] != VERSION or header[1] != KID:
        raise RelayV2Error("지원하지 않는 ver/kid")
    eph_pub = header[2:HEADER_LEN]
    try:
        shared = keys.enc.exchange(X25519PublicKey.from_public_bytes(eph_pub))
    except ValueError as exc:  # 저차 점(공유키 0) — 인증 없이 500을 내지 않게
        raise RelayV2Error("eph_pub 오류") from exc
    k_req, k_rsp = _session_keys(shared, eph_pub, keys.enc_pub)
    try:
        inner = ChaCha20Poly1305(k_req).decrypt(_ZERO_NONCE, req[HEADER_LEN:], header)
    except InvalidTag as exc:
        raise RelayV2Error("복호화 실패") from exc

    reader_id, rnonce, rtc_time = (
        struct.unpack("<I", inner[0:4])[0],
        inner[4:20],
        struct.unpack("<I", inner[20:24])[0],
    )
    qr_signed = inner[24 : 24 + QR_SIGNED_LEN]
    count = inner[24 + QR_SIGNED_LEN]
    if count != extra // OFFLINE_RECORD_LEN:
        raise RelayV2Error("오프라인 기록 수 불일치")
    start = INNER_FIXED_LEN
    offline = tuple(
        _read_offline(
            inner[start + i * OFFLINE_RECORD_LEN : start + (i + 1) * OFFLINE_RECORD_LEN]
        )
        for i in range(count)
    )
    body_end = len(inner) - SIG_LEN
    return Request(
        reader_id=reader_id,
        rnonce=rnonce,
        rtc_time=rtc_time,
        qr_signed=qr_signed,
        offline=offline,
        signed=CTX_REQ + header + inner[:body_end],
        signature=inner[body_end:],
        k_rsp=k_rsp,
    )


def _read_offline(raw: bytes) -> OfflineRecord:
    iat, used_at, seq = (
        struct.unpack("<I", raw[16:20])[0],
        *struct.unpack("<II", raw[28:36]),
    )
    return OfflineRecord(
        ticket_id=UUID(bytes=raw[:16]), iat=iat, qn=raw[20:28], used_at=used_at, seq=seq
    )


def reader_signature_ok(pubkey: bytes, request: Request) -> bool:
    """등록된 리더 공개키로 요청 서명(헤더 포함)을 검증한다."""
    try:
        Ed25519PublicKey.from_public_bytes(pubkey).verify(
            request.signature, request.signed
        )
    except (InvalidSignature, ValueError):
        return False
    return True


def seal_response(
    keys: ServerKeys, k_rsp: bytes, response: Response, nonce: bytes | None = None
) -> bytes:
    """응답 평문에 서버 서명을 붙이고 k_rsp로 암호화한다: ``nonce(12) ‖ ct ‖ tag`` (166B).

    nonce는 응답마다 새 난수다. 재전송 캐시가 사라진 뒤 같은 요청이 다시 오면 같은 k_rsp로
    다른 평문을 암호화하게 되므로, 고정 nonce를 쓰면 키스트림이 재사용된다.
    ``nonce``는 테스트 벡터용으로만 넘긴다.
    """
    plain = (
        struct.pack("<BB", VERSION, response.status)
        + response.rnonce
        + struct.pack("<I", response.reader_id)
        + response.ticket_id
        + response.qn
        + struct.pack(
            "<QIIIII",
            response.txid,
            response.meal_price,
            response.ticket_applied,
            response.cash,
            response.server_time,
            response.ack_seq,
        )
    )
    signed = plain + keys.sign.sign(CTX_RSP + plain)
    nonce = nonce or os.urandom(AEAD_NONCE_LEN)
    return nonce + ChaCha20Poly1305(k_rsp).encrypt(nonce, signed, bytes([VERSION]))


def seal_request(  # noqa: PLR0913
    enc_pub: bytes,
    eph: X25519PrivateKey,
    reader_key: Ed25519PrivateKey,
    *,
    reader_id: int,
    rnonce: bytes,
    rtc_time: int,
    qr_signed: bytes,
    offline: tuple[OfflineRecord, ...] = (),
) -> tuple[bytes, bytes]:
    """리더 쪽 요청 조립(테스트·문서 벡터용). (요청, k_rsp)를 돌려준다."""
    eph_pub = raw_public(eph.public_key())
    header = struct.pack("<BB", VERSION, KID) + eph_pub
    body = (
        struct.pack("<I", reader_id)
        + rnonce
        + struct.pack("<I", rtc_time)
        + qr_signed
        + bytes([len(offline)])
        + b"".join(
            r.ticket_id.bytes
            + struct.pack("<I", r.iat)
            + r.qn
            + struct.pack("<II", r.used_at, r.seq)
            for r in offline
        )
    )
    inner = body + reader_key.sign(CTX_REQ + header + body)
    shared = eph.exchange(X25519PublicKey.from_public_bytes(enc_pub))
    k_req, k_rsp = _session_keys(shared, eph_pub, enc_pub)
    return header + ChaCha20Poly1305(k_req).encrypt(_ZERO_NONCE, inner, header), k_rsp


def open_response(sign_pub: bytes, k_rsp: bytes, resp: bytes) -> bytes:
    """리더 쪽 응답 검증(테스트용). 서명까지 맞으면 74바이트 평문을 돌려준다."""
    nonce, sealed = resp[:AEAD_NONCE_LEN], resp[AEAD_NONCE_LEN:]
    signed = ChaCha20Poly1305(k_rsp).decrypt(nonce, sealed, bytes([VERSION]))
    plain, sig = signed[:RESPONSE_PLAIN_LEN], signed[RESPONSE_PLAIN_LEN:]
    Ed25519PublicKey.from_public_bytes(sign_pub).verify(sig, CTX_RSP + plain)
    return plain


def parse_reg_code(reg: str) -> tuple[str, bytes]:
    """리더 OLED의 등록 QR ``SK2R:<base32(bt_mac6‖reader_pub32)>``을 (bt_mac hex, 공개키)로.

    서명이 없어 위조할 수 있다. 서버는 관리자 인증으로만 이 값을 믿는다.
    """
    text = reg.strip().upper()
    if not text.startswith(REG_PREFIX):
        raise RelayV2Error("등록 QR은 SK2R: 로 시작해야 합니다.")
    code = text[len(REG_PREFIX) :]
    try:
        raw = base64.b32decode(code + "=" * (-len(code) % 8))
    except ValueError as exc:
        raise RelayV2Error("등록 QR base32 오류") from exc
    if len(raw) != REG_LEN:
        raise RelayV2Error(f"등록 QR 길이 오류: {len(raw)}")
    return raw[:6].hex(), raw[6:]


def make_setup_qr(  # noqa: PLR0913
    keys: ServerKeys,
    bt_mac: str,
    reader_pub: bytes,
    reader_id: int,
    ctr: int,
    payload: bytes = b"",
) -> str:
    """서명된 설치 QR ``SK2S.<b64url(body‖sig)>`` (kiosk_c6 SETUP_QR.md §3).

    body = ver ‖ kid ‖ bt_mac(6) ‖ reader_pub(32) ‖ reader_id(4LE) ‖ ctr(4LE) ‖ plen ‖ payload.
    """
    if len(payload) > SETUP_PAYLOAD_MAX:
        raise RelayV2Error(f"설치 payload는 {SETUP_PAYLOAD_MAX}바이트 이하여야 합니다.")
    body = (
        struct.pack("<BB", VERSION, KID)
        + bytes.fromhex(bt_mac)
        + reader_pub
        + struct.pack("<IIB", reader_id, ctr, len(payload))
        + payload
    )
    return f"{SETUP_PREFIX}.{b64url(body + keys.sign.sign(CTX_SETUP + body))}"


def b64url(raw: bytes) -> str:
    """패딩 없는 base64url."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
