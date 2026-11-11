# BLE 중계 검증 v2 (POST /relay, ver=2)

2026-11-11 확정. 서버와 리더 펌웨어(kiosk_c6 · ble_relay)가 이 문서와 §8 테스트 벡터를 기준으로 맞춘다.
v1(docs/ble-relay.md)은 기존 리더용으로 계속 동작한다. 같은 `POST /relay`에서 첫 바이트(ver)로 갈린다.

서버 구현: `app/services/relay_v2.py`(포맷·암호), `app/routers/worker.py`(`POST /relay` v2 분기,
`GET /relay/keys`, `GET /worker/qr`의 `qr2`), `app/routers/readers.py`(리더 공개키 등록).
테스트: `tests/test_relay_v2.py`.

## 0. 발명설명자료와의 대응

| 발명설명자료 | v2 구현 |
|---|---|
| ① QR에 이용권 식별정보·일회성 세션ID·BLE 식별정보·nonce·유효시간·서버 서명 | QR = `SK2.<body‖Ed25519 서명>.<ble_id>`. body에 ticket_id·qn(nonce)·iat·exp. ble_id(일회성 세션/BLE 식별자)는 앱이 QR마다 생성 |
| ④ QR의 BLE 식별정보로 그 폰을 자동 탐색·연결 | 리더가 QR의 ble_id를 광고에 실은 폰만 연결(앱·펌웨어 담당, 서버 무관) |
| ⑤⑥ 리더 인증정보·nonce를 포함한 서명된 검증 요청 | 리더 기기별 Ed25519 서명 + rnonce + reader_id |
| ⑦ 폰이 내용 열람·변조 불가하게 중계 | eph X25519 + ChaCha20-Poly1305로 암호화. 폰은 못 읽고, 바꾸면 복호화 실패 |
| ⑧⑨ 서버가 유효성·중복사용 검증, 서명된 응답 | QR 서명·만료, 2단 멱등, 오프라인 기록 중복·시간 검사(온라인 결제와 교차). 응답에 서버 Ed25519 서명 |
| ⑩⑪ 리더가 서버 공개키로 직접 검증, nonce·거래ID·Reader ID·이용권 ID 결합 | 응답 평문에 rnonce·txid·reader_id·ticket_id·qn 포함, 서버 공개키로 서명 검증 |
| B 종속항: 직접 경로 우선, 실패 시 오프라인 검증 | WiFi 직접 → BLE 중계 → RTC 기반 오프라인 승인(§6, 기기 키에 묶인 1회용 토큰) |

## 1. 알고리즘

- 서명: Ed25519 (RFC 8032, SHA-512). monocypher는 `crypto_ed25519_*`를 쓴다(기본 `crypto_eddsa_*`는 BLAKE2b라 호환 안 됨).
- 키 합의: X25519. KDF: HKDF-SHA256. AEAD: ChaCha20-Poly1305 (IETF, 12바이트 nonce, 16바이트 tag).
- 정수는 little-endian. JSON 안의 바이트는 base64url(패딩 없음).
- 서명 대상 앞에 문맥 문자열을 붙인다: QR `"SK2qr"`, 리더 요청 `"SK2req"`, 서버 응답 `"SK2rsp"`.

## 2. 키

| 이름 | 무엇 | 누가 가짐 |
|---|---|---|
| SPK | 서버 Ed25519 서명키 | 서버 개인키. 리더는 공개키 `sign_pub` |
| SEK | 서버 X25519 암호키 | 서버 개인키. 리더는 공개키 `enc_pub` |
| RK | 리더 Ed25519 키쌍 | 리더가 기기 안에서 생성(개인키는 밖으로 안 나감). 서버에 공개키 등록 |

- 서버 키는 `RELAY_TOKEN_SECRET`에서 유도한다: `seed = HMAC-SHA256(SECRET, "SK2sign")`, `enc = HMAC-SHA256(SECRET, "SK2enc")`.
  비밀이 같으면 재시작해도 공개키가 같다. **운영에서 `RELAY_TOKEN_SECRET`을 바꾸면 모든 리더에 공개키를 다시 넣어야 한다.**
- `kid = 1` 고정. 키 교체가 필요해지면 kid를 올리고 두 키를 함께 받게 한다(지금은 없음).
- 설치 절차
  1. `GET /relay/keys` → `{"kid":1,"sign_pub":"<hex64>","enc_pub":"<hex64>"}` (인증 없음)
  2. USB로 리더에 `sign_pub`·`enc_pub`·`kid` 주입, 리더 공개키(hex64) 읽기
  3. `POST /restaurants/{id}/readers {"bt_mac":"..","pubkey":"<hex64>"}` (Bearer, 식당 권한) → 응답의 `id`가 **reader_id**
  4. USB로 리더에 reader_id 주입
  - 공개키 변경은 감사 로그 `restaurant_reader.pubkey.set`에 남는다. 기존 리더는 같은 POST로 pubkey만 넣으면 된다.

## 3. QR (`GET /worker/qr`의 `qr2`)

```
body (34B) = ver(1)=2 ‖ kid(1)=1 ‖ ticket_id(16) ‖ qn(8) ‖ iat(4) ‖ exp(4)
qn         = HMAC-SHA256(SECRET, 0x02 ‖ ticket_id ‖ iat)[:8]
sig (64B)  = Ed25519(SPK, "SK2qr" ‖ body)
qr_signed  = body ‖ sig                      (98B)
qr2        = "SK2." + b64url(qr_signed)      (135자)
QR 문자열  = qr2 + "." + b64url(ble_id)       ← 앱이 붙인다
```

- ticket_id = 근로자 UUID(Keycloak sub) 16바이트. sub가 UUID가 아닌 레거시 계정은 `qr2 = null`.
- exp = iat + 60. 앱은 `ttl`(60초)마다 다시 받아 그린다.
- qn은 서버가 저장하지 않아도 다시 계산할 수 있다(오프라인 기록 진위 확인용).
- ble_id는 서명 밖이다. 길이·광고 형식은 앱 담당이 정하고 리더는 그것을 따른다. 서버는 보지 않는다.

## 4. 요청 (리더 → 폰/직접 → `POST /relay {"req": b64url}`)

```
header (34B) = ver(1)=2 ‖ kid(1)=1 ‖ eph_pub(32)
shared       = X25519(eph_priv, enc_pub)
okm (64B)    = HKDF-SHA256(ikm=shared, salt=eph_pub ‖ enc_pub, info="SK2 relay v2")
k_req, k_rsp = okm[0:32], okm[32:64]

body  = reader_id(4) ‖ rnonce(16) ‖ rtc_time(4) ‖ qr_signed(98) ‖ n(1) ‖ record × n (36B씩)
inner = body ‖ Ed25519(RK, "SK2req" ‖ header ‖ body)
req   = header ‖ ChaCha20-Poly1305(k_req, nonce=0×12, aad=header, inner)   ← ct ‖ tag16
```

- 길이 = 237 + 36n. **n ≤ 7** (489B, BLE 속성 최대 512B). 그 밖은 400.
- eph는 요청마다 새로 만든다. rnonce는 요청마다 새 난수.
- rtc_time은 리더 RTC 값(unix 초, UTC). 서버는 로그용으로만 본다.
- **온라인 경로에서는 리더가 RTC로 exp를 미리 거르지 않는다.** 판단은 서버가 한다(RTC가 틀어져도 결제가 막히지 않게).

오프라인 기록 (36B, §6):

```
record = ticket_id(16) ‖ iat(4) ‖ qn(8) ‖ used_at(4) ‖ seq(4)
```

## 5. 응답 (`{"resp": b64url}`, 166B)

```
plain (74B) = ver(1)=2 ‖ status(1) ‖ rnonce(16) ‖ reader_id(4) ‖ ticket_id(16) ‖ qn(8)
              ‖ txid(8) ‖ meal_price(4) ‖ ticket_applied(4) ‖ cash(4) ‖ server_time(4) ‖ ack_seq(4)
signed      = plain ‖ Ed25519(SPK, "SK2rsp" ‖ plain)
n_rsp       = 응답마다 새 12바이트 난수
resp        = n_rsp ‖ ChaCha20-Poly1305(k_rsp, nonce=n_rsp, aad=0x02, signed)   ← 12+138+16 = 166B
```

- 응답 nonce를 고정하지 않는 이유: 재전송 캐시(300초)가 지난 뒤 같은 요청이 다시 오면 서버는 같은 k_rsp로
  다른 평문(status·server_time)을 암호화하게 된다. 고정 nonce면 키스트림이 재사용된다. 요청은 eph가
  매번 새 키를 만들므로 nonce 0을 그대로 쓴다.
- 166B ≤ 182B(MTU 185)이므로 한 번 쓰기로 간다.

| status | 뜻 | 리더 화면 |
|---:|---|---|
| 0 | 승인 | OK |
| 1 | QR 서명 불일치·미등록 근로자 | INVALID |
| 2 | 잔액 부족 | NO FUNDS |
| 3 | 가격 정책 없음 | NO PRICE |
| 4 | 리더 미등록·공개키 없음·리더 서명 불일치·식당 비활성 | DEV AUTH |
| 5 | QR 만료 | EXPIRED |

- 거절도 200. 400은 base64·길이·ver/kid·eph_pub 오류(저차 점)·**복호화 실패(폰이 바꿈)** 뿐이다. 429는 IP당 초당 5회 초과.
- ticket_id·qn은 요청 QR의 바이트를 그대로 돌려준다. 승인이 아니면 금액·txid는 0.
- 리더 검증 순서: 길이 166 → 앞 12바이트를 nonce로 k_rsp 복호화 → SPK 서명 → ver==2 → rnonce·reader_id·ticket_id·qn이 보낸 값과 같음.
  하나라도 틀리면 VERIFY ERR.
- server_time은 서명돼 있으므로 리더는 이 값으로만 RTC를 보정한다(NTP는 편의용, 보안 판단에 쓰지 않음).

### 멱등 (v1과 같음)

- 1단: 요청 바이트 전체가 같으면 300초 안에는 저장된 응답을 그대로 준다.
- 2단: `(식당, 근로자)`로 `RELAY_DECISION_TTL_SECONDS`(90초) 안의 결정을 재사용한다. 재스캔해도 한 번만 청구.
  v1·v2가 같은 표를 쓰므로 두 버전을 섞어 찍어도 한 번만 청구된다.

## 6. 오프라인 검증 모드 (RTC)

직접 경로와 BLE 중계가 모두 실패하면 리더는 결제하지 않고 `NO NETWORK`/`CHECK PHONE`을 띄운다.
**오프라인 승인은 오프라인 토큰(`SK2O`, §6-1)과 기기 서명(§6-2)으로만 한다.** 온라인 QR(`SK2`)은 오프라인으로
승인하지 않는다(기기 서명이 없어 스크린샷 하나로 여러 리더를 돌 수 있다). 망이 없으면 앱이 SK2O를 보여 준다.

1. SK2O 서명·RTC 유효기간·로컬 중복표(NVS 보존)·기기 서명을 모두 통과하면 `OFFLINE OK`, 기록 저장
   (seq는 기기 단조 증가, NVS 보존).
2. 다음 온라인 요청(직접이든 BLE든)에 오래된 것부터 seq 오름차순으로 최대 7건을 실어 보낸다.
   응답이 검증되면 **이번에 보낸 기록 중** `seq ≤ ack_seq`인 것을 지운다(안 보낸 기록은 지우지 않는다).

### 6-1. 오프라인 토큰 (`SK2O`) — 폰도 오프라인일 때

온라인 QR은 60초짜리라, 폰이 망을 잃으면 1분 뒤에는 보여줄 QR이 없다. 그래서 앱이 온라인일 때
오프라인 전용 1회용 토큰을 미리 받아 둔다.

```
GET /worker/offline-tokens   (Bearer, 근로자)
→ {"tokens": ["SK2O.<b64url>", ...], "exp": <unix초>}

body (66B) = ver(1)=2 ‖ kid(1)=1 ‖ ticket_id(16) ‖ qn(8) ‖ iat(4) ‖ exp(4) ‖ dev_pub(32)  ← SK2 배치 + 기기 공개키
qn         = HMAC-SHA256(SECRET, 0x03 ‖ ticket_id ‖ iat)[:8]                   ← 도메인 0x03
sig        = Ed25519(SPK, "SK2off" ‖ body)                                      ← 같은 SPK, 다른 문맥
QR 문자열  = "SK2O." + b64url(body66 ‖ sig64) + "." + b64url(ble_id)       ← 130B
```

- 창(`OFFLINE_TOKEN_TTL_SECONDS`, 기본 43200 = 12시간, UTC 기준 정렬) 단위로 **결정적**이다. 같은 창에서
  다시 받아도 같은 토큰이다. 창당 `OFFLINE_TOKENS_PER_WINDOW`(기본 2)장. 토큰 i의 `iat = 창 시작 − i`,
  `exp = 창 끝`(모두 같음). 근로자가 동시에 쥘 수 있는 토큰은 최대 4장(현재 + 직전 창)이고,
  **이 장수가 오프라인 청구 한도다.** 금액 한도는 따로 없다(서버가 정산 시 실제 가격을 청구, 잔액 부족이면 `insufficient`).
- 기기 키가 등록되지 않은 근로자는 토큰을 받지 못한다(409). 등록은 §6-2.
- 앱: 온라인일 때 받아 두고, 망이 없으면 아직 안 쓴 토큰을 QR로 보여 준다. 쓴 토큰은 앱이 알 수 없으므로
  순서대로 보여 주고 사용자가 넘길 수 있게 한다(리더가 로컬 중복표로 같은 토큰 재사용을 거부).

리더 규칙:

- `SK2O`는 **연결 여부와 상관없이 언제나 오프라인 승인 경로**로 간다. 서버 `/relay`는 SK2O를 QR로 받지 않는다
  (`SK2qr` 문맥 서명이 아니므로 status 1).
- 검사: `"SK2off"` 문맥으로 sign_pub 서명(body 66B), ver·kid, RTC 기준 `iat ≤ now ≤ exp`, 로컬 중복표 `(ticket_id, qn)`,
  직전 90초 온라인 승인, 그리고 **기기 서명(§6-2)**.
- 기록 36B는 그대로다. qn·iat 자리에 토큰의 qn·iat를 넣는다. 서버는 qn 도메인(0x02/0x03)으로 종류를 안다.

서버 정산 차이: 토큰 기록은 `used_at ∈ [iat − 300, iat + ttl + 장수 + 300]`이면 유효하고, 이미 결제됐는지는
`used_at ± 90초` 안의 같은 식당 결제로 본다(온라인 QR은 QR 유효 시간 ± 90초).

### 6-2. 기기 키 바인딩 — 토큰 스크린샷 재사용 차단

- 앱이 폰 안에서 Ed25519 키쌍을 만들고 공개키를 `POST /worker/device-key {"pubkey": "<hex64>"}`(Bearer, 204)로 등록한다.
  근로자당 하나이며, 다시 보내면 교체된다(감사 로그 `worker.device_key.set`). 교체하면 이전 키로 받은 토큰은 쓸 수 없다.
- 서버는 토큰 body에 dev_pub를 넣어 서명한다. 리더는 오프라인 승인 직전 기존 BLE 세션(같은 GATT)으로 도전한다.

```
dev_req  (29B) = 0x03 ‖ rnonce(16) ‖ reader_id(4) ‖ qn(8)          ← 리더가 REQ에 올림, qn은 그 토큰의 qn
dev_resp (65B) = 0x03 ‖ Ed25519(dev_priv, "SK2dev" ‖ dev_req)       ← 폰이 RESP에 씀(서버로 보내지 않음)
```

- 앱: REQ가 29B이고 `[0] == 3`이면 서버로 중계하지 않는다. **qn이 지금 보여 준 자기 토큰의 qn과 같을 때만** 서명한다
  (남의 리더가 임의 qn 서명을 받아 가는 오라클 방지). 그 밖의 길이(v1 87, v2 237..489)는 기존대로 중계한다.
- 리더: 65B이고 `[0] == 3`이며 토큰 dev_pub로 서명이 맞아야 승인. 서명 불일치 `DEV KEY`, 5초 안에 응답 없음 `NO PHONE`.
- 서버는 기기 서명을 다시 보지 않는다(기록은 리더 서명으로 신뢰). 남는 위험은 폰 자체를 가진 사람뿐이다.

### 6-3. 서버 정산 (`relay_offline_record` 테이블)

구형 펌웨어가 SK2 오프라인 기록을 올려도 아래 규칙으로 정산한다(0x02 도메인). 새 펌웨어는 SK2O 기록(0x03)만 올린다.

- 리더 서명이 맞은 요청의 기록만 본다. `(reader_id, seq)`가 이미 있으면 건너뛴다(재업로드 안전).
- `invalid`(청구 없음):
  - `qn ≠ HMAC(SECRET, 0x02‖ticket_id‖iat)[:8]`(서버가 발급한 QR이 아님)
  - 미등록 근로자
  - `used_at`이 `[iat − 300, exp + 300]` 밖(RTC가 크게 틀렸거나 기록 조작)
- `duplicate`(청구 없음):
  - 같은 `(ticket_id, qn)` 기록이 이미 있음(같은 QR을 두 리더에서 쓴 경우)
  - 같은 식당에서 그 근로자가 `[iat − 90, exp + 90]` 안에 이미 결제됨(온라인 승인 응답이 유실된 뒤 오프라인 재승인,
    재스캔한 새 QR의 오프라인 승인, 리더가 온라인 승인 QR을 오프라인 기록으로 한 번 더 올리는 경우).
    온라인 2단 멱등과 같은 정책이라, 그 창 안의 진짜 두 번째 식사는 청구되지 않는다.
- 그 밖에는 `used_at` 날짜로 결제 → `charged` / `insufficient` / `no_price`. 감사 로그 action `meal_ticket_usage_request.ble_offline`.
- 오프라인 기록은 온라인 결정보다 먼저 정산된다. ack_seq = 이번 요청 기록 중 가장 큰 seq(없으면 0).
- 한계: 같은 QR을 두 리더가 동시에 올리는 경합은 막지 않는다(드묾, `worker.py`의 ponytail 주석).

## 7. 운영 메모

- 재전송 캐시·2단 결정·레이트 리밋은 v1과 같은 프로세스 내 dict다(uvicorn 워커 1개 전제).
- 알려진 한계(v1부터): 레이트 리밋 키가 X-Forwarded-For 첫 홉이라 프록시가 덮어쓰지 않으면 위조된다.
  `enc_pub`가 공개라 누구나 복호화되는 요청을 만들 수 있어, 리더 서명 검증 전 DB 조회 1~2회까지는 비용을 쓴다.
- 로그: `relay v2 digest=<8B> status=.. [REUSED] NNms`, `relay offline reader=.. seq=.. status=..`.
  요청이 암호화돼 있어 프록시·폰 로그에는 아무 내용도 남지 않는다.

## 8. 테스트 벡터

`tests/test_relay_v2.py::test_document_vectors_match`가 그대로 검증한다. Ed25519·X25519는 결정적이므로
펌웨어는 아래 고정 키로 같은 바이트가 나와야 한다.

```
SECRET        = 000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f
SPK seed      = d55b69b26c9255ff81b1f409d7474278f21419aa1aa4f7da109959c4c6550138
sign_pub      = 20d27176801f39e3ff3617d0531a8539eca617c3b0e1638873d5af3784595ba7
SEK priv      = 998d7d2f1d21557809564e9295feb5aee3e5a19dd5bfc3d1811afe52870c0481
enc_pub       = 4f945e9aa340b05550dac55fb8c874a6e4cdbd633bebaaf88be7db40fa6d4476

ticket_id     = 2ab708e1-5dc7-4f54-abc4-b291f0a08dd1, iat = 1800000000, exp = 1800000060
qn            = bc1999459330123f
qr_signed     = 02012ab708e15dc74f54abc4b291f0a08dd1bc1999459330123f00d2496b3cd2496b
                66fa7f9a03b6f3478829004ad38c75d1dd2a30190d7f41c225bb119e0bcb532d36ae
                97e40cb5d7dddf1cccf396b4561b6f1cc41e33ef3b2ae4a04fcaaa174e03
qr2           = SK2.AgEqtwjhXcdPVKvEspHwoI3RvBmZRZMwEj8A0klrPNJJa2b6f5oDtvNHiCkAStOMddHdKjAZDX9BwiW7EZ4Ly1MtNq6X5Ay1193fHMzzlrRWG28cxB4z7zsq5KBPyqoXTgM

RK seed       = 1111111111111111111111111111111111111111111111111111111111111111
reader_pub    = d04ab232742bb4ab3a1368bd4615e4e6d0224ab71a016baf8520a332c9778737
eph priv      = 2222222222222222222222222222222222222222222222222222222222222222
eph_pub       = 0faa684ed28867b97f4a6a2dee5df8ce974e76b7018e3f22a1c4cf2678570f20
reader_id = 1, rnonce = 000102…0f (16B), rtc_time = 1800000005
offline (n=1) = 2ab708e15dc74f54abc4b291f0a08dd188d1496bdf21689da362abd79cd1496b01000000
                (iat 1799999880, qn df21689da362abd7, used_at 1799999900, seq 1)
k_rsp         = 825ceee8d681d403f4c855ca841667306782cbee853d2a3db2076b4b5642e420
req (273B)    = 02010faa684ed28867b97f4a6a2dee5df8ce974e76b7018e3f22a1c4cf2678570f20
                75183483bc37ea75a6a8864c77d548f1a003e013539f4b47c2c03f29e0f836391d0a
                0ba2372da531f819d655c8c2fa9a3129c141e8e8ce664cbe9610798b8e363e15126a
                f13b4e8430505417cd4bc8f7303acf469b99f59a9caa528363a56e462f5ee0c7974b
                b86b3a8ccfd86544adf2799629cdd0f547d44ba5cad55c5229807a9682abe477fb00
                d8e0ccc4d9c22071e9844d432be568a0dac4b8fbf8a7967f011a5c714fbda109fab3
                2b0f9c1a7bb54feb9f8eb720ea6b2c2d18b8b3e0b30c7bf495769d912b6768507fee
                60b88b9aef81d9a4c4ca282ee6c925a68575c5feef4293947d3672183e3b9796469d
                c9

응답: status 0, txid 7, price 9000, applied 9000, cash 0, server_time 1800000003, ack_seq 1
plain (74B)   = 0200000102030405060708090a0b0c0d0e0f010000002ab708e15dc74f54abc4b291
                f0a08dd1bc1999459330123f070000000000000028230000282300000000000003d2
                496b01000000
오프라인 토큰: now = 1800000100, ttl 43200, 2장 → 창 1799971200..1800014400
dev seed      = 4444444444444444444444444444444444444444444444444444444444444444
dev_pub       = d759793bbc13a2819a827c76adb6fba8a49aee007f49f2d0992d99b825ad2c48
token[0] raw  = 02012ab708e15dc74f54abc4b291f0a08dd10afebba62f6a66f88061496b400a4a6b
  (130B)        d759793bbc13a2819a827c76adb6fba8a49aee007f49f2d0992d99b825ad2c480b93
                72e4d4695e84ecb64f61a5ad78198d4c6b933bca961ceb5bbf3c48fd959ba9c5d59b
                e22e3ce7345c66979a5a986c9cb3e052310b731741787106206c1b07
                (qn 0afebba62f6a66f8, iat 1799971200, exp 1800014400)
token[0]      = SK2O.AgEqtwjhXcdPVKvEspHwoI3RCv67pi9qZviAYUlrQApKa9dZeTu8E6KBmoJ8dq22-6ikmu4Af0ny0JktmbglrSxIC5Ny5NRpXoTstk9hpa14GY1Ma5M7ypYc61u_PEj9lZupxdWb4i485zRcZpeaWphsnLPgUjELcxdBeHEGIGwbBw
dev_req (29B) = 03000102030405060708090a0b0c0d0e0f010000000afebba62f6a66f8   (rnonce 00..0f, reader_id 1)
dev_resp(65B) = 03dbe7fad47ae04387b13b16ff020999974ac3df6b93d83e34a0a2de6edeb2e7302b
                6b56a3a6ce0d9c4ede2be9192a63f1740f3e7baedc388a02a0c404113bf505

n_rsp         = 333333333333333333333333   (벡터용 고정값, 실제로는 난수)
resp (166B)   = 3333333333333333333333332204a97267523e288f97d0d4f47505438740a3f8b798
                c6bc7ea9d750d5262cd37dd0b4f6986ba39d8c0d5de5a3488e2915f470871514fae7
                136ad42c7fe7047c1059c82c8bb0f300807cfa6dea57344fc20712e85afa4cd165d6
                97b32498b322cb911f27ca806f17751b9a828ba4377a5a4e7ac08fb13701a2a0c5c4
                33ee66ef2822671e3888dc8aa12b0ef08d93eac5ca7ab1c5915d6d0ba177
```
