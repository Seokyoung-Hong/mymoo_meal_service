"""OpenAPI authentication and public metrics metadata regressions."""

from __future__ import annotations

from typing import Any

from main import app


PUBLIC_GET_OPERATIONS = {
    ("/", "get"),
    ("/health", "get"),
    ("/meals", "get"),
    ("/meals/today", "get"),
    ("/meals/latest", "get"),
    ("/meals/{meal_id}", "get"),
    ("/meals/restaurant/{restaurant_id}/latest", "get"),
    ("/meals/restaurant/{restaurant_id}", "get"),
    ("/restaurants/{restaurant_id}/price", "get"),
    ("/restaurants/{restaurant_id}/meals", "get"),
    ("/restaurants/", "get"),
    ("/restaurants/{restaurant_id}", "get"),
    ("/images/{file_name}", "get"),
}


def _parameters(operation: dict[str, Any]) -> list[dict[str, Any]]:
    return list(operation.get("parameters", []))


def _header_names(operation: dict[str, Any]) -> set[str]:
    return {
        str(parameter["name"])
        for parameter in _parameters(operation)
        if parameter.get("in") == "header"
    }


def test_openapi_bearer_security_and_public_metrics_headers() -> None:
    app.openapi_schema = None
    schema = app.openapi()

    assert schema["components"]["securitySchemes"]["BearerAuth"] == {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
    }

    seen_operations: set[tuple[str, str]] = set()
    for path, path_item in schema["paths"].items():
        for method, operation in path_item.items():
            if method not in {"get", "post", "patch", "delete"}:
                continue

            operation_key = (path, method)
            seen_operations.add(operation_key)
            header_names = _header_names(operation)

            assert "Authorization" not in header_names
            assert "authorization" not in header_names

            if operation_key in PUBLIC_GET_OPERATIONS:
                assert "security" not in operation
                assert "X-User-ID" in header_names
                x_user_id = next(
                    parameter
                    for parameter in _parameters(operation)
                    if parameter.get("in") == "header"
                    and parameter.get("name") == "X-User-ID"
                )
                assert x_user_id["required"] is False
                assert "untrusted" in x_user_id["description"].lower()
                assert "not authentication" in x_user_id["description"].lower()
            else:
                assert operation.get("security") == [{"BearerAuth": []}]
                assert "X-User-ID" not in header_names
                assert "x-user-id" not in header_names

    assert PUBLIC_GET_OPERATIONS <= seen_operations
    assert ("/restaurants/mine", "get") in seen_operations
    assert ("/restaurants/mine/{restaurant_id}", "get") in seen_operations
    assert ("/admin/restaurants/", "get") in seen_operations
    assert ("/admin/restaurants/{restaurant_id}", "get") in seen_operations
