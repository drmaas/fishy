# AGENTS.md

Rules for `crates/fishy-core`. Also follow the repository [AGENTS.md](../../AGENTS.md).

## Rules

- This crate is the Rust copy of the rules in `packages/core`. The web app does not call this crate. A future Android or iOS client can.
- Stay `publish = false`. Keep `SPDX-License-Identifier: GPL-3.0-only` on Rust sources.
- Contract coverage is `tests/contract.rs` against `fixtures/contract.json`. Do not fork the expected numbers into a second fixture.
- Do not add HTTP clients or tile decoding here.

## Commands

```bash
cargo test -p fishy-core --locked
cargo clippy -p fishy-core --all-targets --locked -- -D warnings
```
