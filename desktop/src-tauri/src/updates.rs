//! Updates: checked on start and once a day, installed when the person
//! chooses "Restart to update" in the tray.

use std::fs;
use std::io::Write;
use std::sync::Mutex;
use std::thread;
use std::time::Duration;

use tauri::{AppHandle, Manager};
use tauri_plugin_updater::{Update, UpdaterExt};

const EVERY: Duration = Duration::from_secs(24 * 60 * 60);

/// Test builds only: install as soon as an update is found, so the update
/// tests need no tray click. Release builds ignore it.
const INSTALL_NOW_ENV: &str = "FLANNER_DESKTOP_UPDATE_NOW";

pub fn watch(app: AppHandle) {
    thread::spawn(move || loop {
        tauri::async_runtime::block_on(check(&app));
        thread::sleep(EVERY);
    });
}

async fn check(app: &AppHandle) {
    let handle = app.clone();
    // On Windows the updater starts the installer and exits the process at
    // once, so the app's own exit never runs: stop flanner here instead.
    let built = app
        .updater_builder()
        .on_before_exit(move || crate::stop_everything(&handle))
        .build();
    let updater = match built {
        Ok(updater) => updater,
        Err(error) => return note(app, &format!("not checking: {error}")),
    };
    match updater.check().await {
        Ok(Some(update)) => {
            note(app, &format!("found {}", update.version));
            if offer(app, update) {
                install(app).await;
            }
        }
        Ok(None) => note(app, "up to date"),
        Err(error) => note(app, &format!("check failed: {error}")),
    }
}

/// Put the update in the tray. True when a test build should install it now.
fn offer(app: &AppHandle, update: Update) -> bool {
    let version = update.version.clone();
    app.state::<crate::State>()
        .update
        .lock()
        .expect("update lock")
        .replace(update);
    crate::offer_update(app, &version);
    if cfg!(debug_assertions) && std::env::var_os(INSTALL_NOW_ENV).is_some() {
        return true;
    }
    crate::notify(
        app,
        &format!("Flanner {version} is ready"),
        "Choose \u{201c}Restart to update\u{201d} in the flanner tray menu. \
         Then restart Claude and Codex so they use it too.",
    );
    false
}

/// Download, check the signature against the public key built into the
/// app, and install. A download that does not verify is never run.
pub async fn install(app: &AppHandle) {
    let Some(update) = app
        .state::<crate::State>()
        .update
        .lock()
        .expect("update lock")
        .take()
    else {
        return;
    };
    note(app, &format!("installing {}", update.version));
    match update.download_and_install(|_, _| {}, || {}).await {
        // Windows has already exited to run the installer by now.
        Ok(()) => {
            crate::stop_everything(app);
            app.restart();
        }
        Err(error) => {
            note(app, &format!("refused: {error}"));
            // The tray still offers it, so keep it there to try again.
            put_back(&app.state::<crate::State>().update, update);
            crate::notify(
                app,
                "The update was not installed",
                &format!("{error}. Flanner keeps running the version you have."),
            );
        }
    }
}

/// Return an update that did not install, so "Restart to update" works
/// again, unless a newer check has offered another one meanwhile.
fn put_back<T>(slot: &Mutex<Option<T>>, update: T) {
    slot.lock().expect("update lock").get_or_insert(update);
}

/// One line per event in logs/updates.log: what the tests and a bug report read.
fn note(app: &AppHandle, line: &str) {
    let Ok(data) = crate::flanner::data_dir(app) else {
        return;
    };
    let logs = data.join("logs");
    let _ = fs::create_dir_all(&logs);
    if let Ok(mut file) = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(logs.join("updates.log"))
    {
        let _ = writeln!(file, "{line}");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_failed_update_is_offered_again() {
        let slot = Mutex::new(None);
        put_back(&slot, "1.2.0");
        assert_eq!(*slot.lock().unwrap(), Some("1.2.0"));
    }

    #[test]
    fn a_newer_offer_is_not_replaced_by_the_failed_one() {
        let slot = Mutex::new(Some("1.3.0"));
        put_back(&slot, "1.2.0");
        assert_eq!(*slot.lock().unwrap(), Some("1.3.0"));
    }
}
