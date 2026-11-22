# AGENTS.md

Rules for `pipeline/`. Also follow the repository [AGENTS.md](../AGENTS.md).

## Layout

- Shared logic: `fishy_pipeline/common/` (`depth.py`, `commands.py`, `build.py`, `upload.py`).
- Wisconsin batch stages (stubs): `fishy_pipeline/states/wi/` (`ingest.py`, `georef.py`, `contours.py`).
- Top-level `fishy_pipeline/depth.py` and `commands.py` re-export `common` for tests and docs.
- CLI: `python3 -m fishy_pipeline` with optional `--state wi`. Module entries `fishy_pipeline.build` and `fishy_pipeline.upload` remain for pnpm scripts.

## Rules

- Pure helpers in `common/depth.py` and `common/commands.py` must stay unit-testable without GDAL, tippecanoe, or network access.
- `common/build.py` may shell out to `ogr2ogr`, `gdal_translate`, `gdaldem`, `tippecanoe`, and `pmtiles`. It reads a local directory. It does not download the statewide DNR archive.
- `common/upload.py` refuses to run unless `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, and `R2_BUCKET` are set. Those values must not appear in the argument list.
- New DNR field handling goes through `normalize_properties`. Positive depth on the tiles is `depth_ft`.
- Wisconsin-only Python deps belong in `pyproject.toml` optional extra `wi`, not in default CI.
- Tests run with the standard library only: `pnpm run pipeline:test`.

## Commands

```bash
pnpm run pipeline:test
pnpm pipeline:sample
```