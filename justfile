set shell := ["bash", "-cu"]

export PATH := env_var("HOME") / ".local/bin:" + env_var("PATH")

default:
    @just --list

web *args:
    pnpm --filter @fishy/web {{ args }}

fmt:
    cargo fmt --all -- --check
    pnpm run lint

lint: fmt

typecheck:
    pnpm run typecheck
    cd apps/web && pnpm exec wrangler types --include-runtime false --check

test:
    cargo test --workspace --locked
    pnpm test
    PYTHONPATH=pipeline python3 -m unittest discover -s pipeline/tests -v

build:
    pnpm --filter @fishy/web build

dev:
    pnpm --filter @fishy/web dev

secrets:
    gitleaks detect --source . --redact --no-banner

clippy:
    cargo clippy --all-targets --locked -- -D warnings

check: fmt clippy typecheck test build secrets

pipeline-test:
    PYTHONPATH=pipeline python3 -m unittest discover -s pipeline/tests -v

# Rebuild the committed Sample Pond archives. Requires ogr2ogr, tippecanoe, gdal, and pmtiles.
pipeline-sample:
    PYTHONPATH=pipeline python3 -m fishy_pipeline.build fixtures/sample-pond apps/web/public/samples

# Upload one archive. Reads CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID, and R2_BUCKET from the environment.
r2-upload file key:
    PYTHONPATH=pipeline python3 -m fishy_pipeline.upload {{ file }} {{ key }}
