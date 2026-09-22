//! The flanner desktop app: a native window over flanner's own web UI.
//!
//! Everything flanner does stays in Python. This shell only:
//! - copies the bundled Python runtime out of the install folder, once per
//!   version, so an update never replaces files a running agent still uses;
//! - points the fixed launchers at it (`flanner desktop-link`);
//! - creates the store on a first start (`flanner init`, outside any repo);
//! - starts `flanner web` on a free port and shows it in the window;
//! - keeps a tray icon, and stops flanner when the app quits.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::fs;
use std::io::{self, Read, Seek, SeekFrom};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use tauri::menu::{Menu, MenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder, WindowEvent};

/// flanner.release.DESKTOP_MARKER. Written last by the builder, so a runtime
/// that has it is complete.
const MARKER: &str = "flanner-desktop";

/// Points at a runtime built by desktop/bundle/build_runtime.py, used in
/// place. For development, where no runtime is bundled.
const RUNTIME_ENV: &str = "FLANNER_DESKTOP_RUNTIME";

/// A cold first start imports FastAPI and SQLAlchemy from a fresh copy.
const START_TIMEOUT: Duration = Duration::from_secs(90);

/// The `flanner web` process, so quitting can stop it.
struct Sidecar(Mutex<Option<Child>>);

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            show(app)
        }))
        .plugin(tauri_plugin_dialog::init())
        .manage(Sidecar(Mutex::new(None)))
        .setup(|app| {
            WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title("Flanner")
                .inner_size(1280.0, 860.0)
                .min_inner_size(720.0, 520.0)
                .build()?;
            tray(app)?;
            let handle = app.handle().clone();
            thread::spawn(move || {
                if let Err(message) = boot(&handle) {
                    report(&handle, &message);
                }
            });
            Ok(())
        })
        .on_window_event(|window, event| {
            // Closing the window keeps flanner running in the tray. Quit stops it.
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .build(tauri::generate_context!())
        .expect("the app could not start")
        .run(|app, event| {
            if let RunEvent::Exit = event {
                stop(app);
            }
        });
}

fn boot(app: &AppHandle) -> Result<(), String> {
    let runtime = runtime(app)?;
    let python = python_in(&runtime);
    let data = app.path().app_local_data_dir().map_err(text)?;
    let logs = app.path().app_log_dir().map_err(text)?;
    fs::create_dir_all(&logs).map_err(text)?;

    flanner(&python, &["desktop-link"], &data, &logs.join("link.log"))?;
    if !store(app)?.is_file() {
        // Run from the app's own folder, which is never a repository, so a
        // first start creates the store and adopts nothing.
        let init = ["init", "--setup", "none", "--no-watch-skills"];
        flanner(&python, &init, &data, &logs.join("init.log"))?;
    }

    let port = free_port().map_err(text)?;
    let log = logs.join("web.log");
    let child = spawn_web(&python, port, &data, &log).map_err(text)?;
    app.state::<Sidecar>()
        .0
        .lock()
        .expect("sidecar lock")
        .replace(child);
    wait_until_serving(app, port, &log)?;

    let url = format!("http://127.0.0.1:{port}/").parse().map_err(text)?;
    main_window(app)?.navigate(url).map_err(text)
}

/// This version's runtime, copied out of the install folder on its first start.
fn runtime(app: &AppHandle) -> Result<PathBuf, String> {
    if let Some(dev) = std::env::var_os(RUNTIME_ENV) {
        return Ok(PathBuf::from(dev));
    }
    let version = app.package_info().version.to_string();
    let versions = app
        .path()
        .app_local_data_dir()
        .map_err(text)?
        .join("runtime");
    let target = versions.join(&version);
    if target.join(MARKER).is_file() {
        return Ok(target);
    }
    let bundled = app.path().resource_dir().map_err(text)?.join("runtime");
    if !bundled.join(MARKER).is_file() {
        return Err(format!(
            "This build has no flanner runtime at {}.\nFor development, set {RUNTIME_ENV} \
             to one built by desktop/bundle/build_runtime.py.",
            bundled.display()
        ));
    }
    // Copied beside the target and renamed into place, so an interrupted copy
    // is never mistaken for a runtime.
    let partial = versions.join(format!("{version}.partial"));
    let _ = fs::remove_dir_all(&partial);
    copy_tree(&bundled, &partial).map_err(text)?;
    let _ = fs::remove_dir_all(&target);
    fs::rename(&partial, &target).map_err(text)?;
    Ok(target)
}

fn python_in(runtime: &Path) -> PathBuf {
    if cfg!(windows) {
        runtime.join("python.exe")
    } else {
        runtime.join("bin").join("python3")
    }
}

/// Where flanner keeps its store. The same rule as flanner's own `get_mcp_dir`.
fn store(app: &AppHandle) -> Result<PathBuf, String> {
    let home = match std::env::var_os("FLANNER_HOME") {
        Some(home) => PathBuf::from(home),
        None => app.path().home_dir().map_err(text)?.join(".flanner"),
    };
    Ok(home.join("data.db"))
}

/// Run one flanner command to completion, its output in `log`.
fn flanner(python: &Path, args: &[&str], cwd: &Path, log: &Path) -> Result<(), String> {
    fs::create_dir_all(cwd).map_err(text)?;
    let out = fs::File::create(log).map_err(text)?;
    let mut command = Command::new(python);
    command
        .args(["-m", "flanner"])
        .args(args)
        .current_dir(cwd)
        .stdin(Stdio::null())
        .stdout(out.try_clone().map_err(text)?)
        .stderr(out);
    no_console(&mut command);
    let status = command.status().map_err(text)?;
    if status.success() {
        Ok(())
    } else {
        Err(format!(
            "`flanner {}` failed ({status}).\n\n{}",
            args.join(" "),
            tail(log)
        ))
    }
}

fn spawn_web(python: &Path, port: u16, cwd: &Path, log: &Path) -> io::Result<Child> {
    let out = fs::File::create(log)?;
    let mut command = Command::new(python);
    command
        .args(["-m", "flanner", "web", "--port", &port.to_string()])
        .current_dir(cwd)
        .stdin(Stdio::null())
        .stdout(out.try_clone()?)
        .stderr(out);
    no_console(&mut command);
    let child = command.spawn()?;
    tie_to_app(&child);
    Ok(child)
}

fn wait_until_serving(app: &AppHandle, port: u16, log: &Path) -> Result<(), String> {
    let address = SocketAddr::from(([127, 0, 0, 1], port));
    let deadline = Instant::now() + START_TIMEOUT;
    loop {
        if TcpStream::connect_timeout(&address, Duration::from_millis(250)).is_ok() {
            return Ok(());
        }
        let exited = app
            .state::<Sidecar>()
            .0
            .lock()
            .expect("sidecar lock")
            .as_mut()
            .and_then(|child| child.try_wait().ok().flatten());
        if let Some(status) = exited {
            return Err(format!("flanner web stopped ({status}).\n\n{}", tail(log)));
        }
        if Instant::now() > deadline {
            return Err(format!(
                "flanner web did not answer within 90 seconds.\n\n{}",
                tail(log)
            ));
        }
        thread::sleep(Duration::from_millis(200));
    }
}

fn free_port() -> io::Result<u16> {
    Ok(TcpListener::bind(("127.0.0.1", 0))?.local_addr()?.port())
}

/// The last part of a log, for an error somebody can act on.
fn tail(log: &Path) -> String {
    let mut text = String::new();
    if let Ok(mut file) = fs::File::open(log) {
        let size = file.metadata().map(|m| m.len()).unwrap_or(0);
        let _ = file.seek(SeekFrom::Start(size.saturating_sub(2000)));
        let _ = file.read_to_string(&mut text);
    }
    format!("{}\n\nFull log: {}", text.trim(), log.display())
}

fn copy_tree(from: &Path, to: &Path) -> io::Result<()> {
    fs::create_dir_all(to)?;
    for entry in fs::read_dir(from)? {
        let entry = entry?;
        let kind = entry.file_type()?;
        let dest = to.join(entry.file_name());
        if kind.is_dir() {
            copy_tree(&entry.path(), &dest)?;
        } else if kind.is_symlink() {
            copy_link(&entry.path(), &dest)?;
        } else {
            fs::copy(entry.path(), dest)?;
        }
    }
    Ok(())
}

#[cfg(unix)]
fn copy_link(from: &Path, to: &Path) -> io::Result<()> {
    // python-build-standalone links bin/python3 to python3.12; keep it a link.
    std::os::unix::fs::symlink(fs::read_link(from)?, to)
}

#[cfg(windows)]
fn copy_link(from: &Path, to: &Path) -> io::Result<()> {
    fs::copy(from, to).map(|_| ())
}

#[cfg(windows)]
fn no_console(command: &mut Command) {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    command.creation_flags(CREATE_NO_WINDOW);
}

#[cfg(not(windows))]
fn no_console(_command: &mut Command) {}

/// Windows ends every process in a kill-on-close job when its last handle
/// closes, so flanner web cannot outlive the app, however the app exits.
#[cfg(windows)]
fn tie_to_app(child: &Child) {
    use std::os::windows::io::AsRawHandle;
    let Ok(job) = win32job::Job::create() else {
        return;
    };
    let Ok(mut info) = job.query_extended_limit_info() else {
        return;
    };
    info.limit_kill_on_job_close();
    if job.set_extended_limit_info(&info).is_ok()
        && job.assign_process(child.as_raw_handle() as isize).is_ok()
    {
        // Never closed on purpose: the handle must live exactly as long as the app.
        std::mem::forget(job);
    }
}

// ponytail: elsewhere flanner web is stopped at exit only, so a crashed app
// leaves it running until logout. PR_SET_PDEATHSIG (Linux) and a pid check at
// the next start would close that; do it when M4 supervises peer serve too.
#[cfg(not(windows))]
fn tie_to_app(_child: &Child) {}

fn tray(app: &tauri::App) -> tauri::Result<()> {
    let open = MenuItem::with_id(app, "open", "Open Flanner", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit Flanner", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&open, &quit])?;
    let mut tray = TrayIconBuilder::with_id("main")
        .tooltip("Flanner")
        .menu(&menu);
    if let Some(icon) = app.default_window_icon() {
        tray = tray.icon(icon.clone());
    }
    tray.on_menu_event(|app, event| match event.id.as_ref() {
        "open" => show(app),
        "quit" => app.exit(0),
        _ => {}
    })
    .build(app)?;
    Ok(())
}

fn main_window(app: &AppHandle) -> Result<tauri::WebviewWindow, String> {
    app.get_webview_window("main")
        .ok_or_else(|| "the window is gone".to_string())
}

fn show(app: &AppHandle) {
    if let Ok(window) = main_window(app) {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

fn report(app: &AppHandle, message: &str) {
    show(app);
    if let Ok(window) = main_window(app) {
        let quoted = serde_json::to_string(message).unwrap_or_default();
        let _ = window.eval(format!("window.showError({quoted})"));
    }
}

fn stop(app: &AppHandle) {
    if let Some(mut child) = app
        .state::<Sidecar>()
        .0
        .lock()
        .expect("sidecar lock")
        .take()
    {
        let _ = child.kill();
        let _ = child.wait();
    }
}

fn text(error: impl std::fmt::Display) -> String {
    error.to_string()
}
