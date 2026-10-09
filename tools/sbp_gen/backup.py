"""Offline, byte-preserving backup and restore verification for data roots.

This module intentionally has no dependency on the review server, artifact
writers, model clients, or repository globals.  It copies only the canonical
filesystem roots selected by the backup contract and verifies them by path,
size, timestamp, and SHA-256 digest.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


SCHEMA_VERSION = 1
MANIFEST_NAME = "manifest.json"
DATA_DIR_NAME = "data"
REQUIRED_ROOTS = ("cases", "runs")
OPTIONAL_ROOTS = ("outputs", "deliverables")
ALLOWED_ROOTS = REQUIRED_ROOTS + OPTIONAL_ROOTS


class BackupError(RuntimeError):
    """A safe, operator-facing backup failure."""


def _absolute(path: str | os.PathLike[str], *, label: str) -> Path:
    try:
        value = Path(path).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise BackupError(f"invalid {label}: {path!r}") from exc
    if not str(value):
        raise BackupError(f"invalid {label}: {path!r}")
    return value


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _ensure_regular_or_directory(path: Path, *, context: str) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise BackupError(f"cannot inspect {context}: {path}") from exc
    if stat.S_ISLNK(mode):
        raise BackupError(f"symlink is not allowed in {context}: {path}")
    if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
        raise BackupError(f"special filesystem entry is not allowed in {context}: {path}")


def _validate_source_root(source: Path) -> None:
    if not source.is_dir() or source.is_symlink():
        raise BackupError(f"source data root is not a directory: {source}")
    for name in REQUIRED_ROOTS:
        root = source / name
        if not root.is_dir() or root.is_symlink():
            raise BackupError(f"required data root is missing: {root}")
        _ensure_regular_or_directory(root, context="source data root")


def _validate_rel(value: object, *, kind: str) -> str:
    if not isinstance(value, str) or not value:
        raise BackupError(f"manifest {kind} path is not a non-empty string")
    if "\\" in value:
        raise BackupError(f"manifest {kind} path is not POSIX-form: {value!r}")
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise BackupError(f"manifest {kind} path is unsafe: {value!r}")
    if parsed.as_posix() != value:
        raise BackupError(f"manifest {kind} path is not normalized: {value!r}")
    if parsed.parts[0] not in ALLOWED_ROOTS:
        raise BackupError(f"manifest {kind} path is outside allowed roots: {value!r}")
    return value


def _inventory(source: Path) -> tuple[list[str], list[dict[str, Any]], list[str]]:
    directories: set[str] = set()
    files: list[dict[str, Any]] = []
    included_roots: list[str] = []
    for root_name in ALLOWED_ROOTS:
        root = source / root_name
        if not root.exists():
            if root_name in REQUIRED_ROOTS:
                raise BackupError(f"required data root is missing: {root}")
            continue
        _ensure_regular_or_directory(root, context="source data root")
        included_roots.append(root_name)
        directories.add(root_name)
        for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            for name in list(dirnames):
                child = current_path / name
                _ensure_regular_or_directory(child, context="source directory tree")
                directories.add(child.relative_to(source).as_posix())
            for name in filenames:
                child = current_path / name
                _ensure_regular_or_directory(child, context="source file tree")
                relative = child.relative_to(source).as_posix()
                digest = _sha256_file(child)
                try:
                    metadata = child.stat()
                except OSError as exc:
                    raise BackupError(f"cannot stat source file: {child}") from exc
                files.append({
                    "path": relative,
                    "size": metadata.st_size,
                    "sha256": digest,
                    "mtime_ns": metadata.st_mtime_ns,
                })
    files.sort(key=lambda entry: entry["path"])
    return sorted(directories), files, sorted(included_roots)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise BackupError(f"cannot read file: {path}") from exc
    return "sha256:" + digest.hexdigest()


def _manifest_without_digest(manifest: dict[str, Any]) -> dict[str, Any]:
    value = dict(manifest)
    value.pop("manifest_digest", None)
    return value


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _manifest_digest(manifest: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(
        _canonical_json(_manifest_without_digest(manifest))
    ).hexdigest()


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    try:
        with path.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(manifest, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise BackupError(f"cannot write manifest: {path}") from exc


def _read_manifest(package: Path) -> dict[str, Any]:
    path = package / MANIFEST_NAME
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError(f"manifest is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise BackupError("manifest must be a JSON object")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise BackupError(f"unsupported manifest schema_version: {value.get('schema_version')!r}")
    if value.get("manifest_digest") != _manifest_digest(value):
        raise BackupError("manifest_digest does not match manifest content")
    roots = value.get("included_roots")
    if not isinstance(roots, list) or roots != sorted(set(roots)):
        raise BackupError("manifest included_roots must be sorted and unique")
    if any(not isinstance(root, str) or root not in ALLOWED_ROOTS for root in roots):
        raise BackupError("manifest contains an unsupported included root")
    if any(root not in roots for root in REQUIRED_ROOTS):
        raise BackupError("manifest does not include all required roots")
    directories = value.get("directories")
    if not isinstance(directories, list) or directories != sorted(set(directories)):
        raise BackupError("manifest directories must be sorted and unique")
    files = value.get("files")
    if not isinstance(files, list):
        raise BackupError("manifest files must be a list")
    for directory in directories:
        _validate_rel(directory, kind="directory")
    seen: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise BackupError("manifest file entry must be an object")
        relative = _validate_rel(entry.get("path"), kind="file")
        if relative in seen:
            raise BackupError(f"manifest contains duplicate file: {relative}")
        seen.add(relative)
        if not isinstance(entry.get("size"), int) or entry["size"] < 0:
            raise BackupError(f"manifest size is invalid for: {relative}")
        if (
            not isinstance(entry.get("sha256"), str)
            or not entry["sha256"].startswith("sha256:")
            or len(entry["sha256"]) != 71
        ):
            raise BackupError(f"manifest sha256 is invalid for: {relative}")
        if not isinstance(entry.get("mtime_ns"), int) or entry["mtime_ns"] < 0:
            raise BackupError(f"manifest mtime_ns is invalid for: {relative}")
    if [entry["path"] for entry in files] != sorted(seen):
        raise BackupError("manifest files must be sorted and unique")
    expected_roots = {path.split("/", 1)[0] for path in directories + [entry["path"] for entry in files]}
    if expected_roots != set(roots):
        raise BackupError("manifest included_roots does not match its contents")
    return value


def _actual_tree(data: Path) -> tuple[list[str], list[str]]:
    if not data.is_dir() or data.is_symlink():
        raise BackupError(f"package data directory is missing or unsafe: {data}")
    directories: list[str] = []
    files: list[str] = []
    for current, dirnames, filenames in os.walk(data, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in list(dirnames):
            child = current_path / name
            _ensure_regular_or_directory(child, context="package tree")
            directories.append(child.relative_to(data).as_posix())
        for name in filenames:
            child = current_path / name
            _ensure_regular_or_directory(child, context="package tree")
            files.append(child.relative_to(data).as_posix())
    return sorted(directories), sorted(files)


def _verify_data(data: Path, manifest: dict[str, Any]) -> None:
    directories, files = _actual_tree(data)
    expected_directories = list(manifest["directories"])
    expected_files = [entry["path"] for entry in manifest["files"]]
    if directories != expected_directories:
        raise BackupError(
            "directory inventory mismatch: "
            f"expected {expected_directories!r}, found {directories!r}"
        )
    if files != expected_files:
        raise BackupError(
            "file inventory mismatch: "
            f"expected {expected_files!r}, found {files!r}"
        )
    for entry in manifest["files"]:
        path = data / Path(*PurePosixPath(entry["path"]).parts)
        try:
            metadata = path.stat()
        except OSError as exc:
            raise BackupError(f"cannot stat packaged file: {entry['path']}") from exc
        if metadata.st_size != entry["size"]:
            raise BackupError(f"size mismatch: {entry['path']}")
        if metadata.st_mtime_ns != entry["mtime_ns"]:
            raise BackupError(f"mtime mismatch: {entry['path']}")
        if _sha256_file(path) != entry["sha256"]:
            raise BackupError(f"sha256 mismatch: {entry['path']}")


def _verify_package_layout(package: Path) -> None:
    if not package.is_dir() or package.is_symlink():
        raise BackupError(f"backup package is not a directory: {package}")
    manifest = package / MANIFEST_NAME
    data = package / DATA_DIR_NAME
    if not manifest.is_file() or manifest.is_symlink():
        raise BackupError(f"backup package manifest is missing: {manifest}")
    if not data.is_dir() or data.is_symlink():
        raise BackupError(f"backup package data directory is missing: {data}")
    for entry in package.iterdir():
        if entry.name not in {MANIFEST_NAME, DATA_DIR_NAME}:
            raise BackupError(f"unexpected backup package entry: {entry.name}")


def create_backup(
    source_root: str | os.PathLike[str],
    package_path: str | os.PathLike[str],
) -> dict[str, Any]:
    source = _absolute(source_root, label="source data root")
    package = _absolute(package_path, label="backup package")
    _validate_source_root(source)
    if _is_within(package, source) or _is_within(source, package):
        raise BackupError("backup package and source data root must be separate")
    if package.exists() or package.is_symlink():
        raise BackupError(f"backup package already exists: {package}")
    package.parent.mkdir(parents=True, exist_ok=True)

    directories, files, included_roots = _inventory(source)
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "included_roots": included_roots,
        "directories": directories,
        "files": files,
    }
    manifest["manifest_digest"] = _manifest_digest(manifest)

    temporary = Path(tempfile.mkdtemp(prefix=f".{package.name}.tmp-", dir=str(package.parent)))
    try:
        data = temporary / DATA_DIR_NAME
        for relative in directories:
            (data / Path(*PurePosixPath(relative).parts)).mkdir(parents=True, exist_ok=True)
        for entry in files:
            source_file = source / Path(*PurePosixPath(entry["path"]).parts)
            target_file = data / Path(*PurePosixPath(entry["path"]).parts)
            target_file.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(source_file, target_file, follow_symlinks=False)
            except OSError as exc:
                raise BackupError(f"cannot copy source file: {entry['path']}") from exc
        _write_manifest(temporary / MANIFEST_NAME, manifest)
        _verify_package_layout(temporary)
        _verify_data(data, _read_manifest(temporary))
        try:
            os.replace(temporary, package)
        except OSError as exc:
            raise BackupError(f"cannot publish backup package: {package}") from exc
        temporary = None  # type: ignore[assignment]
    finally:
        if temporary is not None and temporary.exists():
            shutil.rmtree(temporary)
    return _result(
        "create", package, manifest, data_root=source,
    )


def verify_package(package_path: str | os.PathLike[str]) -> dict[str, Any]:
    package = _absolute(package_path, label="backup package")
    _verify_package_layout(package)
    manifest = _read_manifest(package)
    _verify_data(package / DATA_DIR_NAME, manifest)
    return _result("verify", package, manifest)


def restore_verify(
    package_path: str | os.PathLike[str],
    target_root: str | os.PathLike[str],
) -> dict[str, Any]:
    package = _absolute(package_path, label="backup package")
    target = _absolute(target_root, label="target data root")
    if _is_within(target, package):
        raise BackupError("target data root must not be inside the backup package")
    _verify_package_layout(package)
    manifest = _read_manifest(package)
    _verify_data(package / DATA_DIR_NAME, manifest)
    existed = target.exists()
    if existed:
        if target.is_symlink() or not target.is_dir():
            raise BackupError(f"target data root is not a directory: {target}")
        try:
            if next(target.iterdir(), None) is not None:
                raise BackupError(f"target data root is not empty: {target}")
        except OSError as exc:
            raise BackupError(f"cannot inspect target data root: {target}") from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.restore-", dir=str(target.parent)))
    try:
        source_data = package / DATA_DIR_NAME
        for relative in manifest["directories"]:
            (temporary / Path(*PurePosixPath(relative).parts)).mkdir(parents=True, exist_ok=True)
        for entry in manifest["files"]:
            source_file = source_data / Path(*PurePosixPath(entry["path"]).parts)
            target_file = temporary / Path(*PurePosixPath(entry["path"]).parts)
            target_file.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(source_file, target_file, follow_symlinks=False)
            except OSError as exc:
                raise BackupError(f"cannot restore file: {entry['path']}") from exc
        _verify_data(temporary, manifest)
        try:
            os.replace(temporary, target)
        except OSError as exc:
            raise BackupError(f"cannot publish restored data root: {target}") from exc
        temporary = None  # type: ignore[assignment]
    finally:
        if temporary is not None and temporary.exists():
            shutil.rmtree(temporary)
    return _result("restore-verify", package, manifest, target_root=target)


def _result(
    operation: str,
    package: Path,
    manifest: dict[str, Any],
    *,
    data_root: Path | None = None,
    target_root: Path | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "operation": operation,
        "package": str(package),
        "included_roots": manifest["included_roots"],
        "directory_count": len(manifest["directories"]),
        "file_count": len(manifest["files"]),
        "manifest_digest": manifest["manifest_digest"],
    }
    if data_root is not None:
        result["data_root"] = str(data_root)
    if target_root is not None:
        result["target_data_root"] = str(target_root)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m sbp_gen.backup",
        description="Create and verify offline filesystem data-root backups.",
    )
    subparsers = parser.add_subparsers(dest="operation", required=True)
    create = subparsers.add_parser("create", help="create a new backup package")
    create.add_argument("--data-root", required=True, type=Path)
    create.add_argument("--package", required=True, type=Path)
    verify = subparsers.add_parser("verify", help="verify an existing backup package")
    verify.add_argument("--package", required=True, type=Path)
    restore = subparsers.add_parser("restore-verify", help="restore and verify into a clean target")
    restore.add_argument("--package", required=True, type=Path)
    restore.add_argument("--data-root", required=True, type=Path)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        if args.operation == "create":
            result = create_backup(args.data_root, args.package)
        elif args.operation == "verify":
            result = verify_package(args.package)
        else:
            result = restore_verify(args.package, args.data_root)
    except BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
