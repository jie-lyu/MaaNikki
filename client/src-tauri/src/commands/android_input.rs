//! 安卓模拟器输入的恢复边界（docs/android-support-plan.md 的 B5）。
//!
//! agent 每次向模拟器触摸之前，独占创建 `config/android-input/touch.json`，确认抬起后才删除；标记还在说明
//! 有一次触摸没有确认，agent 拒绝再发任何输入（agent/host/__init__.py 的 DeviceInputState）。MaaNikki 启动时
//! 清除这个标记，这是恢复的边界。清除前先以非阻塞方式拿到 agent 用的同一把排他锁 `owner.lock`：拿不到说明
//! 还有 agent 进程在操作模拟器，这时不清除。锁文件本身从不删除。
//!
//! 安卓目标目前只在 macOS 上开放；其他系统上这里什么都不做（Windows 行为不变）。

use std::path::Path;

/// 启动时清除的结果。
#[derive(Debug, PartialEq, Eq)]
pub enum ClearOutcome {
    /// 没有留下的标记。
    NoMarker,
    /// 清除了上一次没有确认的触摸标记。
    Cleared,
    /// 还有 agent 持有锁，没有清除。
    Busy,
    /// 无法检查或清除（原因）。
    Failed(String),
    /// 当前系统不开放安卓目标。
    Unsupported,
}

pub fn input_dir(data_dir: &Path) -> std::path::PathBuf {
    data_dir.join("config").join("android-input")
}

/// MaaNikki 启动时调用：在 owner.lock 的排他锁下删除 touch.json。
pub fn clear_touch_marker(data_dir: &Path) -> ClearOutcome {
    #[cfg(target_os = "macos")]
    {
        clear_under_lock(&input_dir(data_dir))
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = data_dir;
        ClearOutcome::Unsupported
    }
}

#[cfg(unix)]
fn clear_under_lock(dir: &Path) -> ClearOutcome {
    use std::os::unix::io::AsRawFd;
    let marker = dir.join("touch.json");
    match dir.try_exists() {
        Ok(true) => {}
        Ok(false) => return ClearOutcome::NoMarker,
        Err(e) => return ClearOutcome::Failed(format!("无法检查 {}：{}", dir.display(), e)),
    }
    let lock = match std::fs::OpenOptions::new()
        .read(true)
        .append(true)
        .create(true)
        .open(dir.join("owner.lock"))
    {
        Ok(file) => file,
        Err(e) => return ClearOutcome::Failed(format!("无法打开 owner.lock：{}", e)),
    };
    // SAFETY: flock on a file descriptor owned by `lock` for the duration of the call.
    let rc = unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
    if rc != 0 {
        let error = std::io::Error::last_os_error();
        return if error.raw_os_error() == Some(libc::EWOULDBLOCK) {
            ClearOutcome::Busy
        } else {
            ClearOutcome::Failed(format!("无法锁定 owner.lock：{}", error))
        };
    }
    // The lock is released when `lock` is dropped (and with this process).
    match std::fs::remove_file(&marker) {
        Ok(()) => ClearOutcome::Cleared,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => ClearOutcome::NoMarker,
        Err(e) => ClearOutcome::Failed(format!("无法删除 touch.json：{}", e)),
    }
}

/// `owner.lock` 此刻能否以非阻塞方式拿到（拿到后立即放开）：安卓运行在开始之前和收尾时用它确认没有 agent
/// 进程还在操作模拟器（agent 拿到这把锁后一直持有到进程退出）。目录不存在时没有人持有，什么都不创建。
#[cfg(target_os = "macos")]
pub fn owner_lock_free(dir: &Path) -> Result<bool, String> {
    use std::os::unix::io::AsRawFd;
    match dir.try_exists() {
        Ok(true) => {}
        Ok(false) => return Ok(true),
        Err(e) => return Err(format!("无法检查 {}：{}", dir.display(), e)),
    }
    let lock = std::fs::OpenOptions::new()
        .read(true)
        .append(true)
        .create(true)
        .open(dir.join("owner.lock"))
        .map_err(|e| format!("无法打开 owner.lock：{}", e))?;
    // SAFETY: flock on a file descriptor owned by `lock` for the duration of the call.
    let rc = unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
    if rc == 0 {
        // Released when `lock` is dropped at the end of this function.
        return Ok(true);
    }
    let error = std::io::Error::last_os_error();
    if error.raw_os_error() == Some(libc::EWOULDBLOCK) {
        Ok(false)
    } else {
        Err(format!("无法锁定 owner.lock：{}", error))
    }
}

/// 停止的握手（与 agent 的 DeviceInputState.stop_requested、scripts/android/runner.py 的 request_stop 相同）：停止的
/// 一方先原子地写下 `stop.request`，再等 `touch.json` 消失（已经预留的那次触摸做完），然后才让 Tasker 停下。agent 在
/// 预留触摸之后、发送之前看到这个请求就不发送。开始的一方在启动 agent 之前删掉留下的请求。
#[cfg(target_os = "macos")]
pub fn write_stop_request(dir: &Path) -> Result<(), String> {
    std::fs::create_dir_all(dir).map_err(|e| format!("无法创建 {}：{}", dir.display(), e))?;
    let staging = dir.join(format!("stop.request.{}.tmp", std::process::id()));
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or_default();
    let payload = format!("{{\"pid\":{},\"time_ns\":{}}}", std::process::id(), nanos);
    std::fs::write(&staging, payload).map_err(|e| format!("无法写入停止请求：{}", e))?;
    std::fs::rename(&staging, dir.join("stop.request")).map_err(|e| {
        let _ = std::fs::remove_file(&staging);
        format!("无法写入停止请求：{}", e)
    })
}

/// 删掉上一轮留下的停止请求（调用方保证此刻没有 agent 在运行）。
#[cfg(target_os = "macos")]
pub fn clear_stop_request(dir: &Path) -> Result<(), String> {
    match std::fs::remove_file(dir.join("stop.request")) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(format!("无法清除上一轮的停止请求：{}", e)),
    }
}

/// 有没有已经预留、还没确认的触摸（`touch.json`）。读不出时当作有。
#[cfg(target_os = "macos")]
pub fn touch_reserved(dir: &Path) -> bool {
    !matches!(std::fs::symlink_metadata(dir.join("touch.json")), Err(e) if e.kind() == std::io::ErrorKind::NotFound)
}

/// Where the host-wide device locks live: a fixed directory, never TMPDIR or the data root, so the client and the
/// developer runner lock the same file for one serial however each was launched (scripts/android/runner.py's
/// DEVICE_LOCK_DIR is the same path).
#[cfg(target_os = "macos")]
pub const DEVICE_LOCK_DIR: &str = "/tmp";

/// The host-wide lock file for one emulator: `<dir>/maanikki-android-<serial>.lock`, the serial with every character
/// outside `[A-Za-z0-9._-]` replaced by `_` (scripts/android/runner.py's device_lock_path builds the same name; `dir` is
/// DEVICE_LOCK_DIR in production, a test directory in the unit tests).
#[cfg(target_os = "macos")]
pub fn device_lock_path(dir: &Path, serial: &str) -> std::path::PathBuf {
    let name: String = serial
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-') { c } else { '_' })
        .collect();
    dir.join(format!("maanikki-android-{}.lock", name))
}

/// Takes the device lock without blocking: Ok(Some(file)) holds it until the file is dropped; Ok(None) when another
/// process (or another open handle) holds it. The lock file is never removed.
#[cfg(target_os = "macos")]
pub fn take_device_lock(dir: &Path, serial: &str) -> Result<Option<std::fs::File>, String> {
    use std::os::unix::io::AsRawFd;
    let path = device_lock_path(dir, serial);
    let file = std::fs::OpenOptions::new()
        .read(true)
        .append(true)
        .create(true)
        .open(&path)
        .map_err(|e| format!("无法打开 {}：{}", path.display(), e))?;
    // SAFETY: flock on a file descriptor owned by `file` for the duration of the call.
    let rc = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
    if rc == 0 {
        return Ok(Some(file));
    }
    let error = std::io::Error::last_os_error();
    if error.raw_os_error() == Some(libc::EWOULDBLOCK) {
        Ok(None)
    } else {
        Err(format!("无法锁定 {}：{}", path.display(), error))
    }
}

/// 启动时调用并记录结果。
pub fn clear_at_startup(data_dir: &Path) {
    match clear_touch_marker(data_dir) {
        ClearOutcome::Cleared => log::warn!("已清除上一次没有确认抬起的安卓模拟器触摸标记"),
        ClearOutcome::Busy => log::warn!("还有 agent 在操作安卓模拟器，没有清除触摸标记"),
        ClearOutcome::Failed(reason) => log::warn!("没能清除安卓模拟器触摸标记：{}", reason),
        ClearOutcome::NoMarker | ClearOutcome::Unsupported => {}
    }
}

/// Unit tests that take flocks or spawn processes run one at a time (a process spawned on another thread holds
/// a copy of every open file description until it execs, which can keep a just-released flock held).
#[cfg(all(test, unix))]
pub(crate) fn serial_fd_tests() -> std::sync::MutexGuard<'static, ()> {
    static SERIAL: std::sync::Mutex<()> = std::sync::Mutex::new(());
    SERIAL.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::os::unix::io::AsRawFd;

    fn temp_dir(name: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!("maanikki-android-input-{}-{}", name, std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn a_left_marker_is_cleared_and_the_lock_file_kept() {
        let _serial = serial_fd_tests();
        let dir = temp_dir("clear");
        std::fs::write(dir.join("touch.json"), "{}").unwrap();
        assert_eq!(clear_under_lock(&dir), ClearOutcome::Cleared);
        assert!(!dir.join("touch.json").exists());
        assert!(dir.join("owner.lock").exists());
        assert_eq!(clear_under_lock(&dir), ClearOutcome::NoMarker);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_marker_is_kept_while_an_agent_holds_the_lock() {
        let _serial = serial_fd_tests();
        let dir = temp_dir("busy");
        std::fs::write(dir.join("touch.json"), "{}").unwrap();
        let holder = std::fs::OpenOptions::new().read(true).append(true).create(true)
            .open(dir.join("owner.lock")).unwrap();
        // flock locks belong to the open file description, so this conflicts even within one process.
        assert_eq!(unsafe { libc::flock(holder.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) }, 0);
        assert_eq!(clear_under_lock(&dir), ClearOutcome::Busy);
        assert!(dir.join("touch.json").exists());
        drop(holder);
        assert_eq!(clear_under_lock(&dir), ClearOutcome::Cleared);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn an_unreadable_directory_is_reported_not_taken_as_empty() {
        use std::os::unix::fs::PermissionsExt;
        let root = temp_dir("denied");
        let dir = root.join("config").join("android-input");
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("touch.json"), "{}").unwrap();
        std::fs::set_permissions(root.join("config"), std::fs::Permissions::from_mode(0o000)).unwrap();
        let outcome = clear_under_lock(&dir);
        std::fs::set_permissions(root.join("config"), std::fs::Permissions::from_mode(0o755)).unwrap();
        assert!(matches!(outcome, ClearOutcome::Failed(_)), "{:?}", outcome);
        assert!(dir.join("touch.json").exists());
        let _ = std::fs::remove_dir_all(&root);
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn the_owner_lock_is_free_only_while_no_one_holds_it() {
        let _serial = serial_fd_tests();
        let dir = temp_dir("free");
        assert_eq!(owner_lock_free(&dir.join("missing")), Ok(true));
        assert!(!dir.join("missing").exists());  // nothing is created
        assert_eq!(owner_lock_free(&dir), Ok(true));
        let holder = std::fs::OpenOptions::new().read(true).append(true).create(true)
            .open(dir.join("owner.lock")).unwrap();
        assert_eq!(unsafe { libc::flock(holder.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) }, 0);
        assert_eq!(owner_lock_free(&dir), Ok(false));
        drop(holder);
        assert_eq!(owner_lock_free(&dir), Ok(true));
        assert_eq!(owner_lock_free(&dir), Ok(true));  // the check itself does not keep the lock
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn the_device_lock_is_exclusive_per_serial_and_its_file_is_kept() {
        let _serial = serial_fd_tests();
        let dir = temp_dir("device");
        // The same fixed directory and name as scripts/android/runner.py's device_lock_path (scripts/test_android_runner.py
        // checks them too).
        assert_eq!(DEVICE_LOCK_DIR, "/tmp");
        assert_eq!(
            device_lock_path(Path::new(DEVICE_LOCK_DIR), "127.0.0.1:5555"),
            Path::new("/tmp/maanikki-android-127.0.0.1_5555.lock")
        );
        assert_eq!(
            device_lock_path(&dir, "127.0.0.1:5555").file_name().unwrap(),
            "maanikki-android-127.0.0.1_5555.lock"
        );
        assert_eq!(device_lock_path(&dir, "emulator-5554/x y").file_name().unwrap(), "maanikki-android-emulator-5554_x_y.lock");
        let held = take_device_lock(&dir, "127.0.0.1:5555").unwrap().expect("free at first");
        // flock locks belong to the open file description: a second handle (as another process would) is refused.
        assert!(take_device_lock(&dir, "127.0.0.1:5555").unwrap().is_none());
        assert!(take_device_lock(&dir, "127.0.0.1:5556").unwrap().is_some());  // another emulator is not affected
        drop(held);
        let again = take_device_lock(&dir, "127.0.0.1:5555").unwrap();
        assert!(again.is_some());
        drop(again);
        assert!(device_lock_path(&dir, "127.0.0.1:5555").exists());  // never removed
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn no_directory_means_nothing_to_clear() {
        let dir = temp_dir("none").join("android-input");
        assert_eq!(clear_under_lock(&dir), ClearOutcome::NoMarker);
        assert!(!dir.exists());  // nothing is created
    }
}

#[cfg(all(test, not(target_os = "macos")))]
mod platform_tests {
    #[test]
    fn other_systems_do_nothing() {
        let dir = std::env::temp_dir().join("maanikki-android-input-unsupported");
        assert_eq!(super::clear_touch_marker(&dir), super::ClearOutcome::Unsupported);
    }
}
