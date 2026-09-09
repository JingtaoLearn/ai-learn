from __future__ import annotations

import ctypes
import errno
import json
import os
import stat
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Protocol

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .production_contract import SHA256
from .production_jobs import ProductionJobError, inspect_staged_package


class PackageIdentityAuthorityError(RuntimeError):
    """Raised when the independent package identity authority cannot attest a generation."""


class PackageIdentityAuthorityClient(Protocol):
    def seal(self, production_run_id: str, stage: str) -> str: ...


def _generation_parts(production_run_id: str, stage: str) -> tuple[str, str]:
    if SHA256.fullmatch(production_run_id) is None or stage not in {"acquisition", "computation"}:
        raise PackageIdentityAuthorityError("package generation identity is invalid")
    return production_run_id, stage


def _identity_fingerprint(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_uid,
        value.st_gid,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _rename_noreplace(source: Path, target: Path) -> None:
    renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if renameat2 is None:
        raise OSError(
            errno.ENOSYS,
            "atomic no-replace rename is unavailable",
            str(target),
        )
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,
        os.fsencode(source),
        -100,
        os.fsencode(target),
        1,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(target))


class FilesystemPackageIdentityAuthority:
    """Create-once package identities owned outside the staging writer's filesystem authority."""

    def __init__(self, work_root: Path | str, identity_root: Path | str):
        self.work_root = Path(work_root).absolute()
        self.identity_root = Path(identity_root).absolute()
        if self.work_root == self.identity_root or self.work_root in self.identity_root.parents:
            raise PackageIdentityAuthorityError("identity root must be outside the work root")
        if self.identity_root in self.work_root.parents:
            raise PackageIdentityAuthorityError("work root must be outside the identity root")
        self._lock = threading.Lock()
        self._prepare_identity_root()

    def _prepare_identity_root(self) -> None:
        self.identity_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = os.stat(self.identity_root, follow_symlinks=False)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise PackageIdentityAuthorityError(
                "package identity root must be an authority-owned mode-0700 directory"
            )

    def _identity_path(self, production_run_id: str, stage: str) -> Path:
        run_id, stage_name = _generation_parts(production_run_id, stage)
        return self.identity_root / run_id / f"{stage_name}.sha256"

    @staticmethod
    def _read_identity(path: Path) -> str:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
            try:
                before = os.fstat(descriptor)
                path_before = os.stat(path, follow_symlinks=False)
                payload = os.read(descriptor, 66)
                after = os.fstat(descriptor)
                path_after = os.stat(path, follow_symlinks=False)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise PackageIdentityAuthorityError("package identity is unavailable") from exc
        fingerprint = _identity_fingerprint(before)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or before.st_mode & 0o222
            or fingerprint != _identity_fingerprint(path_before)
            or fingerprint != _identity_fingerprint(after)
            or fingerprint != _identity_fingerprint(path_after)
        ):
            raise PackageIdentityAuthorityError("package identity is unsafe")
        try:
            identity = payload.decode("ascii").removesuffix("\n")
        except UnicodeError as exc:
            raise PackageIdentityAuthorityError("package identity is invalid") from exc
        if len(payload) != 65 or not payload.endswith(b"\n") or SHA256.fullmatch(identity) is None:
            raise PackageIdentityAuthorityError("package identity is invalid")
        return identity

    def seal(self, production_run_id: str, stage: str) -> str:
        run_id, stage_name = _generation_parts(production_run_id, stage)
        target = self.work_root / run_id / stage_name
        label = "production input" if stage_name == "acquisition" else "production computation"
        try:
            observed = inspect_staged_package(target, label=label)
        except ProductionJobError as exc:
            raise PackageIdentityAuthorityError(str(exc)) from exc
        identity_path = self._identity_path(run_id, stage_name)
        with self._lock:
            identity_path.parent.mkdir(mode=0o700, exist_ok=True)
            root_descriptor = os.open(
                self.identity_root,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
            )
            try:
                os.fsync(root_descriptor)
            finally:
                os.close(root_descriptor)
            parent = os.stat(identity_path.parent, follow_symlinks=False)
            if (
                not stat.S_ISDIR(parent.st_mode)
                or parent.st_uid != os.geteuid()
                or stat.S_IMODE(parent.st_mode) != 0o700
            ):
                raise PackageIdentityAuthorityError("package identity namespace is unsafe")
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{identity_path.name}.",
                dir=identity_path.parent,
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(observed.encode("ascii") + b"\n")
                    stream.flush()
                    os.fchmod(stream.fileno(), 0o400)
                    os.fsync(stream.fileno())
                try:
                    _rename_noreplace(temporary, identity_path)
                except FileExistsError:
                    retained = self._read_identity(identity_path)
                    if retained != observed:
                        raise PackageIdentityAuthorityError(
                            "retained package identity conflicts with staged generation"
                        )
                    return retained
                except OSError as exc:
                    raise PackageIdentityAuthorityError(
                        "package identity cannot be retained"
                    ) from exc
            finally:
                temporary.unlink(missing_ok=True)
            parent_descriptor = os.open(
                identity_path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
            )
            try:
                os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)
            return self._read_identity(identity_path)


class HttpPackageIdentityAuthorityClient:
    def __init__(self, base_url: str):
        if base_url != "http://package-identity-authority:8091":
            raise PackageIdentityAuthorityError("package identity authority URL is invalid")
        self.base_url = base_url
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def seal(self, production_run_id: str, stage: str) -> str:
        run_id, stage_name = _generation_parts(production_run_id, stage)
        request = urllib.request.Request(
            f"{self.base_url}/v1/generations/{run_id}/{stage_name}/seal",
            data=b"",
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=10) as response:
                payload = response.read(1025)
                status = response.status
                final_url = response.geturl()
        except (OSError, urllib.error.URLError) as exc:
            raise PackageIdentityAuthorityError("package identity authority is unavailable") from exc
        if status != 200 or final_url != request.full_url or len(payload) > 1024:
            raise PackageIdentityAuthorityError("package identity authority response is invalid")
        try:
            document = json.loads(payload)
            identity = document["package_identity"]
        except (UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise PackageIdentityAuthorityError("package identity authority response is invalid") from exc
        if set(document) != {"package_identity"} or not isinstance(identity, str) or SHA256.fullmatch(identity) is None:
            raise PackageIdentityAuthorityError("package identity authority response is invalid")
        return identity


def create_package_identity_authority_app(
    authority: FilesystemPackageIdentityAuthority,
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health/live")
    async def live() -> JSONResponse:
        return JSONResponse({"status": "live"})

    @app.post("/v1/generations/{production_run_id}/{stage}/seal")
    async def seal(production_run_id: str, stage: str) -> JSONResponse:
        try:
            identity = await run_in_threadpool(authority.seal, production_run_id, stage)
        except PackageIdentityAuthorityError as exc:
            return JSONResponse(
                {"error": {"code": "PACKAGE_IDENTITY_REJECTED", "message": str(exc)}},
                status_code=409,
            )
        return JSONResponse({"package_identity": identity})

    return app


def main() -> None:
    import uvicorn

    work_root = os.environ.get("QR_PRODUCTION_WORK_ROOT", "")
    identity_root = os.environ.get("QR_PACKAGE_IDENTITY_ROOT", "")
    if not Path(work_root).is_absolute() or not Path(identity_root).is_absolute():
        raise RuntimeError("package authority roots must be configured as absolute paths")
    authority = FilesystemPackageIdentityAuthority(work_root, identity_root)
    uvicorn.run(
        create_package_identity_authority_app(authority),
        host="0.0.0.0",
        port=8091,
        workers=1,
    )


if __name__ == "__main__":
    main()
