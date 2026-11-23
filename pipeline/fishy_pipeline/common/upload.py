# SPDX-License-Identifier: GPL-3.0-only
"""Upload a PMTiles archive to R2. Tokens stay in the environment."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from fishy_pipeline.common.commands import r2_put_command
from fishy_pipeline.common.wi_r2 import WI_R2_PREFIX, assert_wi_object_uploadable


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


def upload_wi_artifacts(stage_dir: Path, *, dry_run: bool = False) -> list[str]:
    """Upload every file under ``stage_dir/wi/v1`` using matching object keys."""
    root = stage_dir / WI_R2_PREFIX
    if not root.is_dir():
        raise ValueError(f"staged artifacts not found: {root}")
    uploaded: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        key = str(path.relative_to(stage_dir)).replace("\\", "/")
        upload_file(path, key, dry_run=dry_run)
        uploaded.append(key)
    manifest = stage_dir / "upload-manifest.json"
    manifest.write_text(json.dumps({"keys": uploaded}, indent=2) + "\n", encoding="utf-8")
    return uploaded


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