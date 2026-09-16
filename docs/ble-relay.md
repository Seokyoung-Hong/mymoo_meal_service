# BLE 중계 검증 (POST /relay)

리더(ESP32-C3)가 만든 서명 요청을 근로자 폰(또는 WiFi가 있으면 리더 자신)이 서버에 전달하고,
서버가 서명한 응답을 리더가 검증한다. 폰은 신뢰하지 않는다. 바이트를 옮길 뿐 만들거나 바꿀 수 없다.

서버 구현은 `app/routers/worker.py`의 `POST /relay`와 `GET /worker/qr`이다.

## 1. 키

| 이름 | 값 | 누가 아는가 |
|---|---|---|
| `K` | `SHA-256(scanner_key 원문)` | 리더(설정 QR로 받은 키에서 계산), 서버(`restaurants.scanner_key_hash`에 이미 있다) |
| `SERVER_SECRET` | 32바이트, 환경 변수 `RELAY_TOKEN_SECRET` | 서버만 |

새 키 발급·저장 없음. 스캐너 키 교체(`POST /restaurants/{id}/scanner-key`) 시 K도 같이 바뀐다.
`RELAY_TOKEN_SECRET`은 32바이트 hex 또는 임의 문자열(sha256해서 쓴다)이며, 비워 두면 프로세스마다
임시 키를 만든다 — 재시작하면 발급된 식권 토큰이 무효가 되므로 운영에서는 반드시 설정한다.

## 2. 인코딩

- 바이트 열은 JSON 안에서 base64url, 패딩 없음.
- 정수는 little-endian.
- HMAC은 HMAC-SHA256 32바이트 전체 (식권 토큰만 앞 16바이트 절단).

## 3. 식권 토큰 `tok` (36바이트) — `GET /worker/qr` 응답

```
tok = worker_uuid(16) ‖ exp(4, unix초) ‖ HMAC-SHA256(SERVER_SECRET, 0x01 ‖ worker_uuid ‖ exp)[:16]
```

- `exp = now + 60`. 앱은 60초마다 새로 받아 QR을 다시 그린다.
- 응답: `{"url": "https://.../meal/scan?worker=...", "tok": "<base64url 48자>", "ttl": 60}`.
  `url`은 구형 리더 호환으로 그대로 둔다. Keycloak sub가 UUID가 아닌 레거시 계정은 `tok`이 `null`이다.
- 앱이 QR에 넣는 문자열: `SK1.<sid base64url 11자>.<tok base64url 48자>` (64자).
  `sid`는 앱이 QR을 그릴 때마다 만드는 8바이트 난수이며 서버는 저장하지 않는다.

## 4. `POST /relay`

인증 없음(폰 브라우저·앱·리더 누구나 호출). 요청 본문 자체가 서명돼 있다.

```
요청  {"req":  "<base64url 87바이트>"}
응답  200 {"resp": "<base64url 62바이트>"}      ← 거절도 200 (status 필드로 구분)
      400 {"error": "..."}                       ← 길이·버전·base64 오류만
      429 {"error": "..."}                       ← IP당 초당 5회 초과
```

### 4-1. 요청 87바이트

| 오프셋 | 길이 | 필드 | 검증 |
|---:|---:|---|---|
| 0 | 1 | `ver` | `== 1` 아니면 400 |
| 1 | 2 | `rid` | 식당 ID. 이걸로 `scanner_key_hash`(=K) 조회. 없거나 비활성이면 `status=4` |
| 3 | 8 | `nonce` | 리더 난수. 응답에 그대로 복사 |
| 11 | 8 | `sid` | 세션 ID. 서버는 검사하지 않음(로그용) |
| 19 | 36 | `tok` | 3절 검증. 서명 불일치 `status=1`, 만료 `status=5` |
| 55 | 32 | `mac` | `HMAC-SHA256(K, req[0:55])` 불일치 → `status=4` |

### 4-2. 처리 순서

1. 요청 87바이트 전체가 같은 것이 최근 5분 안에 처리됐으면 저장된 응답을 그대로 반환한다
   (폰·리더 재전송 대비, 이중 결제 방지). 키는 `SHA-256(req)`.
2. `rid` → 식당·K. 없거나 mac 불일치 → `status=4`, 결제 없음.
3. `tok` 검증 → 근로자 user_id. 실패 시 `status=1` 또는 `5`. 미등록 근로자도 `status=1`.
4. 기존 `GET /scan`과 같은 결제 로직(`_charge_wallet`: 가격 정책 → 식대 버킷 차감 → 부족분 캐시 →
   `meal_ticket_usage_request` + 감사 로그 + SSE). 가격 정책 없음 `status=3`, 잔액 부족 `status=2`.
5. 응답 조립·서명, `SHA-256(req)` 키로 5분 저장.

### 4-3. 응답 62바이트

| 오프셋 | 길이 | 필드 | 값 |
|---:|---:|---|---|
| 0 | 1 | `ver` | 1 |
| 1 | 1 | `status` | 0 승인 · 1 무효 QR · 2 잔액 부족 · 3 가격 정책 없음 · 4 기기 인증 오류 · 5 QR 만료 |
| 2 | 8 | `nonce` | 요청의 nonce 그대로 |
| 10 | 4 | `meal_price` | 원. 승인 아니면 0 |
| 14 | 4 | `ticket_applied` | 식대 결제액 |
| 18 | 4 | `cash` | 캐시 결제액 |
| 22 | 8 | `txid` | `meal_ticket_usage_request.id`. 미승인이면 0 |
| 30 | 32 | `mac` | `HMAC-SHA256(K, resp[0:30])` |

`rid`를 찾지 못해 K를 모르면 mac은 0으로 채운다(리더는 "VERIFY ERR"). `rid`는 맞는데 요청 mac이
틀린 경우는 K를 아므로 `status=4` 응답에 정상 서명한다(리더는 "DEV AUTH"). 둘 다 결제는 없다.

## 5. 리더가 검증하는 것

- `len == 62`, `ver == 1`
- `resp.nonce == req.nonce` (다른 요청의 응답·과거 응답 재사용 차단)
- `HMAC-SHA256(K, resp[0:30]) == mac`

시각은 보지 않는다(리더에 시계 없음). 재사용 방지는 nonce가 담당한다.

## 6. 운영 메모

- `/relay`는 인증이 없으므로 IP당 초당 5회로 제한한다(프록시 뒤라 `X-Forwarded-For` 첫 홉을 본다).
- 재전송 캐시와 레이트 리밋은 프로세스 안 dict이다(SSE와 같은 uvicorn 워커 1개 전제). 워커를
  늘리면 Redis로 옮겨야 이중 결제를 계속 막을 수 있다.
- 로그 한 줄: `relay rid=.. sid=.. nonce=.. status=.. NNms` — 리더 시리얼 로그와 대조할 수 있다.
- 기존 `GET /scan?worker=`는 그대로 둔다. 구형 리더와 URL QR이 계속 쓴다.
- 리더가 WiFi에 붙어 있으면 `/relay`를 직접 호출한다(`X-Scanner-Key` 헤더를 보내도 서버는 무시한다).
  `/relay`가 404이면 리더는 BLE로 넘어간다.

## 7. 테스트 벡터

`tests/test_relay.py::test_document_vectors_match`가 이 값을 그대로 검증한다.

```
scanner_key   = 'test-scanner-key'
K             = 494c1391a37a92811eee3f889a38bf2917baa832884deb0bcc94571f8ec69c0b
SERVER_SECRET = 000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f
worker        = 2ab708e1-5dc7-4f54-abc4-b291f0a08dd1, exp = 1800000000
tok (36B)     = 2ab708e15dc74f54abc4b291f0a08dd100d2496bec53c5d872f2d788b47a106c992f2caa
rid = 1, nonce = 0102030405060708, sid = a1b2c3d4e5f60718
req (87B)     = 0101000102030405060708a1b2c3d4e5f607182ab708e15dc74f54abc4b291f0a08dd1
                00d2496bec53c5d872f2d788b47a106c992f2caa
                a318224af023fd24b16281b631387268db344abca5ac8461e7a76bf195dd10a6   ← req.mac
resp (62B)    = 01000102030405060708 28230000 28230000 00000000 0700000000000000
                8e257cae3e58a5cfb81d15e95acb2a140f3421e93ca9323622272e862e90d61d   ← resp.mac
                (status 0, price 9000, applied 9000, cash 0, txid 7, 모두 LE)
```
