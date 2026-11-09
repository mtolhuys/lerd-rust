# Rust

Serves Rust web apps the way lerd serves everything else, with one difference that matters for a compiled language: the app is rebuilt on every save, and a build that fails leaves the previous one serving. It also adds cargo commands to each site, a **Rust** tab with the dev server's last build, and tools that let an AI assistant read compiler errors.

The app runs natively on your machine as a lerd host-proxy site. Nothing runs in a container, and the plugin needs only `python3` and a Rust toolchain.

## Set up a site

In the Rust project:

```bash
lerd rust:setup     # shows the change to .lerd.yaml and asks before writing it
lerd link           # lerd shows the new dev command and asks you to approve it
```

`lerd rust:setup` sets the dev command under `proxy:` in `.lerd.yaml` to `lerd rust:dev`, keeping any `cargo run` arguments the old command had, or writes a new `.lerd.yaml` with HTTPS and a free port from 3100. Pass cargo run arguments after `--` to choose them: `lerd rust:setup -- --bin server`. `--dry-run` shows the change without writing it. A dev command that relies on the shell (a `$VARIABLE`, a pipe or a redirect) is left alone, with how to change it by hand.

`.lerd.yaml` then holds no path of your own, so it can be committed; anyone using it needs this plugin too.

Your app must listen on the `HOST` and `PORT` environment variables lerd sets:

```rust
let host = std::env::var("HOST").unwrap_or_else(|_| "127.0.0.1".into());
let port = std::env::var("PORT").unwrap_or_else(|_| "3000".into());
```

The dev server logs a warning, and the Rust tab shows one, when the app runs but nothing answers there.

## The dev server: lerd rust:dev

`lerd rust:dev [options] [-- <cargo run arguments>]` is the dev command lerd runs for the site. You can also run it in a terminal.

- It builds with `cargo build`, naming the same binary `cargo run` would run, while the current app keeps serving.
- After a successful build it stops the running app with everything it started, and starts the program that build made, with the environment `cargo run` would give it. A save that lands between the build and the start is never compiled into the running site; it is built next. When a build makes several programs and none was named, it uses `cargo run`.
- After a failed build it keeps the previous build serving and says so in the Dev Server log, on the Rust tab and, once per failure, in a notification if you allowed them.
- A save during a build stops that build and starts a new one.
- When the app exits on its own, for example because a database or a network address was not ready yet when the machine started, it is started again after 1, 2, 5, 10, 20, 30 and 60 seconds. If it keeps failing for a few minutes it waits for the next save instead, so a real error does not fill the log.
- It watches the whole cargo workspace, skipping cargo's target folder (wherever it is configured), `.git`, `node_modules`, hidden folders other than `.cargo`, `.lerd.yaml`, symbolic links, editor swap files, and what a running app writes: SQLite databases with their journals (`.db`, `.sqlite`, `-wal`, `-shm`, `-journal`) and `.log` files.

| Option | |
|---|---|
| `--watch PATH` | Watch only these paths, relative to the project (repeatable) |
| `--ignore PATH` | Never watch this path (repeatable) |
| `--poll MS` | How often to look for changes, default 500 |
| `--no-notify` | Never send a notification |

Lerd starts dev servers with a short `PATH`. The plugin finds cargo on it, or in `$CARGO_HOME/bin` and `~/.cargo/bin` where rustup installs it. To use another cargo, set its absolute path in the dev command: `LERD_RUST_CARGO=/opt/rust/bin/cargo lerd rust:dev`.

## The Rust tab

Each site gets a **Rust** tab:

- **Dev server build**: whether the site rebuilds on save, the last build's result and time, and every error and warning with its file, line and full compiler output. It refreshes on its own.
- **cargo check**: runs `cargo check --all-targets` and lists what it finds.

The package, its edition and the Rust version show only on the lerd host itself, since reading them runs cargo.

## Commands

| | |
|---|---|
| `lerd rust:dev` | The dev server, described above |
| `lerd rust:setup` | Make `lerd rust:dev` the site's dev command |
| `lerd rust:status [--json]` | The dev server's last build for the site in this folder |

Each site's Commands menu gets **Cargo check**, **Cargo test**, **Cargo clippy**, **Cargo fmt --check** and **Cargo fmt**, which asks first because it changes files. They run in the site's folder, or a worktree's when one is selected.

## For AI assistants

`lerd mcp:inject` adds a skill and two tools:

- `rust_status`: the dev server's setup and last build, with its errors.
- `rust_check`: runs `cargo check` and returns its errors and warnings.

## What it touches

It reads the project's `Cargo.toml` (through `cargo metadata`), its source files for changes, and `.lerd.yaml`. It writes `.lerd.yaml` only through `lerd rust:setup`, after you confirm, and keeps each dev server's last build in its own data folder. It talks only to lerd's API on this machine, to send the notification. It never reads the contents of `.env` (a change to it restarts the app) and sends nothing anywhere else.
