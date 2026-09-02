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

### 3-1. CLI로 발급하기 (운영자, 서비스 계정 토큰)

배포 서버의 `siksabu_infra/.env`에 있는 서버 간 호출용 confidential client
(`MEAL_SERVICE_CLIENT_ID` / `MEAL_SERVICE_CLIENT_SECRET`, DEPLOYMENT.md §4-3)는
`meal_admin` client role을 가지므로 모든 식당의 키를 발급할 수 있다. `jq`가 필요하다.

```bash
# 0. 값 준비 (배포 서버라면 .env에서 그대로 읽는다)
set -a; . siksabu_infra/.env; set +a
BASE=https://mymoo.quanect.kr/meal
KC=https://auth.quanect.kr/realms/Mymoo/protocol/openid-connect/token

# 1. 서비스 계정 토큰
TOKEN=$(curl -s -X POST "$KC" \
  -d grant_type=client_credentials \
  -d client_id="$MEAL_SERVICE_CLIENT_ID" \
  -d client_secret="$MEAL_SERVICE_CLIENT_SECRET" | jq -r .access_token)

# 2. 식당 ID 확인 (응답 data에서 id·name을 본다)
curl -s "$BASE/restaurants/" | jq .

# 3. 키 발급 — scanner_key는 이 응답에서만 볼 수 있다
curl -s -X POST "$BASE/restaurants/<RESTAURANT_ID>/scanner-key" \
  -H "Authorization: Bearer $TOKEN" | jq .data
```

응답 예:

```json
{ "scanner_key": "Qm9...", "header": "X-Scanner-Key" }
```

`scanner_key` 값을 기기 펌웨어의 고정 헤더 값으로 넣는다.

### 3-2. CLI로 발급하기 (식당 주인 본인 계정)

식당 주인이 직접 하려면 Keycloak 사용자 토큰이 필요하다. `mymoo-test-web` 클라이언트에
**Direct Access Grants**가 켜져 있어야 password grant가 동작한다 (Keycloak 관리 콘솔 →
Clients → mymoo-test-web → Capability config).

```bash
TOKEN=$(curl -s -X POST "$KC" \
  -d grant_type=password -d client_id=mymoo-test-web \
  -d username="<keycloak 아이디>" -d password="<비밀번호>" | jq -r .access_token)

curl -s "$BASE/restaurants/mine" -H "Authorization: Bearer $TOKEN" | jq .   # 내 식당 id
curl -s -X POST "$BASE/restaurants/<RESTAURANT_ID>/scanner-key" \
  -H "Authorization: Bearer $TOKEN" | jq .data
```

Direct Access Grants가 꺼져 있으면 테스트 포털(`/test-web/`)에 로그인해
식당 화면의 "스캐너 키 발급(교체)" 버튼을 쓴다.

### 3-3. 발급 확인

```bash
# 401이면 키가 틀린 것, 404면 근로자 미등록, 200이면 결제까지 실행되므로 실제 근로자 id로는 시험하지 말 것
curl -s -i "$BASE/scan?worker=probe" -H "X-Scanner-Key: <발급받은 키>" | head -1
```

`probe`처럼 존재하지 않는 근로자 id로 호출하면 키가 맞을 때 404, 틀릴 때 401이 나온다.

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
