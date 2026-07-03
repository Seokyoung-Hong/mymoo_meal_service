# Test Meal Web

Meal Service API를 수동 검증하기 위한 독립 웹 포털입니다. 백엔드 앱에 정적 파일로 포함하지 않고 별도 Docker 이미지로 빌드합니다.

## 루트 compose에서 실행

```bash
docker compose up -d --build mymoo-meal-service test-meal-web
```

접속: `http://localhost:5601` 또는 프록시 배포 시 `https://mymoo.quanect.kr/test-web/`

기본 설정에서는 브라우저가 외부 API `https://mymoo.quanect.kr/meal`로 직접 요청합니다.
로컬 백엔드를 검증하려면 `MEAL_API_PUBLIC_BASE_URL=/meal-api`와 `MEAL_API_UPSTREAM`을 지정해 nginx 프록시를 사용할 수 있습니다.

## 웹 프로젝트만 따로 실행

Meal Service가 호스트에서 `http://localhost:5600`으로 실행 중이면:

```bash
docker compose up -d --build
```

## Runtime 환경 변수

- `MEAL_API_PUBLIC_BASE_URL`: 브라우저가 호출할 API base URL, 기본 `https://mymoo.quanect.kr/meal`
- `MEAL_API_UPSTREAM`: nginx가 `/meal-api/*` 프록시 모드에서 사용할 upstream, 루트 compose 기본 `mymoo-meal-service:80`
- `KEYCLOAK_ISSUER`: Keycloak realm issuer, 기본 `https://auth.quanect.kr/realms/Mymoo`
- `KEYCLOAK_CLIENT_ID`: 브라우저 로그인용 public client id, 기본 `mymoo-test-web`
- `KEYCLOAK_DISCOVERY_URL`: OIDC discovery URL
- `AUTH_DEV_HEADER_FALLBACK_ENABLED`: 화면 표시용 fallback 여부

Keycloak client에는 redirect URI로 `http://localhost:5601/`을 허용해야 합니다.

## Keycloak client 설정

`mymoo-test-web` client는 브라우저에서 Authorization Code + PKCE로 로그인하므로 public client여야 합니다.

- Client authentication: Off
- Standard flow: On
- Implicit flow: Off
- Direct access grants: Off
- PKCE: S256 required
- Valid redirect URIs: `http://localhost:5601/*`, `https://mymoo.quanect.kr/test-web/*`
- Valid post logout redirect URIs: `http://localhost:5601/*`, `https://mymoo.quanect.kr/test-web/*`
- Web origins: `http://localhost:5601`, `https://mymoo.quanect.kr`

`unauthorized_client` 및 `Invalid client credentials`가 발생하면 대부분 client authentication이 켜져 있거나, 웹에 내려가는 `KEYCLOAK_CLIENT_ID`가 실제 Keycloak client id와 다를 때입니다.
