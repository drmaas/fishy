# AGENTS.md

Rules for code changes in this repository. Read [docs/architecture.md](docs/architecture.md) before changing data flow or shared depth rules. Do not invent behavior that those files do not describe.

## When to use

Use this file for any change under this repository.

Also read the nearest package file before editing that package:

- [apps/web/AGENTS.md](apps/web/AGENTS.md)
- [pipeline/AGENTS.md](pipeline/AGENTS.md)
- [.github/workflows/AGENTS.md](.github/workflows/AGENTS.md)

## Rules

- Keep lake ids, depth conversion, rounding, and PMTiles URL rules in `packages/core` and `pipeline/fishy_pipeline/depth.py`. When those change, update `fixtures/contract.json` in the same change. Both contract tests must pass.
- Round display values half away from zero. `-1.5` at 0 digits becomes `-2`. Non-finite input becomes `0`.
- A DNR lake id is eight ASCII digits. `depth` on DNR contours is negative feet. The published depth attribute is positive `depth_ft`.
- Do not commit API keys, `.dev.vars`, `.env`, Cloudflare tokens, or R2 credentials. `MAPTILER_API_KEY` is a Worker secret. It is absent from `wrangler.jsonc`. An empty key must still build and deploy.
- Do not log a URL that can contain `MAPTILER_API_KEY`.
- Do not commit survey extracts, statewide zips, GeoTIFFs, or MBTiles. `fixtures/sample-pond` and the tiny PMTiles under `apps/web/public/samples/` are the only bathymetry in git. Sample Pond is synthetic.
- OpenStreetMap data stays under the ODbL. The map footer shows “© OpenStreetMap contributors” while a street basemap is on, plus “Bathymetry © Minnesota DNR” while depth layers are on.
- The default basemap is OpenFreeMap. It needs no key. A keyed basemap is off unless the Worker environment has a key.
- Do not create a `v*` tag, a GitHub release, or a deploy secret from an agent turn. Deploy is the `deploy` job in `ci.yml` on a push to `main` only. Pull requests skip it.
- Do not add a dependency on any other drmaas repository. Copy an idea if it helps, and keep this tree buildable on its own.

## Where to change what

| Change | Edit |
| --- | --- |
| Depth units, lake ids, tile URLs | `packages/core`, `pipeline/fishy_pipeline/depth.py`, `fixtures/contract.json` |
| Worker routes and the MapTiler proxy | `apps/web/lib/server/config.ts`, `apps/web/worker/index.ts` |
| Map and lake panel | `apps/web/src` |
| Contour and raster commands | `pipeline/fishy_pipeline` |
| CI or deploy | `.github/workflows` |

## Commands

From the repository root:

```bash
pnpm test
pnpm run pipeline:test
pnpm check
```

`pnpm test` runs workspace unit tests. `pnpm run pipeline:test` runs the Python pipeline unit tests. `pnpm check` runs lint, typecheck, both test suites, the Worker types check, the web build, and gitleaks.

## Examples

Good: change `roundHalfAwayFromZero` and `round_half_away_from_zero`, update `fixtures/contract.json`, and run the contract tests.

Bad: format a depth only inside `LakePanel.tsx`.

Good: read `MAPTILER_API_KEY` from the Worker env.

Bad: write the key into `wrangler.jsonc`, a fixture, or a log line.