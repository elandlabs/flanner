//! Background sync: `flanner peer serve`, kept running while it is wanted.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Child;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use crate::flanner::Flanner;

/// Three quick stops in a row mean it cannot run here, most often because
/// this device has not joined a workspace. Retrying forever would only
/// spend the battery, so the supervisor gives up and says why.
const GIVE_UP_AFTER: u32 = 3;
const QUICK: Duration = Duration::from_secs(60);

#[derive(Default)]
pub struct Sync {
    wanted: AtomicBool,
    child: Mutex<Option<Child>>,
}

impl Sync {
    /// Start `peer serve` and restart it whenever it stops, until `stop`.
    /// `gave_up` receives the last line peer serve logged.
    pub fn start(
        self: &Arc<Self>,
        flanner: Flanner,
        cwd: PathBuf,
        log: PathBuf,
        gave_up: impl Fn(String) + Send + 'static,
    ) {
        if self.wanted.swap(true, Ordering::SeqCst) {
            return; // already running
        }
        let this = Arc::clone(self);
        thread::spawn(move || {
            let mut quick_stops = 0;
            while this.wanted.load(Ordering::SeqCst) {
                let started = Instant::now();
                match flanner.spawn(&["peer", "serve"], &cwd, &log) {
                    Ok(child) => {
                        this.child.lock().expect("sync lock").replace(child);
                    }
                    Err(error) => {
                        this.wanted.store(false, Ordering::SeqCst);
                        gave_up(error.to_string());
                        return;
                    }
                }
                this.wait_for_exit();
                if !this.wanted.load(Ordering::SeqCst) {
                    return;
                }
                quick_stops = if started.elapsed() < QUICK {
                    quick_stops + 1
                } else {
                    0
                };
                if quick_stops >= GIVE_UP_AFTER {
                    this.wanted.store(false, Ordering::SeqCst);
                    gave_up(last_line(&log));
                    return;
                }
                thread::sleep(Duration::from_secs(5 * u64::from(quick_stops)));
            }
        });
    }

    /// Polls, so `stop` can take the child from under it.
    fn wait_for_exit(&self) {
        loop {
            thread::sleep(Duration::from_millis(500));
            let mut guard = self.child.lock().expect("sync lock");
            match guard.as_mut() {
                None => return,
                Some(child) => {
                    if let Ok(Some(_)) = child.try_wait() {
                        guard.take();
                        return;
                    }
                }
            }
        }
    }

    pub fn stop(&self) {
        self.wanted.store(false, Ordering::SeqCst);
        if let Some(mut child) = self.child.lock().expect("sync lock").take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

fn last_line(log: &Path) -> String {
    fs::read_to_string(log)
        .ok()
        .and_then(|text| {
            text.lines()
                .rev()
                .find(|line| !line.trim().is_empty())
                .map(|line| line.trim().to_string())
        })
        .unwrap_or_else(|| format!("See {}", log.display()))
}
