# QR 스캐너 기기 연동 규칙

식당에 설치된 QR 기기가 근로자의 QR을 읽자마자 서버에 결제 요청을 보내는 방식이다.
기기는 QR에 담긴 URL을 그대로 GET 하고, 펌웨어에 고정된 헤더 하나만 덧붙인다.
서버 구현은 `app/routers/worker.py`의 `GET /scan`이며, 실행 중인 서버의 `/meal/docs`(OpenAPI)에서도 확인할 수 있다.

## 1. 근로자 QR 내용

근로자 앱이 `GET /worker/qr`(Bearer 인증)로 받은 `url` 값을 그대로 QR로 만든다.

```
{PUBLIC_BASE_URL}/scan?worker=<근로자 user_id>
예) https://api.example.com/meal/scan?worker=3f2a9c1e-...
```

- `PUBLIC_BASE_URL` 환경 변수가 비어 있으면 요청의 base URL(root_path `/meal` 포함)을 쓴다.
- `worker`는 Keycloak user_id(sub)이며, 고객사 콘솔이 직원에 연결해 둔 값과 같다.

## 2. 기기 요청 규칙 (펌웨어)

| 항목 | 값 |
|---|---|
| 메서드 | `GET` |
| URL | QR에 담긴 URL 그대로 (수정 금지) |
| 고정 헤더 | `X-Scanner-Key: <식당 스캐너 키>` |
| 본문 | 없음 |
| Bearer 토큰 | 없음 (스캐너 키가 인증 수단) |
| 선택 고정 쿼리 | `&meal_type=breakfast` / `lunch` / `dinner` (끼니 고정이 필요할 때만 URL 끝에 덧붙임) |

기기는 식당 ID를 알 필요가 없다. 서버가 스캐너 키로 식당을 찾는다.

## 3. 스캐너 키 발급

식당 주인 또는 매니저가 Bearer 인증으로 호출한다.

```
POST /restaurants/{restaurant_id}/scanner-key
→ 201 { "data": { "scanner_key": "...", "header": "X-Scanner-Key" } }
```

- 키 원문은 응답에 한 번만 나온다. 서버에는 sha256만 저장된다.
- 다시 호출하면 새 키가 발급되고 이전 키는 즉시 무효가 된다 (기기 분실·교체 시 사용).

## 4. 서버 처리와 응답

1. `X-Scanner-Key`로 활성 식당을 찾는다.
2. `worker`로 근로자를 찾는다.
3. 식당 가격 정책(`resolve_price`)으로 식사 금액을 정한다. 정책이 없으면 400.
4. 근로자 식대 지갑에서 만료일이 빠른 버킷부터 차감하고, 부족분은 현금성 캐시에서 결제한다.
5. 결제 내역(`meal_ticket_usage_request`)을 `used` 상태로 남기고 감사 로그를 기록한다.

| 상태 | 의미 | 기기 표시 예 |
|---|---|---|
| 200 | 결제 완료. `data.ticket_amount_applied`(식대), `data.cash_amount_required`(캐시) | 승인 |
| 400 | 식당 가격 정책 없음 | 설정 오류 |
| 401 | 헤더 없음 또는 잘못된 스캐너 키 | 기기 인증 오류 |
| 404 | 미등록 근로자 | 무효 QR |
| 409 | 식대·캐시 모두 부족 | 잔액 부족 |

응답 본문은 `{ "status": ..., "meta": ..., "data": {...} }` envelope이다.

## 5. 보안 메모

- QR에는 근로자 user_id만 들어 있고 비밀값은 없다. 결제는 유효한 스캐너 키를 가진 식당만 일으킬 수 있고, 그 식당이 결제 내역에 기록된다.
- 스캐너 키는 URL이 아니라 헤더로 보낸다. 접근 로그에 남지 않게 하기 위해서다.
- 기기 회수·분실 시 `POST /restaurants/{id}/scanner-key`를 다시 호출해 키를 교체한다.
