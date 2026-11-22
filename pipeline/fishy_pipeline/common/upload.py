# SPDX-License-Identifier: GPL-3.0-only
"""Upload a PMTiles archive to R2. Tokens stay in the environment."""

from __future__ import annotations

import os
import subprocess
import sys

from fishy_pipeline.common.commands import r2_put_command


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        raise SystemExit("usage: python3 -m fishy_pipeline upload FILE KEY")
    bucket = os.environ.get("R2_BUCKET", "")
    command = r2_put_command(args[0], bucket, args[1], wrangler=os.environ.get("WRANGLER", "wrangler"))
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)