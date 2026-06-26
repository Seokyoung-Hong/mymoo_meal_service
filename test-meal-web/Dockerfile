FROM nginx:1.27-alpine

ENV MEAL_API_PUBLIC_BASE_URL=/meal-api \
    MEAL_API_UPSTREAM=mymoo-meal-service:80 \
    KEYCLOAK_ISSUER=https://auth.quanect.kr/realms/Sandori \
    KEYCLOAK_CLIENT_ID=mymoo-test-web \
    KEYCLOAK_DISCOVERY_URL=https://auth.quanect.kr/realms/Sandori/.well-known/openid-configuration \
    AUTH_DEV_HEADER_FALLBACK_ENABLED=false

COPY src/ /usr/share/nginx/html/
COPY nginx.conf.template /etc/nginx/templates/default.conf.template

EXPOSE 80
