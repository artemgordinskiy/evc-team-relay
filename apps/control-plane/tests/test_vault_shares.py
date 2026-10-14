"""Multiple whole-vault shares per owner via optional vault_id (GH #265)."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.db import models
from app.services import share_service


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _login(client: TestClient) -> str:
    r = client.post(
        "/auth/login", json={"email": "bootstrap@example.com", "password": "super-secret"}
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _create(client: TestClient, token: str, **extra):
    body = {"kind": "folder", "path": "", "visibility": "private", **extra}
    return client.post("/shares", json=body, headers=_headers(token))


def test_two_root_shares_different_vault_ids_both_created(client: TestClient) -> None:
    token = _login(client)
    a = _create(client, token, vault_id="vault-a", vault_name="kanban")
    b = _create(client, token, vault_id="vault-b", vault_name="handbook")
    assert a.status_code == 201, a.text
    assert b.status_code == 201, b.text
    assert b.json()["vault_id"] == "vault-b"
    assert b.json()["vault_name"] == "handbook"


def test_same_vault_id_and_path_is_409(client: TestClient) -> None:
    token = _login(client)
    assert _create(client, token, vault_id="vault-a").status_code == 201
    assert _create(client, token, vault_id="vault-a").status_code == 409


def test_legacy_duplicate_without_vault_id_still_409(client: TestClient) -> None:
    token = _login(client)
    assert _create(client, token).status_code == 201
    assert _create(client, token).status_code == 409


def test_legacy_and_vault_share_coexist(client: TestClient) -> None:
    token = _login(client)
    assert _create(client, token).status_code == 201
    assert _create(client, token, vault_id="vault-a").status_code == 201


@pytest.fixture
def owner(db_session) -> models.User:
    user = models.User(
        email=f"vault-{uuid.uuid4().hex[:8]}@example.com",
        password_hash="x",
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _root(db_session, owner, vault_id):
    share = models.Share(
        kind=models.ShareKind.FOLDER,
        path="",
        vault_id=vault_id,
        visibility=models.ShareVisibility.PRIVATE,
        owner_user_id=owner.id,
    )
    db_session.add(share)
    db_session.commit()
    db_session.refresh(share)
    return share


def test_find_share_for_path_scopes_by_vault_id(db_session, owner) -> None:
    a = _root(db_session, owner, "vault-a")
    b = _root(db_session, owner, "vault-b")
    found_a = share_service.find_share_for_path(db_session, owner, "x.md", vault_id="vault-a")
    found_b = share_service.find_share_for_path(db_session, owner, "x.md", vault_id="vault-b")
    assert found_a is not None and found_a.id == a.id
    assert found_b is not None and found_b.id == b.id
    assert share_service.find_share_for_path(db_session, owner, "x.md", vault_id="nope") is None


def test_share_list_exposes_vault_identity(client: TestClient) -> None:
    token = _login(client)
    assert _create(client, token, vault_id="v-a", vault_name="kanban").status_code == 201
    assert _create(client, token, vault_id="v-b", vault_name="handbook").status_code == 201
    r = client.get("/shares", headers=_headers(token))
    assert r.status_code == 200, r.text
    pairs = {(s["vault_id"], s["vault_name"]) for s in r.json()}
    assert {("v-a", "kanban"), ("v-b", "handbook")} <= pairs
