//! The flanner desktop app: a native window over flanner's own web UI.
//!
//! Everything flanner does stays in Python. This shell only:
//! - copies the bundled Python runtime out of the install folder, once per
//!   version, so an update never replaces files a running agent still uses;
//! - asks, on a first start, which flanner to use and whether to connect
//!   it to Claude and Codex, then links the launchers and connects;
//! - creates the store when there is none (`flanner init`, outside any repo);
//! - starts `flanner web` on a free port and shows it in the window;
//! - keeps a tray icon: background sync, start at login, updates;
//! - says when something new is waiting, and stops flanner when it quits.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod flanner;
mod sync;
mod updates;
mod waiting;

use std::fs;
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::Child;
use std::sync::{mpsc, Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder, WindowEvent, Wry};
use tauri_plugin_autostart::{MacosLauncher, ManagerExt};
use tauri_plugin_notification::NotificationExt;

use flanner::{text, Flanner};

/// A cold first start imports FastAPI and SQLAlchemy from a fresh copy.
const START_TIMEOUT: Duration = Duration::from_secs(90);

/// Passed by the login item, so a start at login stays in the tray.
const AT_LOGIN: &str = "--at-login";

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

struct Tray {
    sync: CheckMenuItem<Wry>,
    update: MenuItem<Wry>,
}

#[derive(Default)]
pub struct State {
    /// The `flanner web` process, so quitting can stop it.
    web: Mutex<Option<Child>>,
    setup: Mutex<Option<Pending>>,
    /// The flanner in use, once boot has chosen it.
    flanner: Mutex<Option<Flanner>>,
    sync: Arc<sync::Sync>,
    pub update: Mutex<Option<tauri_plugin_updater::Update>>,
    tray: Mutex<Option<Tray>>,
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            show(app)
        }))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_autostart::init(
            MacosLauncher::LaunchAgent,
            Some(vec![AT_LOGIN]),
        ))
        .manage(State::default())
        .invoke_handler(tauri::generate_handler![pending_setup, finish_setup])
        .setup(|app| {
            let at_login = std::env::args().any(|arg| arg == AT_LOGIN);
            WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title("Flanner")
                .inner_size(1280.0, 860.0)
                .min_inner_size(720.0, 520.0)
                .visible(!at_login)
                .build()?;
            tray(app)?;
            let handle = app.handle().clone();
            thread::spawn(move || {
                if let Err(message) = boot(&handle) {
                    report(&handle, &message);
                }
            });
            updates::watch(app.handle().clone());
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
                stop_everything(app);
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
    }
    let version = app.package_info().version.to_string();
    let updated_from = settings["version"].as_str().map(str::to_string);
    settings["version"] = json!(version);
    save_settings(&data, &settings)?;
    app.state::<State>()
        .flanner
        .lock()
        .expect("flanner lock")
        .replace(chosen.clone());

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
    main_window(app)?.navigate(url).map_err(text)?;

    if settings["sync"].as_bool() == Some(true) {
        start_sync(app);
    }
    waiting::watch(app.clone(), port);
    if updated_from.is_some_and(|before| before != version) {
        notify(
            app,
            &format!("Flanner updated to {version}"),
            "Restart Claude and Codex so they use the new version too.",
        );
    }
    Ok(())
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

fn remember(app: &AppHandle, key: &str, value: Value) {
    if let Ok(data) = flanner::data_dir(app) {
        let mut settings = load_settings(&data);
        settings[key] = value;
        let _ = save_settings(&data, &settings);
    }
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

// --- background sync -----------------------------------------------------------

fn start_sync(app: &AppHandle) {
    let state = app.state::<State>();
    let Some(chosen) = state.flanner.lock().expect("flanner lock").clone() else {
        return; // boot starts it once flanner is chosen
    };
    let Ok(data) = flanner::data_dir(app) else {
        return;
    };
    let handle = app.clone();
    state.sync.start(
        chosen,
        data.clone(),
        data.join("logs").join("sync.log"),
        move |reason| {
            set_sync_ticked(&handle, false);
            remember(&handle, "sync", json!(false));
            notify(&handle, "Background sync stopped", &reason);
        },
    );
}

fn set_sync_ticked(app: &AppHandle, ticked: bool) {
    if let Some(tray) = app
        .state::<State>()
        .tray
        .lock()
        .expect("tray lock")
        .as_ref()
    {
        let _ = tray.sync.set_checked(ticked);
    }
}

// --- the tray ----------------------------------------------------------------

fn tray(app: &tauri::App) -> tauri::Result<()> {
    let data = flanner::data_dir(app.handle()).unwrap_or_default();
    let syncing = load_settings(&data)["sync"].as_bool() == Some(true);
    let at_login = app.autolaunch().is_enabled().unwrap_or(false);

    let open = MenuItem::with_id(app, "open", "Open Flanner", true, None::<&str>)?;
    let sync = CheckMenuItem::with_id(
        app,
        "sync",
        "Keep syncing in the background",
        true,
        syncing,
        None::<&str>,
    )?;
    let login =
        CheckMenuItem::with_id(app, "login", "Start at login", true, at_login, None::<&str>)?;
    let update = MenuItem::with_id(app, "update", "Flanner is up to date", false, None::<&str>)?;
    let again = MenuItem::with_id(app, "setup", "Set up again…", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit Flanner", true, None::<&str>)?;
    let menu = Menu::with_items(
        app,
        &[
            &open,
            &PredefinedMenuItem::separator(app)?,
            &sync,
            &login,
            &update,
            &again,
            &PredefinedMenuItem::separator(app)?,
            &quit,
        ],
    )?;
    let mut tray = TrayIconBuilder::with_id("main")
        .tooltip("Flanner")
        .menu(&menu);
    if let Some(icon) = app.default_window_icon() {
        tray = tray.icon(icon.clone());
    }
    tray.on_menu_event(|app, event| match event.id.as_ref() {
        "open" => show(app),
        "sync" => toggle_sync(app),
        "login" => toggle_login(app),
        "update" => {
            let handle = app.clone();
            tauri::async_runtime::spawn(async move { updates::install(&handle).await });
        }
        "setup" => set_up_again(app),
        "quit" => app.exit(0),
        _ => {}
    })
    .build(app)?;
    app.state::<State>()
        .tray
        .lock()
        .expect("tray lock")
        .replace(Tray { sync, update });
    Ok(())
}

fn toggle_sync(app: &AppHandle) {
    let state = app.state::<State>();
    let ticked = state
        .tray
        .lock()
        .expect("tray lock")
        .as_ref()
        .and_then(|tray| tray.sync.is_checked().ok())
        .unwrap_or(false);
    remember(app, "sync", json!(ticked));
    if ticked {
        start_sync(app);
    } else {
        state.sync.stop();
    }
}

fn toggle_login(app: &AppHandle) {
    let launcher = app.autolaunch();
    let result = if launcher.is_enabled().unwrap_or(false) {
        launcher.disable()
    } else {
        launcher.enable()
    };
    if let Err(error) = result {
        notify(app, "Start at login did not change", &error.to_string());
    }
}

/// Called by `updates` when an update is waiting.
pub fn offer_update(app: &AppHandle, version: &str) {
    if let Some(tray) = app
        .state::<State>()
        .tray
        .lock()
        .expect("tray lock")
        .as_ref()
    {
        let _ = tray
            .update
            .set_text(format!("Restart to update to {version}"));
        let _ = tray.update.set_enabled(true);
    }
}

/// Called by `waiting` with how many things wait on the person.
pub fn set_waiting(app: &AppHandle, total: u64) {
    if let Some(tray) = app.tray_by_id("main") {
        let tip = match total {
            0 => "Flanner".to_string(),
            1 => "Flanner: 1 thing needs you".to_string(),
            n => format!("Flanner: {n} things need you"),
        };
        let _ = tray.set_tooltip(Some(tip));
    }
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
    stop_everything(app);
    app.restart();
}

// --- the window ----------------------------------------------------------------

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

pub fn notify(app: &AppHandle, title: &str, body: &str) {
    let _ = app.notification().builder().title(title).body(body).show();
}

/// Stop every process the app started. Every way out comes through here:
/// Quit, "Set up again…", and the updater's own exit on Windows.
pub fn stop_everything(app: &AppHandle) {
    let state = app.state::<State>();
    state.sync.stop();
    let web = state.web.lock().expect("web lock").take();
    if let Some(mut child) = web {
        let _ = child.kill();
        let _ = child.wait();
    }
}
