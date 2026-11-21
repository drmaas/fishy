# AGENTS.md

Rules for `.github/workflows`. Also follow the repository [AGENTS.md](../../AGENTS.md).

## Workflows

`ci.yml` runs on pull requests, pushes to `main`, and `workflow_dispatch`.

- `web` uses `pnpm/setup@v3` at the repository root with Node LTS, store cache, and a frozen lockfile. The root `packageManager` is pnpm 12; this action cannot install pnpm 10. Order is install, `wrangler types --check`, lint, typecheck, test, build. `pnpm-workspace.yaml` sets `minimumReleaseAge: 0` so a release published the same day can still install.
- `pipeline` installs `gdal-bin` and runs the Python unit tests, including the EPSG:26915 reprojection fixture. It does not install tippecanoe and it does not download DNR data.
- `rust` uses `dtolnay/rust-toolchain@stable` with `toolchain: 1.83.0`, rustfmt, and clippy, then `cargo fmt`, `cargo clippy --all-targets --locked -- -D warnings`, and `cargo test --workspace --locked`.
- `secrets` runs gitleaks `v8.30.1` with `--redact` after checking the linux x64 tarball sha256. Checkout uses `fetch-depth: 0`.
- Workflow `permissions` are `contents: read`. The deploy job adds `deployments: write`.
- `deploy` needs the other four jobs. It runs only when `github.event_name == 'push'` and the ref is `main`. It rebuilds the web app, then deploys with `cloudflare/wrangler-action@v4`. It needs repository secrets `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID`. Pull requests and `workflow_dispatch` never reach this job.

## Rules

- Keep the Worker deploy job in `ci.yml`. Do not add a second workflow that uses `workflow_run`.
- Do not put `CLOUDFLARE_API_TOKEN` on `web`, `pipeline`, `rust`, or `secrets`. Only the `deploy` job may reference it.
- Do not commit either secret, and do not create them from a pull request.
- Keep `ci.yml` free of `contents: write`. The `deploy` job may use `deployments: write` for `gitHubToken`.
- Deploy concurrency group is `fishy-production` with `cancel-in-progress: false`.
- Cargo invocations in CI stay `--locked`.
- Pin the action major versions already in this file (`checkout@v7`, `pnpm/setup@v3`, `Swatinem/rust-cache@v2`, `cloudflare/wrangler-action@v4`). A gitleaks bump changes the download URL and this file together.
- Do not push a tag or call the deploy job to test it.

## Failures

- gitleaks exits non-zero when it finds a secret. Do not add `--no-git`.
- Deploy fails when `CLOUDFLARE_API_TOKEN` or `CLOUDFLARE_ACCOUNT_ID` is missing. That is expected until a person adds them.
