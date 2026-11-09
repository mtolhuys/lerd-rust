---
name: lerd-rust
description: Rust sites served by lerd through the rust plugin. Use when editing, debugging or checking a Rust (Cargo) web app that lerd serves, or when a Rust lerd site shows a 502, an old version, or does not pick up changes.
---

# Rust sites on lerd

A Rust site on lerd is a host-proxy site: lerd runs its dev command on the host and nginx proxies the site's domain to it. With this plugin the dev command is `lerd rust:dev`, which rebuilds the app with cargo whenever a file is saved.

## After editing Rust code

1. Save the files. Do not run `cargo run` yourself and do not restart the site: `lerd rust:dev` notices the change, builds, and swaps the running app only when the build succeeds.
2. Call `rust_status` with the site name to see the result. While `dev_server.building_since` is set the build is still running; call again shortly.
3. If `dev_server.build.ok` is false, the errors are in `dev_server.build.diagnostics`, each with `file`, `line`, `message` and the compiler's full `rendered` explanation. Fix them and save again.
4. `dev_server.serving_previous: true` means the site still serves the previous successful build, so a browser shows old behaviour until the errors are fixed.

Use `rust_check` for a full `cargo check --all-targets` (tests, examples and benches too) when you want errors beyond the binary the dev server builds, or for a site whose dev server is not `lerd rust:dev`. It can take minutes on a first run.

Both tools take `branch` to look at one of the site's worktrees instead of its main checkout.

## When the site does not answer

- `dev_server.app.listening: false`: the app runs but nothing answers on the address lerd proxies to. The app must listen on the `HOST` and `PORT` environment variables lerd sets, never a hardcoded `127.0.0.1:3000`:

  ```rust
  let host = std::env::var("HOST").unwrap_or_else(|_| "127.0.0.1".into());
  let port = std::env::var("PORT").unwrap_or_else(|_| "3000".into());
  let listener = tokio::net::TcpListener::bind(format!("{host}:{port}")).await?;
  ```

- `dev_server.app_exit` is set: the app exited (a panic at start-up, a port already in use, a database not ready yet). While `dev_server.restart_at` is set the dev server starts it again by itself at that time, backing off from 1s to 60s; once it gives up after a few minutes, a save starts it. Its output is in the site's Dev Server log: `journalctl --user -u lerd-app-<site> -n 100`.
- `dev.watcher: false`: the site does not rebuild on save. Tell the user to run `lerd rust:setup` and then `lerd link` in the project; both ask them to confirm, so do not run them on their behalf.
- `dev_server.phase: "stopped"`: the dev server is not running, for example because the site is paused. `lerd unpause` or `lerd restart` in the project starts it.

## Services

Databases and caches lerd provides run in containers but publish on the host's loopback, which is where a host-proxy app reaches them: `127.0.0.1` and the service's published port. `lerd env` writes those into the project's `.env`.
