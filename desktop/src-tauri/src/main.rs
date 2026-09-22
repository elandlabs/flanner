//! The flanner desktop app: a native window over flanner's own web UI.
//!
//! Everything flanner does stays in Python. This shell only:
//! - copies the bundled Python runtime out of the install folder, once per
//!   version, so an update never replaces files a running agent still uses;
//! - asks, on a first start, which flanner to use and whether to connect
//!   it to Claude and Codex, then links the launchers and connects;
//! - creates the store when there is none (`flanner init`, outside any repo);
//! - starts `flanner web` on a free port and shows it in the window;
//! - keeps a tray icon, and stops flanner when the app quits.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod flanner;

use std::fs;
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::Child;
use std::sync::{mpsc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder, WindowEvent};

use flanner::{text, Flanner};

/// A cold first start imports FastAPI and SQLAlchemy from a fresh copy.
const START_TIMEOUT: Duration = Duration::from_secs(90);

/// Which flanner the person chose on the setup screen.
#[derive(Clone, Debug)]
enum Choice {
    /// The app's own, updated with the app.
    Bundled,
    /// One installed with pip that they chose to keep using.
    Installed(PathBuf),
}

/// The setup screen, while it waits for an answer.
struct Pending {
    probe: String,
    other: Option<PathBuf>,
    reply: mpsc::Sender<(Choice, bool)>,
}

#[derive(Default)]
struct State {
    /// The `flanner web` process, so quitting can stop it.
    web: Mutex<Option<Child>>,
    setup: Mutex<Option<Pending>>,
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            show(app)
        }))
        .plugin(tauri_plugin_dialog::init())
        .manage(State::default())
        .invoke_handler(tauri::generate_handler![pending_setup, finish_setup])
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
    let bundled = Flanner::bundled(&flanner::runtime(app)?);
    let data = flanner::data_dir(app)?;
    let logs = data.join("logs");
    fs::create_dir_all(&logs).map_err(text)?;

    let mut settings = load_settings(&data);
    let first_start = settings.get("flanner").is_none();
    let (choice, connect) = if first_start {
        ask(app, &bundled, &data)?
    } else {
        (saved_choice(&settings), false)
    };

    let chosen = match &choice {
        Choice::Bundled => {
            bundled.run(&["desktop-link"], &data, &logs.join("link.log"))?;
            if connect {
                status(app, "Connecting flanner to Claude and Codex");
                bundled.run(&["desktop-connect"], &data, &logs.join("connect.log"))?;
            }
            bundled
        }
        Choice::Installed(path) => Flanner::installed(path.clone()),
    };
    if !flanner::store(app)?.is_file() {
        // Run from the app's own folder, which is never a repository, so a
        // first start creates the store and adopts nothing.
        let init = ["init", "--setup", "none", "--no-watch-skills"];
        chosen.run(&init, &data, &logs.join("init.log"))?;
    }
    if first_start {
        settings["flanner"] = match &choice {
            Choice::Bundled => json!("bundled"),
            Choice::Installed(path) => json!(path),
        };
        settings["connected"] = json!(connect);
        save_settings(&data, &settings)?;
    }

    status(app, "Starting flanner");
    let port = free_port().map_err(text)?;
    let log = logs.join("web.log");
    let _ = fs::remove_file(&log);
    let child = chosen
        .spawn(&["web", "--port", &port.to_string()], &data, &log)
        .map_err(text)?;
    app.state::<State>()
        .web
        .lock()
        .expect("web lock")
        .replace(child);
    wait_until_serving(app, port, &log)?;

    let url = format!("http://127.0.0.1:{port}/").parse().map_err(text)?;
    main_window(app)?.navigate(url).map_err(text)
}

/// Show the setup screen and wait for the answer.
fn ask(app: &AppHandle, bundled: &Flanner, data: &Path) -> Result<(Choice, bool), String> {
    let probe = bundled.output(&["desktop-probe"], data)?.trim().to_string();
    let parsed: Value = serde_json::from_str(&probe).map_err(text)?;
    let other = parsed["other_flanner"]["path"].as_str().map(PathBuf::from);
    let (reply, answer) = mpsc::channel();
    app.state::<State>()
        .setup
        .lock()
        .expect("setup lock")
        .replace(Pending {
            probe: probe.clone(),
            other,
            reply,
        });
    // The page also asks for it when it loads, in case it loads after this.
    let _ = main_window(app)?.eval(format!("window.showSetup && window.showSetup({probe})"));
    show(app);
    answer
        .recv()
        .map_err(|_| "The setup screen closed without an answer.".to_string())
}

/// The setup screen's question, if one is waiting. Called by the local page.
#[tauri::command]
fn pending_setup(state: tauri::State<'_, State>) -> Option<String> {
    let pending = state.setup.lock().expect("setup lock");
    pending.as_ref().map(|waiting| waiting.probe.clone())
}

/// The setup screen's answer. The path of a kept flanner comes from the
/// probe, never from the page.
#[tauri::command]
fn finish_setup(
    state: tauri::State<'_, State>,
    connect: bool,
    keep_installed: bool,
) -> Result<(), String> {
    let pending = state
        .setup
        .lock()
        .expect("setup lock")
        .take()
        .ok_or("Setup is not waiting for an answer.")?;
    let choice = match (keep_installed, pending.other) {
        (true, Some(path)) => Choice::Installed(path),
        _ => Choice::Bundled,
    };
    let connect = connect && matches!(choice, Choice::Bundled);
    pending.reply.send((choice, connect)).map_err(text)
}

fn saved_choice(settings: &Value) -> Choice {
    match settings["flanner"].as_str() {
        Some(path) if path != "bundled" && Path::new(path).is_file() => {
            Choice::Installed(PathBuf::from(path))
        }
        // Also when a kept pip flanner has since been uninstalled.
        _ => Choice::Bundled,
    }
}

fn load_settings(data: &Path) -> Value {
    fs::read_to_string(data.join("settings.json"))
        .ok()
        .and_then(|saved| serde_json::from_str::<Value>(&saved).ok())
        .filter(Value::is_object)
        .unwrap_or_else(|| json!({}))
}

fn save_settings(data: &Path, settings: &Value) -> Result<(), String> {
    fs::create_dir_all(data).map_err(text)?;
    let pretty = serde_json::to_string_pretty(settings).map_err(text)?;
    fs::write(data.join("settings.json"), pretty).map_err(text)
}

fn wait_until_serving(app: &AppHandle, port: u16, log: &Path) -> Result<(), String> {
    let address = SocketAddr::from(([127, 0, 0, 1], port));
    let deadline = Instant::now() + START_TIMEOUT;
    loop {
        if TcpStream::connect_timeout(&address, Duration::from_millis(250)).is_ok() {
            return Ok(());
        }
        let exited = app
            .state::<State>()
            .web
            .lock()
            .expect("web lock")
            .as_mut()
            .and_then(|child| child.try_wait().ok().flatten());
        if let Some(code) = exited {
            return Err(format!(
                "flanner web stopped ({code}).\n\n{}",
                flanner::tail(log)
            ));
        }
        if Instant::now() > deadline {
            return Err(format!(
                "flanner web did not answer within 90 seconds.\n\n{}",
                flanner::tail(log)
            ));
        }
        thread::sleep(Duration::from_millis(200));
    }
}

fn free_port() -> std::io::Result<u16> {
    Ok(TcpListener::bind(("127.0.0.1", 0))?.local_addr()?.port())
}

fn tray(app: &tauri::App) -> tauri::Result<()> {
    let open = MenuItem::with_id(app, "open", "Open Flanner", true, None::<&str>)?;
    let again = MenuItem::with_id(app, "setup", "Set up again…", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit Flanner", true, None::<&str>)?;
    let line = PredefinedMenuItem::separator(app)?;
    let menu = Menu::with_items(app, &[&open, &again, &line, &quit])?;
    let mut tray = TrayIconBuilder::with_id("main")
        .tooltip("Flanner")
        .menu(&menu);
    if let Some(icon) = app.default_window_icon() {
        tray = tray.icon(icon.clone());
    }
    tray.on_menu_event(|app, event| match event.id.as_ref() {
        "open" => show(app),
        "setup" => set_up_again(app),
        "quit" => app.exit(0),
        _ => {}
    })
    .build(app)?;
    Ok(())
}

/// Forget the setup answer and restart, so the setup screen shows again.
fn set_up_again(app: &AppHandle) {
    if let Ok(data) = flanner::data_dir(app) {
        let mut settings = load_settings(&data);
        if let Some(saved) = settings.as_object_mut() {
            saved.remove("flanner");
        }
        let _ = save_settings(&data, &settings);
    }
    stop(app);
    app.restart();
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

fn status(app: &AppHandle, message: &str) {
    if let Ok(window) = main_window(app) {
        let quoted = serde_json::to_string(message).unwrap_or_default();
        let _ = window.eval(format!("window.showStatus && window.showStatus({quoted})"));
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
    if let Some(mut child) = app.state::<State>().web.lock().expect("web lock").take() {
        let _ = child.kill();
        let _ = child.wait();
    }
}
