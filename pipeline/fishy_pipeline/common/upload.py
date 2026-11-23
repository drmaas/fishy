# SPDX-License-Identifier: GPL-3.0-only
"""Upload a PMTiles archive to R2. Tokens stay in the environment."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from fishy_pipeline.common.commands import r2_put_command
from fishy_pipeline.common.wi_r2 import (
    CURRENT_POINTER_KEY,
    PACKAGE_MANIFEST_NAME,
    build_current_pointer,
    assert_wi_object_uploadable,
)


def upload_file(file_path: Path, key: str, *, dry_run: bool = False) -> None:
    assert_wi_object_uploadable(file_path, key)
    if dry_run:
        print(f"dry-run upload {file_path} -> {key}", flush=True)
        return
    bucket = os.environ.get("R2_BUCKET", "")
    command = r2_put_command(str(file_path), bucket, key, wrangler=os.environ.get("WRANGLER", "wrangler"))
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def upload_wi_artifacts(
    stage_dir: Path,
    *,
    dry_run: bool = False,
    put_object: Callable[[Path, str], None] | None = None,
) -> dict:
    """Upload only keys listed in ``package-manifest.json``; pointer is written last."""
    manifest_path = stage_dir / PACKAGE_MANIFEST_NAME
    if not manifest_path.is_file():
        raise ValueError(f"package manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    build_id = str(manifest.get("build_id") or "")
    if not build_id:
        raise ValueError("package manifest missing build_id")
    uploads = manifest.get("uploads")
    if not isinstance(uploads, list) or not uploads:
        raise ValueError("package manifest has no upload entries")

    def default_put(file_path: Path, key: str) -> None:
        upload_file(file_path, key, dry_run=dry_run)

    put = put_object or default_put
    uploaded: list[str] = []
    pointer_path = stage_dir / CURRENT_POINTER_KEY
    if pointer_path.is_file():
        pointer_path.unlink()

    lake_detail_keys: list[str] = []
    lakes_index_key = ""
    try:
        for entry in uploads:
            if not isinstance(entry, dict):
                raise ValueError("invalid upload entry in package manifest")
            rel = str(entry.get("local") or entry.get("key") or "")
            key = str(entry.get("key") or rel)
            if not rel or not key:
                raise ValueError("upload entry missing local/key")
            file_path = stage_dir / rel
            if not file_path.is_file():
                raise ValueError(f"staged file missing: {file_path}")
            put(file_path, key)
            uploaded.append(key)
            if key.endswith("/lakes_index.json") or key.endswith("lakes_index.json"):
                lakes_index_key = key
            elif "/lakes/" in key and key.endswith(".json"):
                lake_detail_keys.append(key)
    except Exception:
        if pointer_path.is_file():
            pointer_path.unlink()
        raise

    pointer_body = build_current_pointer(
        build_id,
        lake_detail_keys=lake_detail_keys,
        lakes_index_key=lakes_index_key,
    )
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    pointer_path.write_text(json.dumps(pointer_body, indent=2) + "\n", encoding="utf-8")
    put(pointer_path, CURRENT_POINTER_KEY)
    uploaded.append(CURRENT_POINTER_KEY)

    manifest["uploaded_keys"] = uploaded
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) not in {2, 3}:
        raise SystemExit("usage: python3 -m fishy_pipeline upload FILE KEY [--dry-run]")
    dry_run = False
    if len(args) == 3:
        if args[2] != "--dry-run":
            raise SystemExit("usage: python3 -m fishy_pipeline upload FILE KEY [--dry-run]")
        dry_run = True
    upload_file(Path(args[0]), args[1], dry_run=dry_run)