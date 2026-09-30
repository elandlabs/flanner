# The flanner desktop app

A Tauri shell around the flanner this repository already ships. Everything
flanner does stays in Python; the app bundles a real Python with flanner
installed from PyPI, shows `flanner web` in a window, puts `flanner` and
`flanner-mcp` on PATH, and adds a tray: background sync, start at login,
updates, and a notice when something is waiting on you.

The design and its decisions are in the desktop PRD (`.plans/prd-desktop-app_*.md`,
managed by flanner, not committed).

## Layout

| Path | What it is |
|---|---|
| `bundle/build_runtime.py` | Builds the Python runtime: python-build-standalone 3.12, flanner, the marker file, isolation, trimming. Stdlib only. |
| `bundle/runtime.lock` | flanner's dependencies, pinned with hashes, for every platform. |
| `bundle/collect.py` | Gives a build's installers the version-free names the release uses. |
| `bundle/latest_json.py` | Writes `latest.json`, which installed apps read to find updates. |
| `src-tauri/` | The Rust shell. `main.rs` window, tray and setup flow; `flanner.rs` running flanner and the runtime copies; `sync.rs`, `waiting.rs`, `updates.rs`. |
| `src-tauri/tauri.bundle.json` | Bundling settings, used only when building installers. |
| `ui/index.html` | The loading page, the setup screen and the error screen. |
| `tests/smoke/` | Layer 2: a built runtime, driven the way the app and agents use it. |
| `tests/e2e/` | Layers 3 and 4: the app's window on Windows (WebView2) and Linux (tauri-driver), and updates on Windows. |

The Python side lives in `flanner/desktop.py` and three hidden commands:
`desktop-link` (writes the launchers), `desktop-probe` (what the setup
screen shows) and `desktop-connect` (PATH, then agent registration).

## Build and run locally

You need Rust (`rustup`), and on Windows Microsoft's C++ Build Tools, on
Linux `libwebkit2gtk-4.1-dev libayatana-appindicator3-dev librsvg2-dev`.

```bash
python -m build --wheel -o build/dist .
python desktop/bundle/build_runtime.py --flanner build/dist/flanner-*.whl
cargo build --manifest-path desktop/src-tauri/Cargo.toml
FLANNER_DESKTOP_RUNTIME=build/runtime FLANNER_HOME=/tmp/fh FLANNER_DESKTOP_DATA=/tmp/fd \
  desktop/src-tauri/target/debug/flanner-desktop
```

A development build uses the runtime in place; a bundled one copies its
own out of the install folder on first start.

| Variable | What it does |
|---|---|
| `FLANNER_DESKTOP_RUNTIME` | Use this runtime in place instead of the bundled one. |
| `FLANNER_DESKTOP_DATA` | Move the app's own folder (settings, logs, runtime copies). Tests always set it. |
| `FLANNER_HOME` | flanner's own home, as everywhere else. Tests always set it. |
| `FLANNER_DESKTOP_UPDATE_NOW` | Test builds only: install an update as soon as it is found. |
| `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=N` | Windows: open DevTools on the window, which the tests attach to. |

Logs are in the app's folder under `logs/`: `web.log`, `link.log`,
`init.log`, `connect.log`, `sync.log`, `updates.log`.

## Tests

```bash
# Layer 2: the runtime
FLANNER_RUNTIME=build/runtime pytest desktop/tests/smoke
# Layer 3, Windows: the window
FLANNER_DESKTOP_APP=desktop/src-tauri/target/debug/flanner-desktop.exe \
  FLANNER_RUNTIME=build/runtime pytest desktop/tests/e2e
# Layer 4, Windows: updates, against a test build with a throwaway key
(cd desktop/src-tauri && TAURI_CONFIG="$(python ../tests/e2e/update_signing.py)" cargo build)
FLANNER_DESKTOP_UPDATE_APP=desktop/src-tauri/target/debug/flanner-desktop.exe \
  FLANNER_RUNTIME=build/runtime pytest desktop/tests/e2e/test_updates_windows.py
# The Rust side
cargo test --manifest-path desktop/src-tauri/Cargo.toml
```

`desktop-ci.yml` runs all of these on pull requests that touch the app or
the parts of flanner it depends on. Linux drives the window through
tauri-driver instead; macOS builds and runs the Rust tests.

## Releasing

`desktop.yml` runs after `publish.yml` has put a release on PyPI. It builds
the runtime from exactly that version on Windows, macOS and Linux, bundles
and signs the app, uploads the installers to the same GitHub release, and
uploads `latest.json` last. A manual run with **unsigned** builds everything
and uploads nothing: use it to check the pipeline.

### One-time setup

1. **Updater key.** Generate it on your own machine and keep an offline
   backup. Losing it strands every installed app: no update will verify.

   ```bash
   cargo tauri signer generate -w ~/.tauri/flanner-updater.key
   ```

   Add the public key as the repository **variable** `TAURI_UPDATER_PUBKEY`,
   and the private key and its password as the **secrets**
   `TAURI_SIGNING_PRIVATE_KEY` and `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`.
2. **The `desktop` environment.** Create it under the repository's
   Settings, Environments, restrict it to tags `v*`, and put every secret
   below in it rather than in repository secrets.
3. **Apple** (Developer Program): a Developer ID Application certificate
   exported as base64 `.p12` (`APPLE_CERTIFICATE`, `APPLE_CERTIFICATE_PASSWORD`,
   `APPLE_SIGNING_IDENTITY`), and for notarisation `APPLE_ID`, an
   app-specific `APPLE_PASSWORD` and `APPLE_TEAM_ID`.
4. **Windows** (Azure Trusted Signing): an app registration with the
   Trusted Signing Certificate Profile Signer role (`AZURE_CLIENT_ID`,
   `AZURE_CLIENT_SECRET`, `AZURE_TENANT_ID` as secrets) and the account's
   `AZURE_SIGNING_ENDPOINT`, `AZURE_SIGNING_ACCOUNT` and
   `AZURE_SIGNING_PROFILE` as variables.

A signed release stops before building if any of these is missing, and
names what is missing.

### Release checklist

Automated layers cover what they can; these need a person and real
hardware, once per release candidate. Record the results in the release's
notes.

- [ ] The installer opens without a SmartScreen or Gatekeeper warning.
- [ ] The first start shows the setup screen with Connect ticked.
- [ ] With a pip flanner installed, the first start asks which to use and honours the choice.
- [ ] The folder dialog opens natively on macOS and returns a path.
- [ ] Tray: Open, background sync, start at login, Quit; after Quit no flanner process is left (Task Manager, Activity Monitor).
- [ ] Start at login works after a reboot, and background sync resumes.
- [ ] Background sync resumes after the laptop sleeps.
- [ ] Claude Desktop and Claude Code show flanner's tools after install, and still do after an update.
- [ ] An update from the previous release installs, and says to restart Claude and Codex.

#### Windows, recorded 2026-09-23 (unsigned local build of 0.14.0)

- Installer: 41.9 MB; a silent per-user install took 22 seconds and used 169 MB.
- First start: the runtime was copied out of the install folder and the
  setup screen showed with Connect ticked, 14 seconds after launch.
- Answered with Connect unticked: the window moved to flanner's
  dashboard, the launcher answered `flanner, version 0.14.0 (desktop)`.
- A silent uninstall removed the program, its uninstall entry and its
  Start-menu shortcut, and left no process running.
- Not covered by this pass: SmartScreen (the build was unsigned), start at
  login after a reboot, sleep and resume, and macOS and Linux entirely.
