from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from apps.api.application import create_app
from packages.auth.security import PASSWORD_HASHER
from packages.mining import public_assets as assets
from packages.persistence.models import User
from packages.semantic import asset_catalog as catalog
from tests.auth_support import migrate, scratch_database
from tests.semantic_asset_support import source

PASSWORD = "semantic-api-fixture-password"
pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def database():
    with scratch_database() as (settings, engine):
        migrate(engine)
        with Session(engine) as db, db.begin():
            users = {}
            for role in ("Viewer", "Member", "Admin"):
                user = User(
                    id=str(uuid4()),
                    username="semantic-" + role.lower(),
                    role=role,
                    is_active=True,
                    password_hash=PASSWORD_HASHER.hash(PASSWORD),
                )
                db.add(user)
                users[role] = user.id
        yield settings, engine, users


@pytest.fixture
def environment(database, monkeypatch):
    settings, engine, users = database
    parent = assets.ROOT / "runtime/temp"
    parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=parent, prefix="semantic-api-") as temp:
        root = Path(temp)
        directory, m = source(root)
        store = root / "installed"
        installed = catalog.install(directory, m["content_hash"], store=store)
        monkeypatch.setattr(catalog, "STORE", store)
        with TestClient(create_app(settings), base_url="http://127.0.0.1") as api:
            yield api, installed, engine, users


def login(api, role):
    result = api.post(
        "/api/v1/auth/login", json={"username": "semantic-" + role.lower(), "password": PASSWORD}
    )
    assert result.status_code == 201
    return {"Authorization": "Bearer " + result.json()["access_token"]}


@pytest.mark.parametrize("role", ["Viewer", "Member", "Admin"])
def test_real_session_reads_paginated_asset_with_no_write_permission(environment, role):
    api, installed, *_ = environment
    headers = login(api, role)
    listing = api.get("/api/v1/semantic-assets?page_size=1", headers=headers)
    assert listing.status_code == 200 and listing.json()["total"] == 1
    assert listing.headers["Cache-Control"] == "no-store"
    assert listing.json()["items"][0]["payload_verified"] is False
    result = api.get(
        f"/api/v1/semantic-assets/{installed.name}?partition=validation&offset=170&limit=2",
        headers=headers,
    )
    assert result.status_code == 200
    value = result.json()
    assert value["payload_verified"] is True and value["total"] == 172
    assert [r["population_index"] for r in value["items"]] == [658, 659]
    assert value["capabilities"]["training_ready"] is False
    assert "runtime/" not in result.text and "input_directory" not in result.text
    assert "embeddings.npy" not in result.text and "dependencies" not in result.text
    assert (
        api.post("/api/v1/semantic-assets", headers=headers, json={"path": "arbitrary"}).status_code
        == 405
    )


def test_anonymous_and_disabled_session_are_rejected(environment):
    api, installed, engine, users = environment
    assert api.get("/api/v1/semantic-assets").status_code == 401
    headers = login(api, "Viewer")
    with Session(engine) as db, db.begin():
        db.get(User, users["Viewer"]).is_active = False
    try:
        assert (
            api.get(f"/api/v1/semantic-assets/{installed.name}", headers=headers).status_code == 401
        )
    finally:
        with Session(engine) as db, db.begin():
            db.get(User, users["Viewer"]).is_active = True


@pytest.mark.parametrize(
    "suffix",
    [
        "?page=0",
        "?page_size=101",
        "/not-an-id",
        "/" + "a" * 64 + "?limit=101",
        "/" + "a" * 64 + "?offset=661",
        "/" + "a" * 64 + "?partition=test",
    ],
)
def test_invalid_id_or_paging_is_safe_422(environment, suffix):
    api, *_ = environment
    result = api.get("/api/v1/semantic-assets" + suffix, headers=login(api, "Member"))
    assert result.status_code == 422 and result.json()["code"] == "SYSTEM_INVALID_INPUT"
    assert "semantic-api-" not in result.text and "password" not in result.text


def test_corrupted_payload_fails_detail_without_downgrading_listing_claim(environment):
    api, installed, *_ = environment
    headers = login(api, "Member")
    path = installed / "embeddings.npy"
    with path.open("r+b") as stream:
        stream.seek(-1, 2)
        stream.write(b"\xff")
    listing = api.get("/api/v1/semantic-assets", headers=headers)
    assert listing.status_code == 200 and listing.json()["items"][0]["payload_verified"] is False
    result = api.get(f"/api/v1/semantic-assets/{installed.name}", headers=headers)
    assert result.status_code == 409
    assert result.json()["code"].startswith("SEMANTIC_ASSET_")
    assert "semantic-api-" not in result.text and "Traceback" not in result.text


def test_empty_catalog_and_missing_asset(environment, monkeypatch):
    api, installed, *_ = environment
    monkeypatch.setattr(catalog, "STORE", installed.parent / "empty")
    headers = login(api, "Viewer")
    assert api.get("/api/v1/semantic-assets", headers=headers).json()["total"] == 0
    result = api.get(f"/api/v1/semantic-assets/{installed.name}", headers=headers)
    assert result.status_code == 404 and result.json()["code"] == "SEMANTIC_ASSET_NOT_FOUND"
