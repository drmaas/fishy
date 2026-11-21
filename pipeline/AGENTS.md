# AGENTS.md

Rules for `pipeline/`. Also follow the repository [AGENTS.md](../AGENTS.md).

## Rules

- Pure helpers in `depth.py` and `commands.py` must stay unit-testable without GDAL, tippecanoe, or network access.
- `build.py` may shell out to `ogr2ogr`, `gdal_translate`, `gdaldem`, `tippecanoe`, and `pmtiles`. It reads a local directory. It does not download the statewide DNR archive.
- `upload.py` refuses to run unless `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, and `R2_BUCKET` are set. Those values must not appear in the argument list.
- New DNR field handling goes through `normalize_properties`. Positive depth on the tiles is `depth_ft`.
- Tests run with the standard library only: `pnpm run pipeline:test`.

## Commands

```bash
pnpm run pipeline:test
pnpm pipeline:sample
```