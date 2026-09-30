//! Running flanner: which one, from where, and making sure it stops.

use std::fs;
use std::io::{self, Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};

use tauri::{AppHandle, Manager};

/// flanner.release.DESKTOP_MARKER. Written last by the builder, so a runtime
/// that has it is complete.
const MARKER: &str = "flanner-desktop";

/// Points at a runtime built by desktop/bundle/build_runtime.py, used in
/// place. For development, where no runtime is bundled.
const RUNTIME_ENV: &str = "FLANNER_DESKTOP_RUNTIME";

/// Moves the app's own folder (settings, logs, runtime copies) elsewhere.
/// For tests, which must not read or leave settings in the real one.
const DATA_ENV: &str = "FLANNER_DESKTOP_DATA";

/// One way of running flanner: the app's bundled Python, or a flanner
/// somebody installed with pip and chose to keep.
#[derive(Clone, Debug)]
pub struct Flanner {
    program: PathBuf,
    prefix: Vec<String>,
}

impl Flanner {
    pub fn bundled(runtime: &Path) -> Self {
        let python = if cfg!(windows) {
            runtime.join("python.exe")
        } else {
            runtime.join("bin").join("python3")
        };
        Flanner {
            program: python,
            prefix: vec!["-m".into(), "flanner".into()],
        }
    }

    pub fn installed(program: PathBuf) -> Self {
        Flanner {
            program,
            prefix: Vec::new(),
        }
    }

    fn command(&self, args: &[&str], cwd: &Path) -> Command {
        let mut command = Command::new(&self.program);
        command
            .args(&self.prefix)
            .args(args)
            .current_dir(cwd)
            .stdin(Stdio::null());
        no_console(&mut command);
        command
    }

    /// Run one command to completion, its output in `log`.
    pub fn run(&self, args: &[&str], cwd: &Path, log: &Path) -> Result<(), String> {
        fs::create_dir_all(cwd).map_err(text)?;
        let out = fs::File::create(log).map_err(text)?;
        let status = self
            .command(args, cwd)
            .stdout(out.try_clone().map_err(text)?)
            .stderr(out)
            .status()
            .map_err(text)?;
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

    /// Run one command to completion and return what it printed.
    pub fn output(&self, args: &[&str], cwd: &Path) -> Result<String, String> {
        fs::create_dir_all(cwd).map_err(text)?;
        let done = self.command(args, cwd).output().map_err(text)?;
        if done.status.success() {
            Ok(String::from_utf8_lossy(&done.stdout).into_owned())
        } else {
            Err(format!(
                "`flanner {}` failed ({}).\n\n{}",
                args.join(" "),
                done.status,
                String::from_utf8_lossy(&done.stderr)
            ))
        }
    }

    /// Start a long-running command, tied to the app so it cannot outlive it.
    pub fn spawn(&self, args: &[&str], cwd: &Path, log: &Path) -> io::Result<Child> {
        fs::create_dir_all(cwd)?;
        let out = fs::OpenOptions::new().create(true).append(true).open(log)?;
        let child = self
            .command(args, cwd)
            .stdout(out.try_clone()?)
            .stderr(out)
            .spawn()?;
        tie_to_app(&child);
        Ok(child)
    }
}

/// This version's runtime, copied out of the install folder on its first
/// start. Old versions' copies are removed once nothing runs from them.
pub fn runtime(app: &AppHandle) -> Result<PathBuf, String> {
    if let Some(dev) = std::env::var_os(RUNTIME_ENV) {
        return Ok(PathBuf::from(dev));
    }
    let version = app.package_info().version.to_string();
    let versions = data_dir(app)?.join("runtime");
    let target = versions.join(&version);
    if !target.join(MARKER).is_file() {
        let bundled = app.path().resource_dir().map_err(text)?.join("runtime");
        if !bundled.join(MARKER).is_file() {
            return Err(format!(
                "This build has no flanner runtime at {}.\nFor development, set \
                 {RUNTIME_ENV} to one built by desktop/bundle/build_runtime.py.",
                bundled.display()
            ));
        }
        // Copied beside the target and renamed into place, so an interrupted
        // copy is never mistaken for a runtime.
        let partial = versions.join(format!("{version}.partial"));
        let _ = fs::remove_dir_all(&partial);
        copy_tree(&bundled, &partial).map_err(text)?;
        let _ = fs::remove_dir_all(&target);
        fs::rename(&partial, &target).map_err(text)?;
    }
    remove_old_runtimes(&versions, &version);
    Ok(target)
}

/// Delete other versions' runtimes that nothing is running from.
///
/// Renamed first: Windows refuses to rename a folder holding a file in use,
/// so a runtime that a `flanner-mcp` from before the update still runs
/// from is left whole, never half-deleted under it. The next start tries
/// again.
pub fn remove_old_runtimes(versions: &Path, current: &str) {
    let Ok(entries) = fs::read_dir(versions) else {
        return;
    };
    for entry in entries.flatten() {
        let name = entry.file_name().to_string_lossy().into_owned();
        if name == current || !entry.path().is_dir() {
            continue;
        }
        let trash = versions.join(format!(".trash-{name}"));
        let doomed = if name.starts_with(".trash-") {
            entry.path()
        } else if fs::rename(entry.path(), &trash).is_ok() {
            trash
        } else {
            continue;
        };
        let _ = fs::remove_dir_all(doomed);
    }
}

/// The app's own folder: settings, logs and the runtime copies.
pub fn data_dir(app: &AppHandle) -> Result<PathBuf, String> {
    match std::env::var_os(DATA_ENV) {
        Some(folder) => Ok(PathBuf::from(folder)),
        None => app.path().app_local_data_dir().map_err(text),
    }
}

/// Where flanner keeps its store. The same rule as flanner's own `get_mcp_dir`.
pub fn store(app: &AppHandle) -> Result<PathBuf, String> {
    let home = match std::env::var_os("FLANNER_HOME") {
        Some(home) => PathBuf::from(home),
        None => app.path().home_dir().map_err(text)?.join(".flanner"),
    };
    Ok(home.join("data.db"))
}

/// The last part of a log, for an error somebody can act on.
pub fn tail(log: &Path) -> String {
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
/// closes, so flanner cannot outlive the app, however the app exits.
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

// ponytail: elsewhere a child is stopped at exit only, so a crashed app
// leaves it running until logout. PR_SET_PDEATHSIG (Linux) would close that
// there; macOS has no equivalent, so a pid check at the next start is the
// general fix if strays are ever reported.
#[cfg(not(windows))]
fn tie_to_app(_child: &Child) {}

pub fn text(error: impl std::fmt::Display) -> String {
    error.to_string()
}

#[cfg(test)]
mod tests {
    use super::remove_old_runtimes;
    use std::fs;

    #[test]
    fn old_runtimes_go_and_the_current_one_stays() {
        let root = std::env::temp_dir().join(format!("flanner-runtimes-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        for name in ["0.14.0", "0.15.0", ".trash-0.13.0"] {
            fs::create_dir_all(root.join(name).join("Lib")).unwrap();
            fs::write(root.join(name).join("Lib").join("x.py"), "x").unwrap();
        }

        remove_old_runtimes(&root, "0.15.0");

        let left: Vec<_> = fs::read_dir(&root)
            .unwrap()
            .map(|e| e.unwrap().file_name().into_string().unwrap())
            .collect();
        assert_eq!(left, vec!["0.15.0".to_string()]);
        let _ = fs::remove_dir_all(&root);
    }

    #[cfg(windows)]
    #[test]
    fn a_runtime_something_still_runs_from_is_left_whole() {
        let root = std::env::temp_dir().join(format!("flanner-busy-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(root.join("0.14.0")).unwrap();
        fs::create_dir_all(root.join("0.15.0")).unwrap();
        let held = root.join("0.14.0").join("python312.dll");
        fs::write(&held, "x").unwrap();
        // Open without FILE_SHARE_DELETE, as a loaded DLL is.
        let open = {
            use std::os::windows::fs::OpenOptionsExt;
            fs::OpenOptions::new()
                .read(true)
                .share_mode(0x1)
                .open(&held)
                .unwrap()
        };

        remove_old_runtimes(&root, "0.15.0");

        assert!(held.is_file(), "a runtime in use was deleted from under it");
        drop(open);
        remove_old_runtimes(&root, "0.15.0");
        assert!(!root.join("0.14.0").exists());
        let _ = fs::remove_dir_all(&root);
    }
}
