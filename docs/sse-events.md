# 실시간 이벤트 (SSE)

결제·충전·식대 발급이 일어나면 서버가 Server-Sent Events로 즉시 알린다.
클라이언트는 폴링 대신 이 스트림을 열어 두고, 이벤트가 오면 화면을 갱신하면 된다.

## 엔드포인트

| 경로 | 누가 | 받는 이벤트 |
|---|---|---|
| `GET /meal/worker/events` | 근로자 (Bearer) | `payment`, `cash_charged`, `allowance_issued` |
| `GET /meal/restaurants/{restaurant_id}/events` | 식당 주인·매니저·관리자 (Bearer) | `payment` |

- 인증은 다른 API와 같은 `Authorization: Bearer <access token>` 헤더.
- 응답은 `Content-Type: text/event-stream`. 연결 직후 `: connected` 주석 한 줄이 오고,
  이후 25초마다 `: keep-alive` 주석이 온다 (프록시 유휴 끊김 방지). 주석 줄(`:`로 시작)은 무시하면 된다.
- 끊기면 클라이언트가 그냥 다시 GET 하면 된다. 끊긴 동안의 이벤트는 재전송하지 않으므로,
  재연결 직후 잔액/매출을 한 번 GET 해서 맞추는 것을 권장한다.

## 이벤트 형식

```
event: payment
data: {"usage_request": {...}, "allowance_balance": 2000, "cash_balance": 0}

```

`data`는 JSON 한 줄. 페이로드는 기존 REST 응답의 `data`와 같은 모양을 그대로 싣는다.

| event | 토픽 | data |
|---|---|---|
| `payment` | 근로자 | `usage_request` (= `POST /restaurants/{id}/ticket-scans` 응답 data), `allowance_balance`, `cash_balance` (결제 후 잔액) |
| `payment` | 식당 | `usage_request` (위와 동일) |
| `cash_charged` | 근로자 | `cash_balance` (충전 후 잔액), `transaction` (= `POST /worker/cash/card-charges` 응답 data) |
| `allowance_issued` | 근로자 | `ticket` (= `POST /admin/meal-allowances` 응답 data 원소). 식대 잔액은 `GET /worker/allowance/balance`로 재조회 |

## 확인 방법

```bash
curl -N -H "Authorization: Bearer $TOKEN" https://mymoo.quanect.kr/meal/worker/events
```

다른 터미널에서 카드 충전(`POST /worker/cash/card-charges`)이나 QR 스캔을 하면 위 스트림에 즉시 찍힌다.

## 운영 메모

- 이벤트는 프로세스 안 `asyncio.Queue`로 전달된다 (`app/utils/events.py`). uvicorn 워커가 1개인 현재 배포 전제이며,
  워커를 늘리면 Redis pub/sub으로 바꿔야 한다.
- 응답에 `X-Accel-Buffering: no`를 실어 nginx가 이 응답을 버퍼링하지 않게 했다. nginx `proxy_read_timeout`(기본 60초)은
  25초 keep-alive가 매번 초기화하므로 별도 설정이 필요 없다.
- 스트림을 여는 동안 DB 커넥션은 붙들지 않는다 (인증 확인 후 세션을 닫고 스트리밍 시작).
