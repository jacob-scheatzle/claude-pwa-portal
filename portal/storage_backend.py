"""Pluggable blob storage for the portal's on-disk state.

The portal keeps several kinds of *blob* state outside the database: extracted
child-app bundles, per-user storage objects, uploaded branding assets, and
rendered share PDFs. Historically all of this lived on local disk under
``settings.data_dir``. That works for the Docker/Caddy product (one host, a
bind-mounted ``./data``) but not for the AWS deployment, where the Fargate
container filesystem is ephemeral and there may be no shared disk at all.

This module abstracts those reads/writes behind a small ``StorageBackend``
interface with two implementations:

- ``LocalStorageBackend`` — the default. Byte-for-byte the previous behavior:
  physical paths under ``data_dir``, ``flock``-based namespace locks, and
  ``FileResponse`` for serving (so range requests / etags are unchanged).
- ``S3StorageBackend`` — stores every blob as an S3 object (keys mirror the
  old relative paths). Serving reads the object into memory (objects are small
  and capped) and returns a plain ``Response``. Namespace locking uses
  PostgreSQL advisory locks when the DB is Postgres (the AWS pairing), falling
  back to an in-process lock otherwise.

Logical keys are posix-relative paths, identical to the old on-disk layout:
``storage/<slug>/<user_id>/<key>``, ``apps/<slug>/<path>``,
``branding/<name>``, ``shares/<token>.pdf``. Call ``get_storage()`` to obtain
the process-wide backend selected by ``settings.storage_backend``.

Security note: every consumer already validates user-supplied components
(``api._validate_key``, the manifest slug rules, the branding name whitelist).
The backend re-guards defensively — ``_normalize_key`` rejects ``..`` segments
and the local backend confirms the resolved path stays within ``data_dir`` —
so a logic slip upstream can't traverse out of the intended prefix.
"""
from __future__ import annotations

import os
import shutil
import zlib
import mimetypes
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Optional

import anyio
from starlette.responses import FileResponse, Response

from portal.config import settings

try:  # POSIX advisory locks; absent on Windows dev machines.
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None


@dataclass(frozen=True)
class StoredObject:
    """One stored blob: the full logical key and its size in bytes."""

    key: str
    size: int


def _guess_type(key: str) -> Optional[str]:
    mt, _ = mimetypes.guess_type(key)
    return mt


class StorageBackend:
    """Interface + shared helpers. Subclasses implement the storage-specific ops."""

    # ----- key handling -----

    @staticmethod
    def _normalize_key(key: str) -> str:
        """Return a clean posix key or raise ValueError on a traversal attempt.

        Strips a leading slash and ``.`` segments; rejects any ``..`` segment.
        Upstream callers validate the user-controlled parts already; this is
        defense in depth so the backend can never address a sibling prefix.
        """
        k = (key or "").strip().lstrip("/")
        parts = [p for p in k.split("/") if p not in ("", ".")]
        if not parts:
            raise ValueError("empty storage key")
        if any(p == ".." for p in parts):
            raise ValueError(f"unsafe storage key: {key!r}")
        return "/".join(parts)

    @staticmethod
    def namespace_prefix(app_slug: str, user_id: int) -> str:
        """Logical prefix for one app+user storage namespace."""
        return f"storage/{app_slug}/{user_id}"

    # ----- ops every subclass must provide -----

    def read_or_none(self, key: str) -> Optional[bytes]:
        raise NotImplementedError

    def write(self, key: str, data: bytes, *, content_type: Optional[str] = None) -> None:
        raise NotImplementedError

    def exists(self, key: str) -> bool:
        raise NotImplementedError

    def size(self, key: str) -> Optional[int]:
        raise NotImplementedError

    def delete(self, key: str) -> bool:
        """Delete a key; return True if it existed."""
        raise NotImplementedError

    def delete_prefix(self, prefix: str) -> None:
        raise NotImplementedError

    def list(self, prefix: str) -> list[StoredObject]:
        """All blobs under ``prefix`` (recursive), keys returned as full logical keys."""
        raise NotImplementedError

    def file_response(
        self,
        key: str,
        *,
        media_type: Optional[str] = None,
        headers: Optional[dict] = None,
    ) -> Response:
        """Return a Starlette response serving ``key`` (raises FileNotFoundError if absent)."""
        raise NotImplementedError

    def replace_tree(self, prefix: str, src_dir: Path) -> None:
        """Replace everything under ``prefix`` with the file tree at ``src_dir``."""
        raise NotImplementedError

    def _acquire_lock(self, app_slug: str, user_id: int):
        raise NotImplementedError

    def _release_lock(self, handle) -> None:
        raise NotImplementedError

    # ----- shared concrete helpers -----

    def read(self, key: str) -> bytes:
        data = self.read_or_none(key)
        if data is None:
            raise FileNotFoundError(key)
        return data

    def usage(self, prefix: str) -> int:
        """Total bytes stored under ``prefix`` (for quota accounting)."""
        return sum(o.size for o in self.list(prefix))

    @contextmanager
    def namespace_lock(self, app_slug: str, user_id: int):
        """Exclusive lock over one storage namespace, for sync callers
        (e.g. the app-tool ``store`` deliver, which runs in a worker thread)."""
        handle = self._acquire_lock(app_slug, user_id)
        try:
            yield
        finally:
            self._release_lock(handle)

    @asynccontextmanager
    async def namespace_lock_async(self, app_slug: str, user_id: int):
        """Async variant for the storage PUT handler. Acquires in a worker
        thread so the blocking wait can't stall the event loop.

        anyio's default ``abandon_on_cancel=False`` is load-bearing: if the
        client disconnects while we're blocked acquiring, the await stays
        shielded until granted, so the try/finally still runs and the handle
        can't leak. Do not make this acquisition cancellable.
        """
        handle = await anyio.to_thread.run_sync(self._acquire_lock, app_slug, user_id)
        try:
            yield
        finally:
            self._release_lock(handle)


class LocalStorageBackend(StorageBackend):
    """Filesystem backend rooted at ``settings.data_dir`` — the default."""

    def __init__(self) -> None:
        self._root = Path(settings.data_dir).resolve()
        # Per-(slug,user) in-process locks for the fcntl-less fallback. Keyed
        # identically to the flock path so both storage code paths serialize.
        self._fallback_locks: dict[tuple[str, int], Lock] = {}
        self._fallback_guard = Lock()

    def _abspath(self, key: str) -> Path:
        p = (self._root / self._normalize_key(key)).resolve()
        p.relative_to(self._root)  # raises ValueError if the key escaped root
        return p

    def _absprefix(self, prefix: str) -> Path:
        # A prefix may be a bare top-level dir like "storage"; normalize the
        # same way but tolerate it resolving to root itself for "".
        clean = "/".join(p for p in (prefix or "").strip().lstrip("/").split("/") if p not in ("", "."))
        p = (self._root / clean).resolve()
        p.relative_to(self._root)
        return p

    def read_or_none(self, key: str) -> Optional[bytes]:
        path = self._abspath(key)
        if not path.is_file():
            return None
        return path.read_bytes()

    def write(self, key: str, data: bytes, *, content_type: Optional[str] = None) -> None:
        path = self._abspath(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file on the same filesystem, then atomically rename
        # into place — a failed/partial write can never leave a corrupt object
        # or clobber the existing value at this key. The temp lives under a
        # dedicated ``.tmp`` dir so it never shows up in a prefix listing or
        # counts toward quota accounting.
        tmpdir = self._root / ".tmp"
        tmpdir.mkdir(parents=True, exist_ok=True)
        tmp = tmpdir / f"w-{os.urandom(8).hex()}"
        try:
            tmp.write_bytes(data)
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink()

    def exists(self, key: str) -> bool:
        return self._abspath(key).is_file()

    def size(self, key: str) -> Optional[int]:
        path = self._abspath(key)
        return path.stat().st_size if path.is_file() else None

    def delete(self, key: str) -> bool:
        path = self._abspath(key)
        if not path.is_file():
            return False
        path.unlink()
        return True

    def delete_prefix(self, prefix: str) -> None:
        shutil.rmtree(self._absprefix(prefix), ignore_errors=True)

    def list(self, prefix: str) -> list[StoredObject]:
        base = self._absprefix(prefix)
        if not base.exists():
            return []
        out: list[StoredObject] = []
        # Skip symlinks: nothing in the portal ever creates one under a managed
        # prefix, so a symlink here would be an out-of-band plant — don't follow
        # it into another tree or let it skew quota accounting.
        for p in base.rglob("*"):
            if p.is_file() and not p.is_symlink():
                out.append(StoredObject(key=p.relative_to(self._root).as_posix(), size=p.stat().st_size))
        return out

    def file_response(
        self,
        key: str,
        *,
        media_type: Optional[str] = None,
        headers: Optional[dict] = None,
    ) -> Response:
        path = self._abspath(key)
        if not path.is_file():
            raise FileNotFoundError(key)
        mt = media_type or _guess_type(key) or "application/octet-stream"
        return FileResponse(path, media_type=mt, headers=headers or {})

    def replace_tree(self, prefix: str, src_dir: Path) -> None:
        """Atomically swap ``data_dir/<prefix>`` to a copy of ``src_dir``.

        Copies ``src_dir`` into a staging directory *on the data filesystem*
        first (so ``os.replace`` is a same-filesystem atomic rename regardless
        of where ``src_dir`` lives — e.g. a /tmp extraction), then renames the
        old tree aside, swaps the new one in, and removes the old.
        """
        dest = self._abspath(prefix)
        dest.parent.mkdir(parents=True, exist_ok=True)
        token = os.urandom(6).hex()
        staging = dest.with_name(f".{dest.name}.new-{token}")
        backup = dest.with_name(f".{dest.name}.old-{token}")
        shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(src_dir, staging)
        try:
            if dest.exists():
                os.replace(dest, backup)
            os.replace(staging, dest)
        finally:
            shutil.rmtree(backup, ignore_errors=True)
            shutil.rmtree(staging, ignore_errors=True)

    # ----- flock-backed namespace locks (cross-process on one host) -----

    def _lock_path(self, app_slug: str, user_id: int) -> Path:
        base = self._root / "locks" / app_slug
        base.mkdir(parents=True, exist_ok=True)
        return base / f"{user_id}.lock"

    def _acquire_lock(self, app_slug: str, user_id: int):
        if fcntl is None:  # pragma: no cover - Windows fallback
            with self._fallback_guard:
                lock = self._fallback_locks.setdefault((app_slug, user_id), Lock())
            lock.acquire()
            return lock
        fd = os.open(self._lock_path(app_slug, user_id), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def _release_lock(self, handle) -> None:
        if fcntl is None:  # pragma: no cover - Windows fallback
            handle.release()
            return
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            os.close(handle)


class S3StorageBackend(StorageBackend):
    """S3 object-store backend for the AWS (Fargate) deployment."""

    def __init__(self) -> None:
        import boto3  # imported lazily so the base install needn't ship boto3

        if not settings.s3_bucket:
            raise RuntimeError("S3StorageBackend requires S3_BUCKET")
        self.bucket = settings.s3_bucket
        prefix = (settings.s3_prefix or "").strip().lstrip("/")
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        self._prefix = prefix
        self._client = boto3.client(
            "s3",
            region_name=settings.s3_region or None,
            endpoint_url=settings.s3_endpoint_url or None,
        )
        self._use_pg_lock = not settings.database_url.startswith("sqlite")
        self._fallback_locks: dict[tuple[str, int], Lock] = {}
        self._fallback_guard = Lock()

    # ----- key mapping -----

    def _obj_key(self, key: str) -> str:
        return self._prefix + self._normalize_key(key)

    def _obj_prefix(self, prefix: str) -> str:
        clean = "/".join(p for p in (prefix or "").strip().lstrip("/").split("/") if p not in ("", "."))
        full = self._prefix + clean
        return (full.rstrip("/") + "/") if full else ""

    def _to_logical(self, obj_key: str) -> str:
        return obj_key[len(self._prefix):] if self._prefix and obj_key.startswith(self._prefix) else obj_key

    # ----- ops -----

    def read_or_none(self, key: str) -> Optional[bytes]:
        from botocore.exceptions import ClientError

        try:
            resp = self._client.get_object(Bucket=self.bucket, Key=self._obj_key(key))
            return resp["Body"].read()
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound"):
                return None
            raise

    def write(self, key: str, data: bytes, *, content_type: Optional[str] = None) -> None:
        ct = content_type or _guess_type(key) or "application/octet-stream"
        self._client.put_object(
            Bucket=self.bucket, Key=self._obj_key(key), Body=data, ContentType=ct
        )

    def exists(self, key: str) -> bool:
        return self.size(key) is not None

    def size(self, key: str) -> Optional[int]:
        from botocore.exceptions import ClientError

        try:
            resp = self._client.head_object(Bucket=self.bucket, Key=self._obj_key(key))
            return int(resp["ContentLength"])
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound"):
                return None
            raise

    def delete(self, key: str) -> bool:
        existed = self.exists(key)
        self._client.delete_object(Bucket=self.bucket, Key=self._obj_key(key))
        return existed

    def _iter_keys(self, obj_prefix: str):
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=obj_prefix):
            for obj in page.get("Contents", []):
                yield obj["Key"], int(obj["Size"])

    def delete_prefix(self, prefix: str) -> None:
        keys = [k for k, _ in self._iter_keys(self._obj_prefix(prefix))]
        for i in range(0, len(keys), 1000):
            batch = [{"Key": k} for k in keys[i : i + 1000]]
            self._client.delete_objects(Bucket=self.bucket, Delete={"Objects": batch})

    def list(self, prefix: str) -> list[StoredObject]:
        return [
            StoredObject(key=self._to_logical(k), size=sz)
            for k, sz in self._iter_keys(self._obj_prefix(prefix))
        ]

    def file_response(
        self,
        key: str,
        *,
        media_type: Optional[str] = None,
        headers: Optional[dict] = None,
    ) -> Response:
        data = self.read_or_none(key)
        if data is None:
            raise FileNotFoundError(key)
        mt = media_type or _guess_type(key) or "application/octet-stream"
        return Response(content=data, media_type=mt, headers=headers or {})

    def replace_tree(self, prefix: str, src_dir: Path) -> None:
        target = self._obj_prefix(prefix)
        new_keys: set[str] = set()
        for f in sorted(src_dir.rglob("*")):
            if f.is_file() and not f.is_symlink():
                rel = f.relative_to(src_dir).as_posix()
                key = target + rel
                ct = _guess_type(rel) or "application/octet-stream"
                self._client.upload_file(
                    str(f), self.bucket, key, ExtraArgs={"ContentType": ct}
                )
                new_keys.add(key)
        # Reconcile: drop any object under the prefix that the new tree omits.
        stale = [k for k, _ in self._iter_keys(target) if k not in new_keys]
        for i in range(0, len(stale), 1000):
            batch = [{"Key": k} for k in stale[i : i + 1000]]
            self._client.delete_objects(Bucket=self.bucket, Delete={"Objects": batch})

    # ----- namespace locks: PostgreSQL advisory locks (or in-process) -----

    @staticmethod
    def _lock_keys(app_slug: str, user_id: int) -> tuple[int, int]:
        # pg_advisory_lock(int4, int4) wants two signed 32-bit ints. Fold the
        # slug's CRC and the user id into that range.
        def _int4(n: int) -> int:
            return ((n + 2**31) % 2**32) - 2**31

        return _int4(zlib.crc32(app_slug.encode("utf-8"))), _int4(user_id)

    def _acquire_lock(self, app_slug: str, user_id: int):
        if self._use_pg_lock:
            from sqlalchemy import text

            from portal.db import engine

            k1, k2 = self._lock_keys(app_slug, user_id)
            conn = engine.connect()
            conn.execute(text("SELECT pg_advisory_lock(:a, :b)"), {"a": k1, "b": k2})
            return ("pg", conn, k1, k2)
        with self._fallback_guard:
            lock = self._fallback_locks.setdefault((app_slug, user_id), Lock())
        lock.acquire()
        return ("mem", lock)

    def _release_lock(self, handle) -> None:
        if handle[0] == "pg":
            from sqlalchemy import text

            _, conn, k1, k2 = handle
            try:
                conn.execute(text("SELECT pg_advisory_unlock(:a, :b)"), {"a": k1, "b": k2})
            finally:
                conn.close()
        else:
            handle[1].release()


_backend: Optional[StorageBackend] = None


def get_storage() -> StorageBackend:
    """Return the process-wide storage backend selected by config."""
    global _backend
    if _backend is None:
        if settings.storage_backend == "s3":
            _backend = S3StorageBackend()
        else:
            _backend = LocalStorageBackend()
    return _backend
