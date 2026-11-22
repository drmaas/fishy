# AGENTS.md

Rules for `pipeline/`. Also follow the repository [AGENTS.md](../AGENTS.md).

## Layout

- Shared logic: `fishy_pipeline/common/` (`depth.py`, `commands.py`, `build.py`, `upload.py`).
- Wisconsin batch stages: `fishy_pipeline/states/wi/` (`ingest.py` implemented; `georef.py`, `contours.py` stubs).
- Top-level `fishy_pipeline/depth.py` and `commands.py` re-export `common` for tests and docs.
- CLI: `python3 -m fishy_pipeline` with optional `--state wi`. Module entries `fishy_pipeline.build` and `fishy_pipeline.upload` remain for pnpm scripts.

## Rules

- Pure helpers in `common/depth.py` and `common/commands.py` must stay unit-testable without GDAL, tippecanoe, or network access.
- `common/build.py` may shell out to `ogr2ogr`, `gdal_translate`, `gdaldem`, `tippecanoe`, and `pmtiles`. It reads a local directory. It does not download the statewide DNR archive.
- `common/upload.py` refuses to run unless `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, and `R2_BUCKET` are set. Those values must not appear in the argument list.
- New DNR field handling goes through `normalize_properties`. Positive depth on the tiles is `depth_ft`.
- Wisconsin-only Python deps belong in `pyproject.toml` optional extra `wi`, not in default CI. `ingest` unit tests use stdlib only; PDF rendering in production uses PyMuPDF from the `wi` extra.
- WI download caches: `pipeline/.cache/wi/`. WI work output: `pipeline/work/wi/`. Do not commit catalog pages, hydro extracts, PDFs, or rasters.
- Tests run with the standard library only: `pnpm run pipeline:test`.

## Commands

```bash
pnpm run pipeline:test
pnpm pipeline:sample
```