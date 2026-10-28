"""Connection availability during non-database work and worker saturation."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import anyio
import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException, Request, Response
from minio.error import S3Error
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import QueuePool

from app.api.routers import shares, web
from app.core import security
from app.core.config import get_settings
from app.db import models
from app.main import build_app
from app.schemas.share import ShareCreate
from app.schemas.token import RelayTokenRequest, TokenMode
from app.services import auth_service, token_service


@pytest.fixture
def sync_db(tmp_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'sync.db'}",
        poolclass=QueuePool,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.1,
    )
    models.Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = models.User(email="sync@example.com", password_hash="unused", is_active=True)
        db.add(user)
        db.flush()
        share = models.Share(
            owner_user_id=user.id,
            path="notes",
            kind=models.ShareKind.FOLDER,
            visibility=models.ShareVisibility.PRIVATE,
            web_folder_items=[{"path": "a.bin", "storage_key": "test/a.bin"}],
        )
        db.add(share)
        db.flush()
        raw_key = "test-sync-key"
        db.add(
            models.ShareAgentKey(
                share_id=share.id,
                created_by=user.id,
                key_hash=hashlib.sha256(raw_key.encode()).hexdigest(),
                scopes="read,write",
                label="test",
            )
        )
        user_id, share_id = user.id, share.id
        db.commit()
    try:
        yield engine, user_id, share_id, raw_key
    finally:
        engine.dispose()


@pytest.mark.parametrize("auth", ["user", "key"])
def test_sync_download_releases_connection_before_storage(sync_db, monkeypatch, auth):
    engine, user_id, share_id, raw_key = sync_db
    storage = MagicMock()
    storage.get_object.return_value.read.return_value = b"file bytes"
    with Session(engine) as db:
        user = db.get(models.User, user_id) if auth == "user" else None
        request = Request(
            {
                "type": "http",
                "headers": [(b"x-agent-key", raw_key.encode())] if user is None else [],
            }
        )

        def get_object(*args):
            assert not db.in_transaction(), "database transaction held during object storage I/O"
            with engine.connect() as available:
                assert available.execute(select(1)).scalar() == 1
            return storage.get_object.return_value

        storage.get_object.side_effect = get_object
        monkeypatch.setattr(shares, "_get_minio_client", lambda: storage)
        response = shares.download_share_file(request, share_id, "a.bin", db, user)
        assert response.body == b"file bytes"
        storage.get_object.return_value.close.assert_called_once()
        storage.get_object.return_value.release_conn.assert_called_once()


@pytest.mark.parametrize("route", ["head", "download-url", "content"])
def test_cas_read_releases_connection_before_storage(sync_db, monkeypatch, route):
    engine, user_id, share_id, _ = sync_db
    token = security.create_file_token(
        subject=str(user_id),
        share_id=str(share_id),
        path="a.bin",
        sha256="abc",
        content_type="application/octet-stream",
        content_length=10,
    )
    storage = MagicMock()
    storage.get_object.return_value.read.return_value = b"file bytes"
    storage.get_object.return_value.headers = {"Content-Type": "application/octet-stream"}
    with Session(engine) as db:

        def available(*args):
            assert not db.in_transaction(), "database transaction held during object storage I/O"
            with engine.connect() as conn:
                assert conn.execute(select(1)).scalar() == 1
            return storage.get_object.return_value

        storage.get_object.side_effect = available
        storage.stat_object.side_effect = available
        monkeypatch.setattr(shares, "_get_minio_client", lambda: storage)
        if route == "head":
            response = shares.head_share_file(share_id, "a.bin", db, f"Bearer {token}")
            assert response.status_code == 200
        elif route == "download-url":
            response = shares.get_file_download_url(share_id, "a.bin", db, f"Bearer {token}")
            assert "/content?token=" in response.downloadUrl
        else:
            response = shares.get_file_content(share_id, "a.bin", token, db)
            assert response.body == b"file bytes"


def test_relay_token_returns_connection_after_audit(sync_db):
    engine, user_id, share_id, _ = sync_db
    app = SimpleNamespace(
        state=SimpleNamespace(
            relay_private_key=Ed25519PrivateKey.generate(),
            relay_key_id="test-key",
        )
    )
    request = Request({"type": "http", "headers": [], "app": app})
    with Session(engine) as db:
        user = db.get(models.User, user_id)
        payload = RelayTokenRequest(share_id=share_id, doc_id=str(share_id), mode=TokenMode.READ)
        response = token_service.issue_relay_token(db, request, payload, user)
        assert response.token
        assert not db.in_transaction(), "audit refresh left an idle transaction open"
        with Session(engine) as observer:
            audit = observer.execute(select(models.AuditLog)).scalar_one()
            assert audit.action == models.AuditAction.TOKEN_ISSUED


def test_password_verification_does_not_occupy_pool(sync_db, monkeypatch):
    engine, user_id, _, _ = sync_db
    with Session(engine) as db:

        def verify(password, password_hash):
            assert not db.in_transaction()
            with engine.connect() as conn:
                assert conn.execute(select(1)).scalar() == 1
            return password == "password" and password_hash == "unused"

        monkeypatch.setattr(security, "verify_password", verify)
        user = auth_service.authenticate_user(db, "sync@example.com", "password")
        assert user.id == user_id


@pytest.mark.parametrize("route", ["sync-write", "cas-upload"])
def test_upload_body_does_not_occupy_pool(sync_db, monkeypatch, route):
    engine, user_id, share_id, raw_key = sync_db
    storage = MagicMock()
    monkeypatch.setattr(shares, "_get_minio_client", lambda: storage)
    with Session(engine) as db:

        async def receive():
            assert not db.in_transaction()
            with engine.connect() as conn:
                assert conn.execute(select(1)).scalar() == 1
            return {"type": "http.request", "body": b"new content", "more_body": False}

        request = Request(
            {
                "type": "http",
                "headers": [(b"x-agent-key", raw_key.encode())],
                "method": "PUT",
                "path": "/test",
                "client": ("127.0.0.1", 1234),
            },
            receive,
        )
        if route == "sync-write":
            result = asyncio.run(
                shares.sync_write_file(
                    request,
                    Response(),
                    share_id,
                    "new.bin",
                    None,
                    "*",
                    db,
                    None,
                )
            )
            assert result.sha256 == hashlib.sha256(b"new content").hexdigest()
            db.close()
            with Session(engine) as observer:
                items = observer.get(models.Share, share_id).web_folder_items
                assert any(item["path"] == "new.bin" for item in items)
        else:
            token = security.create_file_token(
                subject=str(user_id),
                share_id=str(share_id),
                path="new.bin",
                sha256="abc",
                content_type="application/octet-stream",
                content_length=11,
            )
            response = asyncio.run(shares.put_file_content(request, share_id, "new.bin", token, db))
            assert response.status_code == 200
        storage.put_object.assert_called_once()


def test_billing_wait_does_not_occupy_pool(sync_db, monkeypatch):
    engine, user_id, _, _ = sync_db
    settings = shares.get_settings().model_copy(update={"billing_enabled": True})
    monkeypatch.setattr(shares, "get_settings", lambda: settings)
    with Session(engine) as db:

        async def check_limit(*args):
            assert not db.in_transaction()
            with engine.connect() as conn:
                assert conn.execute(select(1)).scalar() == 1

        monkeypatch.setattr(shares.billing_service, "check_limit", check_limit)
        request = Request(
            {
                "type": "http",
                "headers": [],
                "method": "POST",
                "path": "/test",
                "client": ("127.0.0.1", 1234),
            }
        )
        user = db.get(models.User, user_id)
        result = asyncio.run(
            shares.create_share(
                request,
                ShareCreate(path="another", kind=models.ShareKind.FOLDER),
                db,
                user,
            )
        )
        assert result.path == "another"


@pytest.mark.parametrize(
    "path,marker",
    [
        ("/metrics", "control_plane_info"),
        ("/v1/health", '"ok":true'),
        ("/v1/health/live", '"status":"healthy"'),
    ],
)
def test_probes_respond_when_sync_worker_pool_is_busy(path, marker):
    async def exercise():
        limiter = anyio.to_thread.current_default_thread_limiter()
        previous = limiter.total_tokens
        limiter.total_tokens = 1
        try:
            async with limiter:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=build_app()),
                    base_url="http://test",
                ) as client:
                    response = await asyncio.wait_for(client.get(path), timeout=0.5)
                    assert response.status_code == 200
                    assert marker in response.text
        finally:
            limiter.total_tokens = previous

    asyncio.run(exercise())


def _sync_write_request(raw_key: str, **headers: str) -> Request:
    async def receive():
        return {"type": "http.request", "body": b"new content", "more_body": False}

    return Request(
        {
            "type": "http",
            "headers": [(b"x-agent-key", raw_key.encode())]
            + [(name.lower().encode(), value.encode()) for name, value in headers.items()],
            "method": "PUT",
            "path": "/test",
            "client": ("127.0.0.1", 1234),
        },
        receive,
    )


def _storage_failure(*args, **kwargs):
    raise S3Error(MagicMock(), "InternalError", "storage down", "/", "req", "host")


@pytest.mark.parametrize(
    "path,if_match,if_none_match,put_object,expected",
    [
        ("a.bin", '"' + "0" * 64 + '"', None, None, 412),  # stale
        ("a.bin", None, "*", None, 412),  # create over an existing file
        ("missing.bin", '"' + "0" * 64 + '"', None, None, 412),  # update of a missing file
        ("new.bin", None, "*", _storage_failure, 500),  # object store failure
    ],
    ids=["stale", "exists", "missing", "storage-failure"],
)
def test_refused_sync_write_releases_its_row_lock(
    sync_db, monkeypatch, path, if_match, if_none_match, put_object, expected
):
    # A refused write must end its transaction before the handler returns.
    # Leaving it open kept the share's row lock until get_db's cleanup, which
    # the next writer's blocked lock wait prevented from ever running.
    engine, _, share_id, raw_key = sync_db
    storage = MagicMock()
    storage.put_object.side_effect = put_object
    monkeypatch.setattr(shares, "_get_minio_client", lambda: storage)
    with Session(engine) as db:
        with pytest.raises(HTTPException) as refused:
            asyncio.run(
                shares.sync_write_file(
                    _sync_write_request(raw_key),
                    Response(),
                    share_id,
                    path,
                    if_match,
                    if_none_match,
                    db,
                    None,
                )
            )
        assert refused.value.status_code == expected
        assert not db.in_transaction()

    with Session(engine) as observer:
        items = observer.get(models.Share, share_id).web_folder_items
    assert items == [{"path": "a.bin", "storage_key": "test/a.bin"}]


def test_sync_write_critical_section_runs_off_the_event_loop(sync_db, monkeypatch):
    # The row-lock wait and the object-store write block. On the event loop
    # they would stall every other request the worker is serving.
    engine, _, share_id, raw_key = sync_db
    release = threading.Event()
    storage = MagicMock()
    storage.put_object.side_effect = lambda *args, **kwargs: release.wait(timeout=5)
    monkeypatch.setattr(shares, "_get_minio_client", lambda: storage)

    async def exercise(db):
        write = asyncio.create_task(
            shares.sync_write_file(
                _sync_write_request(raw_key),
                Response(),
                share_id,
                "new.bin",
                None,
                "*",
                db,
                None,
            )
        )
        while not storage.put_object.called:
            await asyncio.sleep(0.01)
        # The loop got control back while the write sits in storage I/O.
        assert not write.done()
        release.set()
        return await write

    with Session(engine) as db:
        result = asyncio.run(exercise(db))
    assert result.sha256 == hashlib.sha256(b"new content").hexdigest()


@pytest.fixture
def web_publish(monkeypatch):
    monkeypatch.setenv("WEB_PUBLISH_DOMAIN", "docs.test.com")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _web_upload(route: str, db: Session, share_id, raw_key: str, receive) -> dict:
    request = Request(
        {
            "type": "http",
            "headers": [(b"x-agent-key", raw_key.encode()), (b"content-type", b"text/markdown")],
            "method": "POST",
            "path": "/test",
            "client": ("127.0.0.1", 1234),
        },
        receive,
    )
    handler = web.upload_mesh_artifact if route == "mesh-upload" else web.sync_upload
    return asyncio.run(handler(request, str(share_id), "new.md", db))


async def _new_content():
    return {"type": "http.request", "body": b"new content", "more_body": False}


@pytest.mark.parametrize("route", ["mesh-upload", "sync-upload"])
def test_web_upload_holds_no_transaction_across_body_or_storage(
    sync_db, web_publish, monkeypatch, route
):
    # The mesh upload used to take the share's row lock first and keep it
    # across the body await; a second upload to the same share then blocked
    # the event loop on that lock and the worker deadlocked.
    engine, _, share_id, raw_key = sync_db
    storage = MagicMock()
    monkeypatch.setattr(web, "_get_minio_client", lambda: storage)
    with Session(engine) as db:

        def assert_pool_free(*args, **kwargs):
            assert not db.in_transaction()
            with engine.connect() as conn:
                assert conn.execute(select(1)).scalar() == 1

        async def receive():
            assert_pool_free()
            return await _new_content()

        storage.put_object.side_effect = assert_pool_free
        _web_upload(route, db, share_id, raw_key, receive)
    storage.put_object.assert_called_once()

    with Session(engine) as observer:
        items = observer.get(models.Share, share_id).web_folder_items
    assert any(item["path"] == "new.md" for item in items)


@pytest.mark.parametrize("route", ["mesh-upload", "sync-upload"])
def test_web_upload_keeps_index_entries_committed_meanwhile(
    sync_db, web_publish, monkeypatch, route
):
    # Another upload commits its entry while this one stores its bytes. The
    # locked re-read must see that entry: updating the session's copy from
    # before the body read silently dropped it.
    engine, _, share_id, raw_key = sync_db
    other_worker = create_engine(engine.url)

    def concurrent_upload(*args, **kwargs):
        with Session(other_worker) as db:
            share = db.get(models.Share, share_id)
            share.web_folder_items = [
                *share.web_folder_items,
                {"path": "other.md", "storage_key": "test/other.md"},
            ]
            db.commit()

    storage = MagicMock()
    storage.put_object.side_effect = concurrent_upload
    monkeypatch.setattr(web, "_get_minio_client", lambda: storage)
    try:
        with Session(engine) as db:
            _web_upload(route, db, share_id, raw_key, _new_content)
    finally:
        other_worker.dispose()

    with Session(engine) as observer:
        items = observer.get(models.Share, share_id).web_folder_items
    assert sorted(item["path"] for item in items) == ["a.bin", "new.md", "other.md"]
