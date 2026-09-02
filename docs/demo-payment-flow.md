# 데모: 식대 지갑 결제 흐름 (백엔드 정리)

데모에서 보여줄 것은 두 가지다. **돈이 어디서 어디로 흐르는지**(고객사 식대 → 근로자 지갑 → 식당 매출)와
**결제가 어떤 단계로 처리되는지**(QR 스캔 → 지갑 차감 → 부족분 캐시 결제 → 매출 반영).
이 문서는 그 흐름을 만드는 백엔드 요소와 API를 순서대로 정리한다.

관련 문서: [QR 스캐너 기기 연동 규칙](qr-scanner.md)

## 1. 결제 모델 요약

- **식대 지갑은 잔액 차감형**이다. 고객사 콘솔(또는 관리자)이 발급한 식대는 만료일이 있는
  버킷(`meal_ticket` 행, `amount`/`remaining_amount`)으로 쌓인다.
- 결제 시 **만료일이 빠른 버킷부터** 식사 금액만큼 차감한다. 여러 버킷에 걸쳐 차감되며 남은 금액은 소멸하지 않는다.
- 식대가 모자라면 **근로자의 현금성 캐시**(`cash_wallet`)에서 부족분을 결제한다. 캐시도 모자라면 409로 거부하고 아무것도 차감하지 않는다.
- 결제 1건은 `meal_ticket_usage_request` 행(`status=used`)으로 남고, 식사 금액·식대 결제액·캐시 결제액이 분리 기록된다.
- 모든 결제/발급/키 발급은 감사 로그(`audit_log`)에 before/after 잔액과 함께 남는다.

## 2. 등장 인물과 인증

| 역할 | 인증 | 데모에서 하는 일 |
|---|---|---|
| 관리자(고객사 콘솔 또는 운영자) | Bearer, admin | 식대 발급 |
| 근로자 | Bearer | 잔액 확인, QR 발급, (선택) 캐시 충전 |
| 식당 주인/매니저 | Bearer | 스캐너 키 발급, 결제, 매출·내역 확인 |
| QR 스캐너 기기 | `X-Scanner-Key` 헤더 | 근로자 QR의 URL을 GET |

## 3. 돈의 흐름 (데모 순서)

각 단계의 응답이 곧 "화면에 보이는 돈"이다. 응답은 모두 `{status, meta, data}` envelope이다.

### 3-1. 식대 발급 — 고객사 → 근로자 지갑

```
POST /admin/meal-allowances
{ "worker_user_ids": ["<근로자 user_id>"], "amount": 100000, "expires_on": "2026-09-30" }
→ 201  data[0]: { amount: 100000, remaining_amount: 100000, expires_on, status: "available" }
```

고객사 콘솔(siksabu_company)의 식대 할당 실행이 직원별로 이 API를 호출한다. 콘솔 없이 데모할 때는 포털의 "식대 발급"을 쓴다.

### 3-2. 근로자 지갑 확인

```
GET /worker/allowance/balance   → { balance: 100000 }        # 미만료 버킷 잔액 합계
GET /worker/tickets             → 버킷별 amount / remaining_amount / expires_on / status
GET /worker/qr                  → { url: "https://.../meal/scan?worker=<user_id>" }   # QR에 담을 URL
```

### 3-3. (선택) 캐시 충전 — 식대가 모자라는 장면용

```
POST /worker/cash/card-charges  { "amount": 5000, "card_last4": "1234" }  → 201
GET  /worker/cash/balance       → { balance: 5000 }
```

### 3-4. 결제 — 근로자 지갑 → 식당

QR 기기 경로(권장):

```
GET /scan?worker=<user_id>          헤더 X-Scanner-Key: <식당 스캐너 키>
→ 200  data: { meal_price: 8000, ticket_amount_applied: 8000, cash_amount_required: 0, status: "used", ... }
```

식당 주인 화면 경로(기기 없이 시연할 때, 금액 직접 지정 가능):

```
POST /restaurants/{id}/ticket-scans   { "worker_user_id": "<user_id>", "meal_price": 8000 }
→ 201  같은 형태
```

식대가 부족한 결제는 `ticket_amount_applied`와 `cash_amount_required`가 나뉘어 나오고, 캐시 내역에 부족분이 음수로 남는다.

### 3-5. 결제 직후 잔액 변화

```
GET /worker/allowance/balance       → { balance: 92000 }
GET /worker/ticket-usage-requests   → 결제 내역 (식당명, 금액, 식대/캐시 분리)
GET /worker/cash/transactions       → 부족분 결제가 있었다면 ticket_shortfall_payment 행
```

### 3-6. 식당 매출 확인 — 즉시 반영

```
GET /restaurants/{id}/revenue?date_from=2026-09-02&date_to=2026-09-02
→ { transaction_count, total_amount, allowance_amount, cash_amount, by_day: [ ... ] }
   # 기간 생략 시 오늘 하루

GET /restaurants/{id}/ticket-usage-requests?date_from=&date_to=   → 건별 상세
```

`allowance_amount`는 고객사 식대로 들어온 매출, `cash_amount`는 근로자 캐시로 들어온 매출이다.

## 4. 데모 전 준비 체크리스트

1. **배포**: `wallet-balance` 변경이 포함된 main을 배포한다. 컨테이너 시작 시 마이그레이션
   `c7a1e5b9d2f3`이 자동 적용된다(`remaining_amount`, `scanner_key_hash` 컬럼 추가, 대기 중 1회용 식권은 잔액으로 전환).
2. **식당 가격 정책**: `GET /scan`은 금액을 가격 정책에서 가져온다. 정책이 없으면 400. 포털의 "스캔 처리"는 금액을 직접 넣을 수 있다.
3. **스캐너 키**: 식당 주인이 `POST /restaurants/{id}/scanner-key`를 한 번 호출하고, 응답의 `scanner_key`를 기기 펌웨어의 `X-Scanner-Key`에 넣는다.
4. **근로자 계정**: 근로자 user_id(Keycloak sub)가 급식 서비스 `User`에 있어야 한다. 콘솔에서 직원 계정을 연결하면 같은 값이 쓰인다.
5. **`PUBLIC_BASE_URL`** (선택): QR URL의 베이스. 비우면 요청 base URL(`/meal` 포함)을 쓴다.

## 5. 테스트 포털 버튼 대응표 (`test-meal-web`)

| 화면 | 버튼 | API |
|---|---|---|
| 관리자 | 식대 발급 / 발급 내역 조회 | `POST/GET /admin/meal-allowances` |
| 근로자 | 잔액 조회 / 식대 버킷 목록 / 내 QR URL | `/worker/allowance/balance`, `/worker/tickets`, `/worker/qr` |
| 근로자 | mock 카드 충전 / 캐시 내역 / 잔액 조회 | `/worker/cash/*` |
| 근로자 | 사용 내역 | `GET /worker/ticket-usage-requests` |
| 식당 | 스캔 처리 | `POST /restaurants/{id}/ticket-scans` |
| 식당 | 오늘 매출 / 결제 내역 / 스캐너 키 발급(교체) | `/restaurants/{id}/revenue`, `.../ticket-usage-requests`, `.../scanner-key` |

## 6. 이번 범위에서 뺀 것

- 고객사 콘솔(siksabu_company)로의 결제 내역 동기화와 콘솔 정산 화면 연동.
- 1회용 식권 자가 등록, 대기 요청 → 식당 승인 흐름 (지갑 모델과 맞지 않아 제거).
- 근로자 앱의 QR 이미지 렌더링 (서버는 URL만 준다).
