"""Regression tests for removed external meal sync residue."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI

from scripts import check_residue


@pytest.mark.asyncio
async def test_lifespan_does_not_import_or_start_scheduler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production startup keeps DB bootstrap but has no scheduler/crawler side effects."""
    main = importlib.import_module("main")
    calls: list[str] = []

    async def fake_init_db() -> None:
        calls.append("init_db")

    async def fake_sync_meal_types() -> None:
        calls.append("sync_meal_types")

    async def fake_ensure_service_account_in_db() -> None:
        calls.append("ensure_service_account_in_db")

    monkeypatch.setattr(main, "init_db", fake_init_db)
    monkeypatch.setattr(main, "sync_meal_types", fake_sync_meal_types)
    monkeypatch.setattr(
        main,
        "ensure_service_account_in_db",
        fake_ensure_service_account_in_db,
    )

    async with main.lifespan(FastAPI()):
        assert calls == ["init_db", "sync_meal_types", "ensure_service_account_in_db"]

    assert "app.jobs.scheduler" not in sys.modules
    assert "app.services.crawler_service" not in sys.modules
    assert "app.services.ibook_downloader" not in sys.modules
    assert "app.services.excel_importer" not in sys.modules


@pytest.mark.asyncio
async def test_meal_sync_route_returns_404(async_client) -> None:
    """The old manual meal sync endpoint is no longer mounted."""
    response = await async_client.post("/meals/meal_sync")

    assert response.status_code == 404


def test_required_residue_terms_have_no_production_matches() -> None:
    """Use the production residue checker for the Task 13 scan terms."""
    project_root = Path(__file__).resolve().parents[1]
    terms = [
        "ibook",
        "TUKorea",
        "한국공학",
        "TIP_RESTAURANT_ID",
        "E_RESTAURANT_ID",
        "meal_sync",
    ]
    paths = ["app", "main.py", "README.md", "docker-compose.yml", ".env.example"]

    matches: list[str] = []
    for path in check_residue.iter_files(paths, project_root):
        matches.extend(check_residue.scan_file(path, terms, project_root))

    assert matches == []
