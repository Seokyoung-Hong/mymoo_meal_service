const storageKey = "mymooMealPortal";
const authStorageKey = `${storageKey}:auth`;
const persistedSettingKeys = [
  "apiBaseUrl",
  "keycloakIssuer",
  "keycloakClientId",
  "redirectUri",
  "authMode",
  "devUserId",
];

const defaultApiBaseUrl = "https://mymoo.quanect.kr/meal";
const defaultKeycloakIssuer = "https://auth.quanect.kr/realms/Mymoo";
const defaultKeycloakClientId = "mymoo-test-web";

function defaultRedirectUri() {
  const path = window.location.pathname.endsWith("/")
    ? window.location.pathname
    : `${window.location.pathname}/`;
  return `${window.location.origin}${path}`;
}

const state = {
  apiBaseUrl: defaultApiBaseUrl,
  keycloakIssuer: defaultKeycloakIssuer,
  keycloakClientId: defaultKeycloakClientId,
  redirectUri: defaultRedirectUri(),
  authMode: "bearer",
  devUserId: "",
  accessToken: "",
  idToken: "",
  refreshToken: "",
  expiresAt: 0,
};

const $ = (id) => document.getElementById(id);

function setOutput(value, requestLine = "완료") {
  $("requestLine").textContent = requestLine;
  $("output").textContent =
    typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

function loadStoredState() {
  const raw = localStorage.getItem(storageKey);
  if (raw) {
    try {
      const stored = JSON.parse(raw);
      persistedSettingKeys.forEach((key) => {
        if (stored[key] !== undefined) {
          state[key] = stored[key];
        }
      });
      persistSettings();
    } catch {
      localStorage.removeItem(storageKey);
    }
  }

  const authRaw = sessionStorage.getItem(authStorageKey);
  if (authRaw) {
    try {
      const authState = JSON.parse(authRaw);
      ["accessToken", "idToken", "refreshToken", "expiresAt"].forEach((key) => {
        if (authState[key] !== undefined) {
          state[key] = authState[key];
        }
      });
    } catch {
      sessionStorage.removeItem(authStorageKey);
    }
  }
}

function persistSettings() {
  const settings = {};
  persistedSettingKeys.forEach((key) => {
    settings[key] = state[key];
  });
  localStorage.setItem(storageKey, JSON.stringify(settings));
}

function normalizeApiBaseUrl(value) {
  return (value || defaultApiBaseUrl)
    .trim()
    .replace(/\/$/, "")
    .replace("https://nymoo.quanect.kr/meal", defaultApiBaseUrl);
}

function normalizeRedirectUri(value) {
  const uri = (value || defaultRedirectUri()).trim();
  return uri.endsWith("/test-web") ? `${uri}/` : uri;
}

function normalizeKeycloakIssuer(value) {
  return (value || defaultKeycloakIssuer)
    .trim()
    .replace(/\/$/, "")
    .replace("https://auth.quanect.kr/realms/Sandori", defaultKeycloakIssuer);
}

function normalizeKeycloakClientId(value) {
  return (value || defaultKeycloakClientId).trim();
}

function persistSessionAuth() {
  if (!state.accessToken && !state.idToken && !state.refreshToken) {
    sessionStorage.removeItem(authStorageKey);
    return;
  }
  sessionStorage.setItem(
    authStorageKey,
    JSON.stringify({
      accessToken: state.accessToken,
      idToken: state.idToken,
      refreshToken: state.refreshToken,
      expiresAt: state.expiresAt,
    }),
  );
}

function saveState() {
  state.apiBaseUrl = normalizeApiBaseUrl($("apiBaseUrl").value);
  state.keycloakIssuer = normalizeKeycloakIssuer($("keycloakIssuer").value);
  state.keycloakClientId = normalizeKeycloakClientId($("keycloakClientId").value);
  state.redirectUri = normalizeRedirectUri($("redirectUri").value);
  state.devUserId = $("devUserId").value.trim();
  state.accessToken = $("accessToken").value.trim();
  persistSettings();
  persistSessionAuth();
  updateAuthStatus();
}

function applyStateToInputs() {
  $("apiBaseUrl").value = state.apiBaseUrl;
  $("keycloakIssuer").value = state.keycloakIssuer;
  $("keycloakClientId").value = state.keycloakClientId;
  $("redirectUri").value = state.redirectUri;
  $("devUserId").value = state.devUserId;
  $("accessToken").value = state.accessToken;
  document.querySelectorAll("[data-auth-mode]").forEach((button) => {
    button.classList.toggle("active", button.dataset.authMode === state.authMode);
  });
}

function decodeJwt(token) {
  if (!token || !token.includes(".")) {
    return {};
  }
  try {
    const payload = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    const json = decodeURIComponent(
      atob(payload)
        .split("")
        .map((char) => `%${`00${char.charCodeAt(0).toString(16)}`.slice(-2)}`)
        .join(""),
    );
    return JSON.parse(json);
  } catch {
    return {};
  }
}

function currentUserId() {
  if (state.authMode === "dev") {
    return $("devUserId").value.trim();
  }
  return decodeJwt($("accessToken").value.trim()).sub || "";
}

function updateAuthStatus() {
  const token = $("accessToken").value.trim();
  const claims = decodeJwt(token);
  const expiresIn = state.expiresAt
    ? Math.max(0, Math.floor((state.expiresAt - Date.now()) / 1000))
    : 0;
  const mode = state.authMode === "dev" ? "X-User-ID" : "Bearer";
  const subject =
    state.authMode === "dev" ? $("devUserId").value.trim() : claims.sub || "토큰 없음";
  $("authStatus").textContent = `${mode} | user_id: ${subject || "미설정"}${
    expiresIn ? ` | 만료까지 ${expiresIn}s` : ""
  }`;
}

async function loadServerConfig() {
  try {
    const response = await fetch(new URL("./config.json", window.location.href));
    if (!response.ok) {
      return;
    }
    const config = await response.json();
    state.apiBaseUrl = normalizeApiBaseUrl(config.apiBaseUrl || state.apiBaseUrl);
    state.keycloakIssuer = normalizeKeycloakIssuer(
      config.keycloakIssuer || state.keycloakIssuer,
    );
    state.keycloakClientId = normalizeKeycloakClientId(
      config.keycloakClientId || state.keycloakClientId,
    );
  } catch {
    // The portal can still run from a file or another static server.
  }
}

function base64Url(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  bytes.forEach((byte) => {
    binary += String.fromCharCode(byte);
  });
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function randomString() {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return base64Url(bytes);
}

async function sha256(value) {
  return crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
}

async function discovery() {
  const issuer = $("keycloakIssuer").value.trim().replace(/\/$/, "");
  if (!issuer) {
    throw new Error("Keycloak Issuer를 입력하세요.");
  }
  try {
    const response = await fetch(`${issuer}/.well-known/openid-configuration`);
    if (response.ok) {
      return response.json();
    }
  } catch {
    // Keycloak의 표준 경로로 fallback합니다.
  }
  return {
    authorization_endpoint: `${issuer}/protocol/openid-connect/auth`,
    token_endpoint: `${issuer}/protocol/openid-connect/token`,
    end_session_endpoint: `${issuer}/protocol/openid-connect/logout`,
  };
}

async function loginWithKeycloak() {
  saveState();
  const verifier = randomString();
  const challenge = base64Url(await sha256(verifier));
  const authState = randomString();
  const issuer = state.keycloakIssuer.replace(/\/$/, "");
  const clientId = state.keycloakClientId;
  const redirectUri = state.redirectUri;

  sessionStorage.setItem("mymooPkceVerifier", verifier);
  sessionStorage.setItem("mymooAuthState", authState);
  sessionStorage.setItem("mymooAuthIssuer", issuer);
  sessionStorage.setItem("mymooAuthClientId", clientId);
  sessionStorage.setItem("mymooRedirectUri", redirectUri);

  const oidc = await discovery();
  const params = new URLSearchParams({
    client_id: clientId,
    redirect_uri: redirectUri,
    response_type: "code",
    scope: "openid profile email",
    state: authState,
    code_challenge: challenge,
    code_challenge_method: "S256",
    prompt: "login",
    max_age: "0",
  });
  window.location.href = `${oidc.authorization_endpoint}?${params.toString()}`;
}

async function completeLoginIfNeeded() {
  const url = new URL(window.location.href);
  const code = url.searchParams.get("code");
  const returnedState = url.searchParams.get("state");
  const returnedIssuer = url.searchParams.get("iss");
  if (!code) {
    return;
  }
  const expectedState = sessionStorage.getItem("mymooAuthState");
  const verifier = sessionStorage.getItem("mymooPkceVerifier");
  if (!expectedState || returnedState !== expectedState || !verifier) {
    throw new Error("Keycloak state 또는 PKCE verifier가 일치하지 않습니다.");
  }

  state.keycloakIssuer = (
    returnedIssuer ||
    sessionStorage.getItem("mymooAuthIssuer") ||
    state.keycloakIssuer
  ).replace(/\/$/, "");
  state.keycloakClientId =
    sessionStorage.getItem("mymooAuthClientId") || state.keycloakClientId;
  state.redirectUri = sessionStorage.getItem("mymooRedirectUri") || state.redirectUri;
  applyStateToInputs();

  const oidc = await discovery();
  const body = new URLSearchParams({
    grant_type: "authorization_code",
    client_id: state.keycloakClientId,
    redirect_uri: state.redirectUri,
    code,
    code_verifier: verifier,
  });
  const response = await fetch(oidc.token_endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(
      JSON.stringify(
        {
          ...payload,
          token_endpoint: oidc.token_endpoint,
          client_id: state.keycloakClientId,
          redirect_uri: state.redirectUri,
          issuer: state.keycloakIssuer,
        },
        null,
        2,
      ),
    );
  }

  state.authMode = "bearer";
  state.accessToken = payload.access_token || "";
  state.idToken = payload.id_token || "";
  state.refreshToken = payload.refresh_token || "";
  state.expiresAt = Date.now() + Number(payload.expires_in || 0) * 1000;
  sessionStorage.removeItem("mymooPkceVerifier");
  sessionStorage.removeItem("mymooAuthState");
  sessionStorage.removeItem("mymooAuthIssuer");
  sessionStorage.removeItem("mymooAuthClientId");
  sessionStorage.removeItem("mymooRedirectUri");
  persistSettings();
  persistSessionAuth();
  window.history.replaceState({}, document.title, state.redirectUri);
  applyStateToInputs();
  setOutput(decodeJwt(state.accessToken), "Keycloak 로그인 완료");
}

async function logout() {
  const idToken = state.idToken;
  const issuer = $("keycloakIssuer").value.trim();
  const clientId = $("keycloakClientId").value.trim();
  state.accessToken = "";
  state.idToken = "";
  state.refreshToken = "";
  state.expiresAt = 0;
  $("accessToken").value = "";
  saveState();

  if (issuer) {
    const oidc = await discovery();
    const params = new URLSearchParams({
      post_logout_redirect_uri: state.redirectUri,
    });
    if (idToken) {
      params.set("id_token_hint", idToken);
    }
    if (clientId) {
      params.set("client_id", clientId);
    }
    window.location.href = `${oidc.end_session_endpoint}?${params.toString()}`;
  } else {
    setOutput("로컬 토큰을 제거했습니다.", "로그아웃");
  }
}

function queryString(params) {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && String(value).trim() !== "") {
      query.set(key, value);
    }
  });
  const serialized = query.toString();
  return serialized ? `?${serialized}` : "";
}

function apiUrl(path, params = {}) {
  const base = normalizeApiBaseUrl($("apiBaseUrl").value);
  return `${base}${path}${queryString(params)}`;
}

function authHeaders(hasBody = false) {
  const headers = {
    Accept: "application/json",
    "X-Request-ID": `portal-${Date.now()}`,
  };
  if (hasBody) {
    headers["Content-Type"] = "application/json";
  }
  if (state.authMode === "dev") {
    const userId = $("devUserId").value.trim();
    if (userId) {
      headers["X-User-ID"] = userId;
    }
  } else {
    const token = $("accessToken").value.trim();
    if (token) {
      headers.Authorization = `Bearer ${token}`;
    }
  }
  return headers;
}

async function callApi(method, path, { params = {}, body = undefined, auth = true } = {}) {
  saveState();
  const url = apiUrl(path, params);
  const options = {
    method,
    headers: auth ? authHeaders(body !== undefined) : { Accept: "application/json" },
  };
  if (body !== undefined) {
    options.body = JSON.stringify(body);
  }
  $("requestLine").textContent = `${method} ${url}`;
  const response = await fetch(url, options);
  const text = await response.text();
  let payload = text;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = text;
  }
  if (!response.ok) {
    setOutput({ status: response.status, body: payload }, `${method} ${url}`);
    throw new Error(`HTTP ${response.status}`);
  }
  setOutput(payload ?? { status: response.status }, `${method} ${url}`);
  return payload;
}

function requireNumber(id, label) {
  const value = $(id).value;
  if (!value) {
    throw new Error(`${label}을 입력하세요.`);
  }
  return Number(value);
}

function splitList(value) {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function today() {
  return new Date().toISOString().slice(0, 10);
}

function restaurantPayload(prefix) {
  const price = $(`${prefix}Price`).value;
  const ownerInput = $(`${prefix}OwnerUserId`);
  const payload = {
    name: $(`${prefix}Name`).value.trim(),
    establishment_type: $(`${prefix}Type`).value,
    price: price ? Number(price) : null,
    location: {
      is_campus: true,
      building: $(`${prefix}Building`).value.trim() || null,
      map_links: null,
      latitude: null,
      longitude: null,
    },
    opening_time: { start: "09:00", end: "20:00" },
    break_time: null,
    breakfast_time: null,
    lunch_time: null,
    dinner_time: null,
  };
  if (ownerInput && ownerInput.value.trim()) {
    payload.owner_user_id = ownerInput.value.trim();
  }
  return payload;
}

function requestPayload(prefix) {
  const price = $(`${prefix}Price`).value;
  return {
    name: $(`${prefix}Name`).value.trim(),
    establishment_type: $(`${prefix}Type`).value,
    price: price ? Number(price) : null,
    location: {
      is_campus: true,
      building: $(`${prefix}Building`).value.trim() || null,
      map_links: null,
      latitude: null,
      longitude: null,
    },
    opening_time: {
      start: $(`${prefix}OpenStart`).value || "09:00",
      end: $(`${prefix}OpenEnd`).value || "20:00",
    },
    break_time: null,
    breakfast_time: null,
    lunch_time: null,
    dinner_time: null,
  };
}

function mealPayload({ partial = false } = {}) {
  const payload = {
    served_date: $("ownerMealDate").value || today(),
    main_menu: $("mainMenu").value.trim(),
    side_menus: splitList($("sideMenus").value),
    image_url: $("imageUrl").value.trim() || null,
    meal_type: $("ownerMealType").value,
  };
  if (partial) {
    payload.restaurant_id = Number($("ownerMealRestaurantId").value) || undefined;
    Object.keys(payload).forEach((key) => {
      if (payload[key] === "" || payload[key] === undefined) {
        delete payload[key];
      }
    });
  }
  return payload;
}

function pricingPayload() {
  const policyType = $("policyType").value;
  const payload = {
    policy_type: policyType,
    price: Number($("policyPrice").value),
    is_active: $("policyActive").value === "true",
  };
  if (policyType === "meal_type_fixed") {
    payload.meal_type = $("policyMealType").value || "lunch";
  }
  if (policyType === "date_specific") {
    payload.served_date = $("policyDate").value || today();
    if ($("policyMealType").value) {
      payload.meal_type = $("policyMealType").value;
    }
  }
  return payload;
}

function workerTicketPayload() {
  return {
    code: $("workerTicketCode").value.trim(),
    amount: Number($("workerTicketAmount").value),
    expires_on: $("workerTicketExpiresOn").value || today(),
  };
}

function workerCardChargePayload() {
  const payload = {
    amount: Number($("workerCashChargeAmount").value),
  };
  const cardLast4 = $("workerCardLast4").value.trim();
  if (cardLast4) {
    payload.card_last4 = cardLast4;
  }
  return payload;
}

function workerTicketUsagePayload() {
  const payload = {
    ticket_code: $("workerUseTicketCode").value.trim(),
    restaurant_id: requireNumber("workerUseRestaurantId", "restaurant ID"),
    served_date: $("workerUseDate").value || today(),
  };
  const mealType = $("workerUseMealType").value;
  const mealPrice = $("workerUseMealPrice").value;
  if (mealType) {
    payload.meal_type = mealType;
  }
  if (mealPrice) {
    payload.meal_price = Number(mealPrice);
  }
  return payload;
}

function mealAllowancePayload() {
  return {
    worker_user_ids: $("allowanceWorkerUserIds").value
      .split(/[\n,]/)
      .map((item) => item.trim())
      .filter(Boolean),
    amount: Number($("allowanceAmount").value),
    expires_on: $("allowanceExpiresOn").value || today(),
  };
}

function ticketScanPayload() {
  const payload = {
    ticket_code: $("scanTicketCode").value.trim(),
  };
  const mealType = $("scanMealType").value;
  const mealPrice = $("scanMealPrice").value;
  if (mealType) {
    payload.meal_type = mealType;
  }
  if (mealPrice) {
    payload.meal_price = Number(mealPrice);
  }
  return payload;
}

const actions = {
  health: () => callApi("GET", "/health", { auth: false }),
  listUsers: () => callApi("GET", "/users/"),
  registerCurrentUser: () => {
    const userId = currentUserId();
    if (!userId) {
      throw new Error("현재 user_id를 확인할 수 없습니다.");
    }
    return callApi("POST", "/users/", { body: { user_id: userId } });
  },
  todayMeals: () =>
    callApi("GET", "/meals/today", {
      params: {
        restaurant_name: $("mealRestaurantName").value,
        meal_type: $("mealTypeFilter").value,
        page: 1,
        size: 50,
      },
      auth: false,
    }),
  listMeals: () =>
    callApi("GET", "/meals", {
      params: {
        restaurant_name: $("mealRestaurantName").value,
        meal_type: $("mealTypeFilter").value,
        date: $("mealDate").value,
        start_date: $("mealStartDate").value,
        end_date: $("mealEndDate").value,
        page: 1,
        size: 50,
      },
      auth: false,
    }),
  latestMeals: () =>
    callApi("GET", "/meals/latest", {
      params: {
        restaurant_name: $("mealRestaurantName").value,
        meal_type: $("mealTypeFilter").value,
        start_date: $("mealStartDate").value,
        end_date: $("mealEndDate").value,
        page: 1,
        size: 50,
      },
      auth: false,
    }),
  getMeal: () =>
    callApi("GET", `/meals/${requireNumber("mealLookupId", "식단 ID")}`, {
      auth: false,
    }),
  listRestaurants: () =>
    callApi("GET", "/restaurants/", {
      params: {
        name: $("restaurantName").value,
        establishment_type: $("establishmentType").value,
        is_campus: $("isCampus").value,
        page: 1,
        size: 50,
      },
      auth: false,
    }),
  getRestaurant: () =>
    callApi("GET", `/restaurants/${requireNumber("restaurantLookupId", "식당 ID")}`, {
      auth: false,
    }),
  restaurantMeals: () =>
    callApi("GET", `/restaurants/${requireNumber("restaurantLookupId", "식당 ID")}/meals`, {
      params: {
        date: $("mealDate").value,
        start_date: $("mealStartDate").value,
        end_date: $("mealEndDate").value,
        meal_type: $("mealTypeFilter").value,
        page: 1,
        size: 50,
      },
      auth: false,
    }),
  resolvePrice: () =>
    callApi("GET", `/restaurants/${requireNumber("restaurantLookupId", "식당 ID")}/price`, {
      params: {
        meal_type: $("priceMealType").value,
        served_date: $("priceServedDate").value,
      },
      auth: false,
    }),
  registerWorkerTicket: () =>
    callApi("POST", "/worker/tickets", { body: workerTicketPayload() }),
  listWorkerTickets: () => callApi("GET", "/worker/tickets"),
  getWorkerCashBalance: () => callApi("GET", "/worker/cash/balance"),
  chargeWorkerCash: () =>
    callApi("POST", "/worker/cash/card-charges", {
      body: workerCardChargePayload(),
    }),
  listWorkerCashTransactions: () => callApi("GET", "/worker/cash/transactions"),
  createWorkerTicketUsageRequest: () =>
    callApi("POST", "/worker/ticket-usage-requests", {
      body: workerTicketUsagePayload(),
    }),
  listWorkerTicketUsageRequests: () =>
    callApi("GET", "/worker/ticket-usage-requests"),
  scanRestaurantTicket: () =>
    callApi(
      "POST",
      `/restaurants/${requireNumber("scanRestaurantId", "식당 ID")}/ticket-scans`,
      { body: ticketScanPayload() },
    ),
  issueMealAllowances: () =>
    callApi("POST", "/admin/meal-allowances", { body: mealAllowancePayload() }),
  listMealAllowances: () =>
    callApi("GET", "/admin/meal-allowances", {
      params: { worker_user_id: $("allowanceFilterWorkerUserId").value },
    }),
  ownerSubmitRestaurantRequest: () =>
    callApi("POST", "/restaurants/requests", { body: requestPayload("ownerRequest") }),
  listRequests: () =>
    callApi("GET", "/restaurants/requests", { params: { page: 1, size: 50 } }),
  ownerListRequests: () =>
    callApi("GET", "/restaurants/requests", { params: { page: 1, size: 50 } }),
  getRequest: () => {
    const id = $("adminRequestId").value;
    if (!id) {
      throw new Error("요청 ID를 입력하세요.");
    }
    return callApi("GET", `/restaurants/requests/${Number(id)}`);
  },
  ownerGetRequest: () =>
    callApi(
      "GET",
      `/restaurants/requests/${requireNumber("ownerRequestLookupId", "신청 ID")}`,
    ),
  ownerDeleteRequest: () =>
    callApi(
      "DELETE",
      `/restaurants/requests/${requireNumber("ownerRequestLookupId", "신청 ID")}`,
    ),
  ownerRestaurants: () =>
    callApi("GET", "/restaurants/mine", {
      params: {
        page: 1,
        size: 50,
      },
    }),
  ownerRestaurantDetail: () =>
    callApi(
      "GET",
      `/restaurants/mine/${requireNumber("ownerRestaurantId", "식당 ID")}`,
    ),
  deleteRestaurant: () =>
    callApi("DELETE", `/restaurants/${requireNumber("ownerRestaurantId", "식당 ID")}`),
  createMeal: () =>
    callApi("POST", `/meals/${requireNumber("ownerMealRestaurantId", "식당 ID")}`, {
      body: mealPayload(),
    }),
  updateMeal: () =>
    callApi("PATCH", `/meals/${requireNumber("ownerMealId", "식단 ID")}`, {
      body: mealPayload({ partial: true }),
    }),
  patchMealMenu: () =>
    callApi("PATCH", `/meals/${requireNumber("ownerMealId", "식단 ID")}/menus`, {
      body: { menu: splitList($("sideMenus").value) },
    }),
  deleteMealMenu: () =>
    callApi("DELETE", `/meals/${requireNumber("ownerMealId", "식단 ID")}/menus`, {
      body: { menu: splitList($("sideMenus").value) },
    }),
  deleteMeal: () => callApi("DELETE", `/meals/${requireNumber("ownerMealId", "식단 ID")}`),
  listManagers: () =>
    callApi(
      "GET",
      `/restaurants/${requireNumber("ownerRestaurantId", "식당 ID")}/managers`,
    ),
  addManager: () =>
    callApi(
      "POST",
      `/restaurants/${requireNumber("ownerRestaurantId", "식당 ID")}/managers`,
      { body: { user_id: $("restaurantManagerUserId").value.trim() } },
    ),
  removeManager: () =>
    callApi(
      "DELETE",
      `/restaurants/${requireNumber("ownerRestaurantId", "식당 ID")}/managers/${encodeURIComponent(
        $("restaurantManagerUserId").value.trim(),
      )}`,
    ),
  listPricing: () =>
    callApi("GET", `/restaurants/${requireNumber("ownerRestaurantId", "식당 ID")}/pricing`),
  createPricing: () =>
    callApi("POST", `/restaurants/${requireNumber("ownerRestaurantId", "식당 ID")}/pricing`, {
      body: pricingPayload(),
    }),
  updatePricing: () =>
    callApi(
      "PATCH",
      `/restaurants/${requireNumber("ownerRestaurantId", "식당 ID")}/pricing/${requireNumber(
        "pricingPolicyId",
        "정책 ID",
      )}`,
      { body: pricingPayload() },
    ),
  updatePricingStatus: () =>
    callApi(
      "PATCH",
      `/restaurants/${requireNumber("ownerRestaurantId", "식당 ID")}/pricing/${requireNumber(
        "pricingPolicyId",
        "정책 ID",
      )}/status`,
      { body: { is_active: $("policyActive").value === "true" } },
    ),
  deletePricing: () =>
    callApi(
      "DELETE",
      `/restaurants/${requireNumber("ownerRestaurantId", "식당 ID")}/pricing/${requireNumber(
        "pricingPolicyId",
        "정책 ID",
      )}`,
    ),
  listRestaurantTicketUsageRequests: () =>
    callApi(
      "GET",
      `/restaurants/${requireNumber("ownerRestaurantId", "restaurant ID")}/ticket-usage-requests`,
      { params: { status: $("ownerTicketUsageStatus").value } },
    ),
  approveRestaurantTicketUsageRequest: () =>
    callApi(
      "POST",
      `/restaurants/${requireNumber("ownerRestaurantId", "restaurant ID")}/ticket-usage-requests/${requireNumber(
        "ownerTicketUsageRequestId",
        "ticket usage request ID",
      )}/approval`,
    ),
  approveRequest: () =>
    callApi(
      "POST",
      `/restaurants/requests/${requireNumber("adminRequestId", "요청 ID")}/approval`,
    ),
  rejectRequest: () =>
    callApi(
      "POST",
      `/restaurants/requests/${requireNumber("adminRequestId", "요청 ID")}/rejection`,
      { body: { message: $("rejectionMessage").value.trim() } },
    ),
  createRestaurant: () =>
    callApi("POST", "/restaurants/", { body: restaurantPayload("adminRestaurant") }),
  adminListRestaurants: () =>
    callApi("GET", "/admin/restaurants/", {
      params: {
        owner_user_id: $("adminRestaurantOwnerUserId").value,
        include_inactive: $("adminIncludeInactive").value,
        page: 1,
        size: 50,
      },
    }),
  adminRestaurantDetail: () =>
    callApi(
      "GET",
      `/admin/restaurants/${requireNumber("adminRestaurantId", "식당 ID")}`,
    ),
  updateRestaurant: () =>
    callApi("PATCH", `/restaurants/${requireNumber("adminRestaurantId", "식당 ID")}`, {
      body: restaurantPayload("adminRestaurant"),
    }),
  updateRestaurantStatus: () =>
    callApi(
      "PATCH",
      `/restaurants/${requireNumber("adminRestaurantId", "식당 ID")}/status`,
      { body: { is_active: $("restaurantActive").value === "true" } },
    ),
};

function bindEvents() {
  $("saveSettings").addEventListener("click", () => {
    saveState();
    setOutput("설정을 저장했습니다.", "설정 저장");
  });
  $("loginButton").addEventListener("click", () => {
    loginWithKeycloak().catch((error) => setOutput(error.message, "로그인 실패"));
  });
  $("logoutButton").addEventListener("click", () => {
    logout().catch((error) => setOutput(error.message, "로그아웃 실패"));
  });
  $("clearOutput").addEventListener("click", () => setOutput({}));

  document.querySelectorAll("[data-mode]").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll("[data-mode]").forEach((item) => {
        item.classList.toggle("active", item === button);
      });
      document.querySelectorAll(".mode-view").forEach((view) => {
        view.classList.toggle("active", view.id === `${button.dataset.mode}View`);
      });
    });
  });

  document.querySelectorAll("[data-auth-mode]").forEach((button) => {
    button.addEventListener("click", () => {
      state.authMode = button.dataset.authMode;
      saveState();
      applyStateToInputs();
    });
  });

  document.querySelectorAll("[data-action]").forEach((button) => {
    button.addEventListener("click", async () => {
      const action = actions[button.dataset.action];
      if (!action) {
        return;
      }
      try {
        await action();
      } catch (error) {
        setOutput(error.message || String(error), "호출 실패");
      }
    });
  });

  ["apiBaseUrl", "keycloakIssuer", "keycloakClientId", "redirectUri", "devUserId", "accessToken"].forEach(
    (id) => {
      $(id).addEventListener("change", saveState);
      $(id).addEventListener("input", updateAuthStatus);
    },
  );
}

async function init() {
  loadStoredState();
  await loadServerConfig();
  applyStateToInputs();
  bindEvents();
  $("ownerMealDate").value ||= today();
  $("priceServedDate").value ||= today();
  $("policyDate").value ||= today();
  const ticketExpiry = new Date();
  ticketExpiry.setFullYear(ticketExpiry.getFullYear() + 1);
  $("workerTicketExpiresOn").value ||= ticketExpiry.toISOString().slice(0, 10);
  $("allowanceExpiresOn").value ||= ticketExpiry.toISOString().slice(0, 10);
  $("workerUseDate").value ||= today();
  await completeLoginIfNeeded();
  updateAuthStatus();
}

init().catch((error) => setOutput(error.message, "초기화 실패"));
