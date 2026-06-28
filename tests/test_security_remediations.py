"""Regression tests for security scan remediations."""

from __future__ import annotations

from pathlib import Path

from app.models.restaurants import Restaurant, set_service_user_id


ROOT = Path(__file__).resolve().parents[1]


def test_restaurant_soft_delete_deactivates_restaurant() -> None:
    set_service_user_id(42)
    restaurant = Restaurant(
        name="Deleted Target",
        owner=1,
        is_active=True,
        is_campus=True,
        establishment_type="fixed_menu_restaurant",
    )

    restaurant.soft_delete()

    assert restaurant.is_active is False
    assert restaurant.owner == 42


def test_database_url_is_not_logged_directly() -> None:
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    alembic_source = (ROOT / "alembic" / "env.py").read_text(encoding="utf-8")

    assert '"database_url": Config.DATABASE_URL' not in main_source
    assert "DATABASE_URL: {DATABASE_URL}" not in alembic_source
    assert "render_as_string(hide_password=True)" in alembic_source


def test_test_portal_keeps_tokens_out_of_local_storage() -> None:
    source = (ROOT / "test-meal-web" / "src" / "app.js").read_text(encoding="utf-8")

    assert "localStorage.setItem(storageKey, JSON.stringify(state))" not in source
    assert "const authStorageKey" in source
    assert "sessionStorage.setItem(" in source
    assert '"accessToken",' not in source.split("const persistedSettingKeys = [", 1)[1].split(
        "];",
        1,
    )[0]
