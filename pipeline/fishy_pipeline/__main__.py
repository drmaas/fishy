# SPDX-License-Identifier: GPL-3.0-only
"""CLI entry: shared build/upload, or ``--state wi`` batch stages."""

from __future__ import annotations

import argparse
import sys

from fishy_pipeline.common import build as common_build
from fishy_pipeline.common import upload as common_upload
from fishy_pipeline.states.wi import contours as wi_contours
from fishy_pipeline.states.wi import gcp_review as wi_gcp_review
from fishy_pipeline.states.wi import georef as wi_georef
from fishy_pipeline.states.wi import ingest as wi_ingest
from fishy_pipeline.states.wi import pilot_setup as wi_pilot_setup

WI_COMMANDS = frozenset({"ingest", "georef", "contours", "gcp-review", "pilot-setup"})
SHARED_COMMANDS = frozenset({"build", "upload"})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fishy_pipeline")
    parser.add_argument(
        "--state",
        metavar="CODE",
        help="State-specific pipeline stage (only wisconsin: wi)",
    )
    sub = parser.add_subparsers(dest="command", metavar="command", required=True)

    p_build = sub.add_parser("build", help="Build PMTiles from a local lake directory")
    p_build.add_argument("source", metavar="SOURCE_DIR")
    p_build.add_argument("dest", metavar="DEST_DIR")

    p_upload = sub.add_parser("upload", help="Upload a PMTiles file to R2")
    p_upload.add_argument("file", metavar="FILE")
    p_upload.add_argument("key", metavar="OBJECT_KEY")

    sub.add_parser("ingest", help="(wi) Ingest DNR PDF bathymetry maps")
    sub.add_parser("georef", help="(wi) Georeference ingested map images")
    sub.add_parser("contours", help="(wi) Vectorize contours and tag depth_ft")
    sub.add_parser("gcp-review", help="(wi) Local GCP review UI (localhost only)")
    sub.add_parser(
        "pilot-setup",
        help="(wi) Bootstrap git-ignored pilot work dir (hydro, PDFs, rasters, georef)",
    )
    return parser


def dispatch(argv: list[str]) -> None:
    parser = build_parser()
    parsed, trailing = parser.parse_known_args(argv)
    state = parsed.state
    command = parsed.command

    if state and state != "wi":
        raise SystemExit(f"unknown state {state!r} (only wi is defined)")

    if state == "wi":
        if command not in WI_COMMANDS:
            raise SystemExit(f"command {command!r} is not valid with --state wi")
        extra = trailing
    else:
        if command in WI_COMMANDS:
            raise SystemExit(f"command {command!r} requires --state wi")
        extra = []

    if command == "build":
        if extra:
            parser.error("too many arguments for build")
        common_build.main([parsed.source, parsed.dest])
        return
    if command == "upload":
        if extra:
            parser.error("too many arguments for upload")
        common_upload.main([parsed.file, parsed.key])
        return
    if command == "ingest":
        wi_ingest.main(extra)
        return
    if command == "georef":
        wi_georef.main(extra)
        return
    if command == "contours":
        wi_contours.main(extra)
        return
    if command == "gcp-review":
        wi_gcp_review.main(extra)
        return
    if command == "pilot-setup":
        wi_pilot_setup.main(extra)
        return


def main() -> None:
    dispatch(sys.argv[1:])


if __name__ == "__main__":
    main()