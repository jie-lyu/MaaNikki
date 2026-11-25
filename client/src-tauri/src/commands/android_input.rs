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
