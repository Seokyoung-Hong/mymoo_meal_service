# Mymoo Meal Service

FastAPI 기반의 Mymoo 식당/식단 서비스입니다. 식당 등록 요청, 식단 조회/관리, 가격 정책, 관리자 권한, 감사 로그를 제공합니다.

## 핵심 동작

- 식단 비즈니스 날짜는 `Asia/Seoul` 기준 `served_date`입니다.
- 식사 유형은 `breakfast`, `lunch`, `dinner`만 지원합니다.
- 인증은 `Authorization: Bearer` JWT + OIDC discovery/JWKS 검증이 기본입니다.
- Keycloak Admin API는 사용하지 않습니다. 유효한 JWT의 `sub`를 로컬 `users.user_id`로 자동 등록하고, owner/manager 관계는 meal-service DB 기준으로 검증합니다.
- `X-User-ID` fallback은 `AUTH_DEV_HEADER_FALLBACK_ENABLED=true`인 local/test/development 용도로만 허용됩니다. 운영에서는 활성화하면 안 됩니다.
- 가격 우선순위는 `date_specific` > `meal_type_fixed` > `restaurant_fixed`입니다.
- 쓰기 작업은 `X-Request-ID`를 전파하거나 자동 생성된 request_id와 함께 감사 로그에 기록됩니다.
- 기존 크롤러, 스케줄러, 수동 동기화 스타트업 동작은 제거되었습니다.

## 빠른 시작

1. `.env.example`를 복사해 `.env`를 생성합니다.
2. PostgreSQL 및 인증 관련 환경 변수를 채웁니다.
3. 아래 검증/실행 명령을 사용합니다.

## 검증 명령

```bash
uv run pytest
uv run ruff check .
uv run mypy app main.py
uv run alembic upgrade head
```

## Docker 실행

```bash
docker compose up -d --build
docker compose down
```

앱은 기본적으로 `5600` 포트를 사용하며 FastAPI root path는 `/meal`입니다.

## 임시 웹 포털

개발/검증용 웹 포털은 `test-meal-web` 독립 폴더에 분리되어 있습니다.

- 루트 compose 실행: `docker compose up -d --build test-meal-web`
- 웹 URL: `http://localhost:5601` 또는 프록시 배포 시 `https://mymoo.quanect.kr/test-web/`
- 기본 설정에서는 웹이 외부 API `https://mymoo.quanect.kr/meal`로 직접 요청합니다.
- 로컬 백엔드를 검증하려면 `TEST_MEAL_WEB_API_BASE_URL=/meal-api` 및 `TEST_MEAL_WEB_API_UPSTREAM=mymoo-meal-service:80`을 지정해 `/meal-api/*` 프록시를 사용할 수 있습니다.
- Keycloak 로그인은 Authorization Code + PKCE로 동작합니다.
- 웹 로그인 client 기본값은 `mymoo-test-web`이며, 루트 compose에서는 `TEST_MEAL_WEB_KEYCLOAK_CLIENT_ID`로 변경할 수 있습니다.
- 로컬 개발에서 `X-User-ID` 모드를 쓰려면 Meal Service `.env`에 `ENV=local` 및 `AUTH_DEV_HEADER_FALLBACK_ENABLED=true`를 설정합니다.

포털은 사용자 사이트, 식당주인 사이트, 관리자 시나리오로 나뉘며 식단 조회/관리, 식당 등록 요청/승인/거절, 식당 관리자, 가격 정책 API를 직접 호출합니다. 웹 프로젝트만 따로 실행하려면 `test-meal-web/docker-compose.yml`을 사용할 수 있습니다.

## 주요 환경 변수

- `ENV`: `local`, `test`, `development`, `production`
- `DEBUG`: 디버그 로깅 활성화 여부
- `AUTH_DEV_HEADER_FALLBACK_ENABLED`: 기본값 `false`, 운영에서 활성화 금지
- `CORS_ALLOW_ORIGINS`: 브라우저 직접 호출을 허용할 origin CSV, 예: `http://localhost:5601,https://mymoo.quanect.kr`
- `KEYCLOAK_DISCOVERY_URL`: OIDC discovery URL
- `JWT_ISSUER`: 기대 issuer
- `JWT_AUDIENCE`: 기대 audience
- `JWT_ALLOWED_ALGORITHMS`: 허용 알고리즘 목록
- `JWT_CLIENT_ID`: role claim을 확인할 backend client ID, 기본 `mymoo-meal-service`
- `TIMEZONE`: 기본 `Asia/Seoul`
- `DATABASE_URL`: 명시 시 `POSTGRES_*`보다 우선

## 운영 메모

- Docker Compose는 앱과 PostgreSQL을 함께 실행합니다.
- 제거된 student cafeteria seed/crawler 파일은 더 이상 마운트하지 않습니다.
- 현재 읽기 전용 config mount는 `app/config/meal_types.json`만 사용합니다.
