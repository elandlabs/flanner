//! Background sync: `flanner peer serve`, kept running while it is wanted.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Child;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
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
    /// Bumped by every `start` and `stop`. A supervisor runs only while the
    /// value it started with is current, so after a quick off/on the old one
    /// stops instead of running beside the new one: two would each spawn
    /// `peer serve`, and the one whose child was replaced in `child` would
    /// leave a receiver that Stop and Quit could no longer kill.
    generation: AtomicU64,
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
        let mine = self.generation.fetch_add(1, Ordering::SeqCst) + 1;
        let this = Arc::clone(self);
        thread::spawn(move || {
            let mut quick_stops = 0;
            while this.current(mine) {
                let started = Instant::now();
                match flanner.spawn(&["peer", "serve"], &cwd, &log) {
                    Ok(mut child) => {
                        let mut slot = this.child.lock().expect("sync lock");
                        // Checked under the lock `stop` takes, so a stop or a
                        // newer start cannot slip between this check and the
                        // child being recorded where they can reach it.
                        if !this.current(mine) {
                            let _ = child.kill();
                            let _ = child.wait();
                            return;
                        }
                        if let Some(mut older) = slot.replace(child) {
                            let _ = older.kill();
                            let _ = older.wait();
                        }
                    }
                    Err(error) => {
                        if this.retire(mine) {
                            gave_up(error.to_string());
                        }
                        return;
                    }
                }
                this.wait_for_exit(mine);
                if !this.current(mine) {
                    return;
                }
                quick_stops = if started.elapsed() < QUICK {
                    quick_stops + 1
                } else {
                    0
                };
                if quick_stops >= GIVE_UP_AFTER {
                    if this.retire(mine) {
                        gave_up(last_line(&log));
                    }
                    return;
                }
                thread::sleep(Duration::from_secs(5 * u64::from(quick_stops)));
            }
        });
    }

    fn current(&self, generation: u64) -> bool {
        self.wanted.load(Ordering::SeqCst) && self.generation.load(Ordering::SeqCst) == generation
    }

    /// Gives up on sync for `generation`, and says whether it may report so.
    /// Only the current supervisor may: a superseded one that clears
    /// `wanted` would switch off the newer one and the user's preference.
    /// Held under the lock `stop` takes, and `start` cannot begin a new
    /// generation while `wanted` is still set, so nothing slips in between.
    fn retire(&self, generation: u64) -> bool {
        let _slot = self.child.lock().expect("sync lock");
        if !self.current(generation) {
            return false;
        }
        self.wanted.store(false, Ordering::SeqCst);
        true
    }

    /// Polls, so `stop` can take the child from under it. A superseded
    /// supervisor leaves at once, so it never waits on, or takes, the child
    /// a newer one recorded.
    fn wait_for_exit(&self, generation: u64) {
        loop {
            thread::sleep(Duration::from_millis(500));
            let mut guard = self.child.lock().expect("sync lock");
            if self.generation.load(Ordering::SeqCst) != generation {
                return;
            }
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
        let mut slot = self.child.lock().expect("sync lock");
        self.wanted.store(false, Ordering::SeqCst);
        self.generation.fetch_add(1, Ordering::SeqCst);
        if let Some(mut child) = slot.take() {
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_quick_stop_and_start_retires_the_older_supervisor() {
        let sync = Sync::default();
        sync.wanted.store(true, Ordering::SeqCst);
        let first = sync.generation.fetch_add(1, Ordering::SeqCst) + 1;
        assert!(sync.current(first));

        sync.stop();
        // `start` again, before the first supervisor has looked.
        sync.wanted.store(true, Ordering::SeqCst);
        let second = sync.generation.fetch_add(1, Ordering::SeqCst) + 1;

        assert!(
            !sync.current(first),
            "the old one must not carry on beside the new one"
        );
        assert!(sync.current(second));
    }

    #[test]
    fn only_the_current_supervisor_may_give_up() {
        let sync = Sync::default();
        sync.wanted.store(true, Ordering::SeqCst);
        let first = sync.generation.fetch_add(1, Ordering::SeqCst) + 1;
        sync.stop();
        sync.wanted.store(true, Ordering::SeqCst);
        let second = sync.generation.fetch_add(1, Ordering::SeqCst) + 1;

        assert!(
            !sync.retire(first),
            "a superseded supervisor must not report"
        );
        assert!(sync.current(second), "nor switch off the newer one");

        assert!(sync.retire(second));
        assert!(!sync.wanted.load(Ordering::SeqCst));
    }
}
