# BLE 중계 검증 (POST /relay)

리더 펌웨어 v2 기준(2026-09-18). 바이트 포맷·상태 코드·`tok`은 v1 그대로다.

리더(ESP32-C3)가 만든 서명 요청을 근로자 폰(또는 WiFi가 있으면 리더 자신)이 서버에 전달하고,
서버가 서명한 응답을 리더가 검증한다. 폰은 신뢰하지 않는다. 바이트를 옮길 뿐 만들거나 바꿀 수 없다.

서버 구현은 `app/routers/worker.py`의 `POST /relay`와 `GET /worker/qr`이다.

## 1. 키

| 이름 | 값 | 누가 아는가 |
|---|---|---|
| `K` | `SHA-256(scanner_key 원문)` | 리더(USB로 주입), 서버(`restaurants.scanner_key_hash`에 이미 있다) |
| `SERVER_SECRET` | 32바이트, 환경 변수 `RELAY_TOKEN_SECRET` | 서버만 |

새 키 발급·저장 없음. 스캐너 키 교체(`POST /restaurants/{id}/scanner-key`) 시 K도 같이 바뀌며,
직전 키는 `scanner_key_hash_prev`에 유예로 남는다(§8).
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

1. **1단 멱등** — 요청 87바이트 전체가 같은 것이 최근 300초 안에 처리됐으면 저장된 응답을 그대로
   반환한다(폰·리더 재전송 대비). 키는 `SHA-256(req)`.
2. `rid` → 식당·K. 없거나 mac 불일치 → `status=4`, 결제 없음. mac은 현재 키와 교체 유예 키를
   둘 다 시도하고, **맞은 키로 응답에 서명한다**(§8).
3. `tok` 검증 → 근로자 user_id. 실패 시 `status=1` 또는 `5`. 미등록 근로자도 `status=1`.
4. **2단 멱등** — `(rid, worker_uuid)`로 최근 `RELAY_DECISION_TTL_SECONDS`(기본 90초) 안에 내린
   결정이 있으면 **결제하지 않고** 그 결정을 재사용한다(§4-4).
5. 기존 `GET /scan`과 같은 결제 로직(`_charge_wallet`: 가격 정책 → 식대 버킷 차감 → 부족분 캐시 →
   `meal_ticket_usage_request` + 감사 로그 + SSE). 가격 정책 없음 `status=3`, 잔액 부족 `status=2`.
   결과 다섯 값(`status`, `meal_price`, `ticket_applied`, `cash`, `txid`)을 결정으로 저장한다.
6. 응답 조립·서명, `SHA-256(req)` 키로 300초 저장.

### 4-3. 응답 62바이트

| 오프셋 | 길이 | 필드 | 값 |
|---:|---:|---|---|
| 0 | 1 | `ver` | 1 |
| 1 | 1 | `status` | 0 승인 · 1 무효 QR · 2 잔액 부족 · 3 가격 정책 없음 · 4 기기 인증 오류 · 5 QR 만료 · 6 이미 결제됨(재스캔) |
| 2 | 8 | `nonce` | 요청의 nonce 그대로 |
| 10 | 4 | `meal_price` | 원. 승인 아니면 0 |
| 14 | 4 | `ticket_applied` | 식대 결제액 |
| 18 | 4 | `cash` | 캐시 결제액 |
| 22 | 8 | `txid` | `meal_ticket_usage_request.id`. 미승인이면 0 |
| 30 | 32 | `mac` | `HMAC-SHA256(K, resp[0:30])` |

`rid`를 찾지 못해 K를 모르면 mac은 0으로 채운다(리더는 "VERIFY ERR"). `rid`는 맞는데 요청 mac이
틀린 경우는 K를 아므로 `status=4` 응답에 정상 서명한다(리더는 "DEV AUTH"). 둘 다 결제는 없다.

### 4-4. 2단 멱등 — 재스캔 이중 결제 차단

1단은 **완전히 같은 87바이트**만 막는다. 그런데 현장의 재시도는 이렇게 생겼다: 리더가 결과를 못 받아
`CHECK PHONE`을 띄우면 사장님이 QR을 다시 찍는다 → 앱이 새 `sid`·새 `tok`을 만들고 리더가 새 `nonce`를
뽑는다 → 87바이트가 통째로 달라 1단을 그냥 통과한다 → **한 끼에 두 번 청구된다.**

그래서 `tok` 검증으로 근로자를 알아낸 뒤, `(rid, worker_uuid)` 단위로도 최근 결정을 재사용한다.

- **이미 승인된 결정을 재사용하면 status를 6(DUPLICATE)으로 바꿔 준다.** 결제는 없고, 금액·txid는 첫 결제 것이다.
  리더는 OK가 아니라 DUPLICATE를 띄운다. 거절 결정(2·3)은 그대로 준다. 같은 87바이트 재전송(1단)은 원래 응답 그대로다.
- **응답은 언제나 이번 요청의 nonce로 다시 서명한다.** 저장된 응답 바이트를 그대로 주면 리더가
  `resp.nonce != req.nonce`로 거부한다(§5). 서버는 K를 아니까 재서명할 수 있다.
- 창 시작은 **첫 결정 시각**이다. 재사용해도 갱신하지 않으므로 90초 뒤에는 정상적으로 새 결제가 된다.
- `status=4`(기기 인증 오류)는 애초에 결제가 없으므로 2단과 무관하다.
- 실패한 결정(`2` 잔액 부족, `3` 가격 정책 없음)도 같이 저장된다(참조 구현과 동일). 잔액을 충전하거나
  가격 정책을 고쳐도 창이 지나야 다시 시도된다.
- **요청 제한이 아니다.** 429를 주지 않고 몇 번을 물어도 200으로 답한다. 리더가 응답을 받아야
  화면에 결과를 띄우기 때문이다. "90초에 한 번만 요청"이 아니라 "90초 안에는 몇 번 요청해도 한 번만 청구".

대가: 같은 근로자가 같은 식당에서 **90초 안에 진짜로 두 끼를 사면 두 번째가 청구되지 않는다**
(동료 몫 대리 결제). `RELAY_DECISION_TTL_SECONDS`로 조정한다. 짧게 하면 이 오탐이 줄고 이중 결제
위험이 는다. 재사용된 응답은 로그에 `REUSED`로 남으니 현장 빈도를 세어 값을 정하면 된다.

### 4-5. 인증 헤더는 받지 않는다

`/relay`와 `/health`는 `X-Scanner-Key`를 **받지 않는다**(있어도 무시, 요구하지 않음). 요청 본문이
이미 K로 서명돼 있고, 리더가 아직 TLS 인증서 검증을 생략하고 있어(`setInsecure`) 헤더로 키를 보내면
유출 경로만 남기 때문이다. 리더도 두 경로에서 헤더를 보내지 않도록 이미 고쳤다.

`GET /scan?worker=`는 그대로다. 구형 리더(`qr_config_curl`)와 URL QR이 계속 `X-Scanner-Key`로 인증한다.

## 5. 리더가 검증하는 것

- `len == 62`, `ver == 1`
- `resp.nonce == req.nonce` (다른 요청의 응답·과거 응답 재사용 차단)
- `HMAC-SHA256(K, resp[0:30]) == mac`

시각은 보지 않는다(리더에 시계 없음). 재사용 방지는 nonce가 담당한다.

## 6. 운영 메모

- `/relay`는 인증이 없으므로 IP당 초당 5회로 제한한다(프록시 뒤라 `X-Forwarded-For` 첫 홉을 본다).
- 재전송 캐시와 레이트 리밋은 프로세스 안 dict이다(SSE와 같은 uvicorn 워커 1개 전제). 워커를
  늘리면 Redis로 옮겨야 이중 결제를 계속 막을 수 있다.
- 로그 한 줄: `relay rid=.. sid=.. nonce=.. status=.. [REUSED] NNms` — 리더 시리얼 로그와 대조할 수
  있다. 1단에서 걸린 재전송은 `RESENT`로 남는다.
- `RELAY_DECISION_TTL_SECONDS`(기본 90) 로 2단 멱등 창을 조정한다.
- 기존 `GET /scan?worker=`는 그대로 둔다. 구형 리더와 URL QR이 계속 쓴다.
- 리더가 WiFi에 붙어 있고 직전 `/health` 핑이 성공했을 때만 `/relay`를 직접 호출한다. 재시도 0회,
  연결 800ms·응답 1200ms 타임아웃이라 서버가 느리면 즉시 BLE 경로로 넘어간다 — 장애 시 재시도 폭주가 없다.

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

## 8. 스캐너 키 교체 유예 (`scanner_key_hash_prev`)

키를 바꾸는 동안 기기가 먹통이 되는 것을 막는다.

- `POST /restaurants/{id}/scanner-key`는 새 키를 저장하면서 **직전 키를 `scanner_key_hash_prev`에 남긴다.**
- `/relay`의 요청 mac 검증과 `GET /scan`의 `X-Scanner-Key` 조회 모두 현재 키·직전 키를 둘 다 받아준다.
  `/relay` 응답은 **요청이 맞은 그 키로** 서명한다(리더가 아직 옛 키만 알고 있을 수 있다).
- **새 키로 결제가 한 번 성공하면 직전 키를 버린다.** 결제와 같은 트랜잭션에서 지워지므로,
  결제가 실패하면 유예도 그대로 남는다.

### 교체 순서 — 서버 먼저, 리더 나중

1. `POST /restaurants/{id}/scanner-key`로 새 키 발급 (서버가 `prev ← 현재`, `현재 ← 신규`)
2. USB 시리얼로 리더에 `key: <값>` 주입 (리더 부팅 후 120초 창 안에서만)
3. 새 키로 결제가 한 번 성공하면 서버가 유예를 폐기

2단계가 실패해도 리더는 옛 키로 계속 결제된다. 반대 순서로 하면 그 사이 결제가 전부 `status=4`로 거절된다.

> 리더 펌웨어에서 `DEFAULT_SCANNER_KEY` 상수가 삭제됐다. 키가 없는 리더는 `NO KEY`를 띄우고 결제를
> 거부하므로 **신규 기기는 설치 시 USB로 키를 심어야 한다.** 기존 기기는 NVS에 키가 있어 영향 없다.

## 9. 설정 QR (`SKC1`) — 리더 원격 설정

리더 설정(WiFi·모드·타임아웃·식당 ID)을 바꾸는 유일한 원격 경로다. **BLE 설정 채널은 만들지 않았다**
— 상시 광고로 연결 슬롯이 점유되면 결제가 전면 마비되기 때문이다. 대신 서버가 서명한 QR을 사장이
화면에 띄워 리더에 보여준다. 새 앱도, 새 서명 키도, 스토어 심사도 필요 없다.

### 9-1. 엔드포인트 (모두 Bearer 인증 + 식당 권한)

```
GET    /restaurants/{id}/readers                         등록된 리더 목록
POST   /restaurants/{id}/readers          {bt_mac, label?}   리더 등록(설치 시)
DELETE /restaurants/{id}/readers/{bt_mac}                 리더 삭제
POST   /restaurants/{id}/readers/{bt_mac}/config-qr
       {ssid?, password?, mode?, rid?, t_adv?, t_req?, t_rsp?}
       → {"qr": "SKC1.<base64url>.<base64url>", "ctr": 1789600000, "payload": "mode: ble"}
```

`bt_mac`은 `44:B1:76:17:36:68` / `44-b1-...` / `44b176173668` 모두 받아 소문자 hex 12자로 정규화한다.
리더 MAC은 설치 시 등록한다(리더가 결제할 때 보고하게 하려면 요청 포맷을 바꿔야 해서 지금은 안 한다).

### 9-2. 서명

```
KC   = HMAC-SHA256(K, "SKcfg1")          ← K 는 기존 scanner_key_hash
body = ver(1) ‖ ctr(4, LE) ‖ dev(6, 대상 리더 BT MAC) ‖ plen(1) ‖ payload(0..160)
mac16 = HMAC-SHA256(KC, body)[0:16]
QR   = "SKC1." + base64url(body) + "." + base64url(mac16)
```

- `KC`로 도메인을 분리하는 이유: 리더의 REQ 특성은 인증 없이 읽히고 그 안에 `HMAC(K, req[0:55])`가
  들어 있다. 같은 키로 설정에도 서명하면 그 서명을 재활용할 여지가 생긴다.
- `ctr`은 발급 시각의 unix 초다. 리더는 시각으로 해석하지 않고 **저장값보다 큰지만** 본다(재전송 차단).
  서버는 `ctr = max(now, 직전 ctr + 1)`로 정해 같은 초에 두 번 발급해도 값이 반드시 커지게 한다.

### 9-3. payload와 역할

줄 단위 텍스트(`키: 값`, `\n` 구분). 순서는 `ssid`, `password`, `mode`, `rid`, `t_adv`, `t_req`, `t_rsp`.

| 필드 | 뜻 | 허용 |
|---|---|---|
| `ssid` / `password` | 리더가 붙을 WiFi | 매니저 · 사장 · 관리자 |
| `mode` | `auto`(WiFi 있으면 서버 직접) / `ble`(항상 중계) | 매니저 · 사장 · 관리자 |
| `t_adv` / `t_req` / `t_rsp` | 단계별 마감 ms (1000~20000) | 매니저 · 사장 · 관리자 |
| `rid` | 식당 ID | **사장(owner)·관리자만** |
| `key` | 스캐너 키 | **어떤 역할도 불가.** 스키마에 필드가 없고 리더도 QR 경로에서 영구 거부한다 |

서버가 반드시 지키는 것:

1. **폰·브라우저가 준 자유 텍스트에 서명하지 않는다.** 타입된 필드만 받아 서버가 payload를 조립한다.
2. 역할 화이트리스트를 서버에서 강제한다(매니저가 `rid`를 넣으면 403).
3. `ssid`/`password`에 개행이 들어오면 거부한다(줄 단위 payload 주입 방지).
4. 발급 이력을 감사 로그에 남긴다. **`password` 값은 남기지 않는다**(필드명만).

### 9-4. 리더가 하는 검증 (순서대로, 하나라도 실패하면 NVS를 건드리지 않는다)

1. 길이·`ver`·`plen` 일관성 → 조용히 폐기
2. `dev == 자기 BT MAC` → 조용히 폐기
3. `ctr > 저장된 cfgctr` → 조용히 폐기(재전송 차단)
4. `mac16` 상수시간 비교 → 실패 시 `CFG DENIED`, 30초 쿨다운
5. 통과하면 `cfgctr`을 먼저 저장한 뒤 payload 적용

### 9-5. 테스트 벡터

`tests/test_reader_config.py`가 그대로 검증한다.

```
scanner_key = 'test-scanner-key'
K           = 494c1391a37a92811eee3f889a38bf2917baa832884deb0bcc94571f8ec69c0b
KC          = 05e22022caa116e85dc4ce83eecaba8370f01412f765ad97ad14130f9d83c1ac
dev         = 44b176173668      ctr = 1789600000      payload = 'mode: ble'
body (21B)  = 010021ab6a44b176173668096d6f64653a20626c65
mac16       = e1e6e3471fb7bc73cbd0e7ddc67d9943
QR          = SKC1.AQAhq2pEsXYXNmgJbW9kZTogYmxl.4ebjRx-3vHPL0Ofdxn2ZQw
```

### 9-6. 화면

테스트 포털(`test-meal-web`)의 식당주인 화면에 리더 등록·목록·삭제, 설정 QR 폼(QR 이미지는
브라우저에서 생성 — WiFi 비밀번호가 외부로 나가지 않는다), 그리고 **오늘 승인 목록**이 있다.
리더가 `CHECK PHONE`으로 끝난 거래는 리더도 근로자도 결과를 모르므로 서버 로그가 유일한 장부다.
