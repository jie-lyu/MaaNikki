//! Agent 相关命令
//!
//! 提供 MaaFramework Agent 启动和管理功能

use log::{debug, error, info, warn};
use std::collections::HashMap;
use std::fs::OpenOptions;
use std::io::{BufRead, BufReader, Write};
use std::path::{Component, Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Condvar, Mutex, OnceLock};
use std::thread;

use chrono::Local;
use tauri::{Emitter, Manager, State};

use maa_framework::agent_client::AgentClient;
use maa_framework::controller::Controller;
use maa_framework::resource::Resource;
use maa_framework::tasker::Tasker;

use super::types::{AgentConfig, ControllerInfo, MaaState, TaskConfig};
use super::utils::{emit_callback_event, get_logs_dir, handle_task_callback, normalize_path};
use regex::Regex;
use std::sync::LazyLock;
use std::time::{Duration, Instant};

#[cfg(windows)]
fn assign_child_to_kill_job(child: &Child) {
    use std::ffi::c_void;
    use std::os::windows::io::AsRawHandle;

    const JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS: i32 = 9;
    const JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: u32 = 0x0000_2000;

    #[repr(C)]
    #[derive(Default)]
    struct IoCounters {
        read_operation_count: u64,
        write_operation_count: u64,
        other_operation_count: u64,
        read_transfer_count: u64,
        write_transfer_count: u64,
        other_transfer_count: u64,
    }

    #[repr(C)]
    #[derive(Default)]
    struct JobObjectBasicLimitInformation {
        per_process_user_time_limit: i64,
        per_job_user_time_limit: i64,
        limit_flags: u32,
        minimum_working_set_size: usize,
        maximum_working_set_size: usize,
        active_process_limit: u32,
        affinity: usize,
        priority_class: u32,
        scheduling_class: u32,
    }

    #[repr(C)]
    #[derive(Default)]
    struct JobObjectExtendedLimitInformation {
        basic_limit_information: JobObjectBasicLimitInformation,
        io_info: IoCounters,
        process_memory_limit: usize,
        job_memory_limit: usize,
        peak_process_memory_used: usize,
        peak_job_memory_used: usize,
    }

    #[link(name = "kernel32")]
    extern "system" {
        fn CreateJobObjectW(security_attrs: *mut c_void, name: *const u16) -> *mut c_void;
        fn SetInformationJobObject(
            job: *mut c_void,
            info_class: i32,
            info: *const c_void,
            info_len: u32,
        ) -> i32;
        fn AssignProcessToJobObject(job: *mut c_void, process: *mut c_void) -> i32;
        fn CloseHandle(handle: *mut c_void) -> i32;
    }

    static AGENT_JOB: OnceLock<usize> = OnceLock::new();
    let job = *AGENT_JOB.get_or_init(|| unsafe {
        let h = CreateJobObjectW(std::ptr::null_mut(), std::ptr::null());
        if h.is_null() {
            warn!("[agent] CreateJobObjectW failed");
            return 0;
        }
        let mut info = JobObjectExtendedLimitInformation::default();
        info.basic_limit_information.limit_flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if SetInformationJobObject(
            h,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            &info as *const _ as *const c_void,
            std::mem::size_of::<JobObjectExtendedLimitInformation>() as u32,
        ) == 0
        {
            warn!("[agent] SetInformationJobObject failed");
            CloseHandle(h);
            return 0;
        }
        h as usize
    });

    if job != 0 {
        unsafe {
            if AssignProcessToJobObject(job as *mut c_void, child.as_raw_handle() as *mut c_void)
                == 0
            {
                warn!(
                    "[agent] AssignProcessToJobObject failed for pid {}: {}",
                    child.id(),
                    std::io::Error::last_os_error()
                );
            }
        }
    }
}

/// Agent 输出事件载荷
#[derive(Clone, serde::Serialize)]
pub struct AgentOutputEvent {
    pub instance_id: String,
    pub stream: String,
    pub line: String,
}

struct AgentOutputBatchState {
    lines: Vec<String>,
    first_stream: Option<String>,
    has_mixed_streams: bool,
    flush_deadline: Option<Instant>,
    closed: bool,
}

struct AgentOutputBatcherInner {
    app: tauri::AppHandle,
    instance_id: String,
    state: Mutex<AgentOutputBatchState>,
    notify: Condvar,
}

pub(crate) struct AgentOutputBatcher {
    inner: Arc<AgentOutputBatcherInner>,
}

impl AgentOutputBatcher {
    pub(crate) fn new(app: tauri::AppHandle, instance_id: String) -> Arc<Self> {
        let inner = Arc::new(AgentOutputBatcherInner {
            app,
            instance_id,
            state: Mutex::new(AgentOutputBatchState {
                lines: Vec::new(),
                first_stream: None,
                has_mixed_streams: false,
                flush_deadline: None,
                closed: false,
            }),
            notify: Condvar::new(),
        });
        let worker = Arc::clone(&inner);
        thread::spawn(move || Self::flush_loop(worker));
        Arc::new(Self { inner })
    }

    pub(crate) fn enqueue(&self, stream: &str, line: &str) {
        let mut state = self.inner.state.lock().unwrap();
        if state.closed {
            return;
        }
        let was_empty = state.flush_deadline.is_none();
        state.lines.push(line.to_string());
        match state.first_stream.as_ref() {
            None => state.first_stream = Some(stream.to_string()),
            Some(existing) if existing != stream => state.has_mixed_streams = true,
            _ => {}
        }
        state.flush_deadline = Some(Instant::now() + Duration::from_millis(1));
        if was_empty {
            self.inner.notify.notify_one();
        }
    }

    fn flush_loop(inner: Arc<AgentOutputBatcherInner>) {
        loop {
            let payload = {
                let mut state = inner.state.lock().unwrap();
                loop {
                    if let Some(deadline) = state.flush_deadline {
                        let now = Instant::now();
                        if deadline > now && !state.closed {
                            let (guard, _) = inner
                                .notify
                                .wait_timeout(state, deadline.duration_since(now))
                                .unwrap();
                            state = guard;
                            continue;
                        }
                        state.flush_deadline = None;
                        if !state.lines.is_empty() {
                            let merged_line = state.lines.join("\n");
                            state.lines.clear();
                            let stream = if state.has_mixed_streams {
                                "mixed".to_string()
                            } else {
                                state
                                    .first_stream
                                    .take()
                                    .unwrap_or_else(|| "stdout".to_string())
                            };
                            state.has_mixed_streams = false;
                            break Some((stream, merged_line));
                        }
                    }
                    if state.closed {
                        return;
                    }
                    state = inner.notify.wait(state).unwrap();
                }
            };

            if let Some((stream, merged_line)) = payload {
                emit_agent_output(&inner.app, &inner.instance_id, &stream, &merged_line);
            }
        }
    }
}

impl Drop for AgentOutputBatcher {
    fn drop(&mut self) {
        let mut state = self.inner.state.lock().unwrap();
        state.closed = true;
        self.inner.notify.notify_all();
    }
}

/// 发送 Agent 输出事件（Tauri WebView + WebSocket 浏览器客户端）
pub(crate) fn emit_agent_output(app: &tauri::AppHandle, instance_id: &str, stream: &str, line: &str) {
    let clean_line = strip_ansi_escapes(line);

    // 广播到所有 WebSocket 客户端
    if let Some(ws) = app.try_state::<Arc<crate::ws_broadcast::WsBroadcast>>() {
        ws.send(crate::ws_broadcast::WsEvent::AgentOutput {
            instance_id: instance_id.to_string(),
            stream: stream.to_string(),
            line: clean_line.clone(),
        });
    }

    // 发送到 Tauri WebView
    let event = AgentOutputEvent {
        instance_id: instance_id.to_string(),
        stream: stream.to_string(),
        line: clean_line,
    };
    if let Err(e) = app.emit("maa-agent-output", event) {
        log::error!("[agent_output] Failed to emit event: {}", e);
    }
}

/// 移除 ANSI 转义序列
static ANSI_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07?").unwrap());

fn strip_ansi_escapes(s: &str) -> String {
    ANSI_RE.replace_all(s, "").into_owned()
}

/// 判断给定 child_exec 是否是“裸命令名”（仅包含单个普通组件且不含路径分隔符）。
///
/// 例如 `python` / `node` / `git` 返回 true，`./agent.py` / `subdir/tool` 返回 false。
fn is_bare_command(child_exec: &str, path: &Path) -> bool {
    // `python/`、`python\` 这类带分隔符输入应视为路径而非裸命令。
    if child_exec.contains('/') || child_exec.contains('\\') {
        return false;
    }

    let mut components = path.components();
    matches!(components.next(), Some(Component::Normal(_))) && components.next().is_none()
}

/// child_exec 的解析类别。
enum ChildExecKind {
    Empty,
    BareCommand,
    RelativePath,
    AbsoluteOrPrefixedPath,
}

/// 对 child_exec 做一次统一分类，供后续路径解析逻辑复用。
fn classify_child_exec(child_exec: &str) -> ChildExecKind {
    if child_exec.is_empty() {
        return ChildExecKind::Empty;
    }

    let path = Path::new(child_exec);
    let first = path.components().next();

    if is_bare_command(child_exec, path) {
        return ChildExecKind::BareCommand;
    }

    // 根目录路径（如 /usr/bin/python 或 \Windows\System32）不拼接 cwd。
    if matches!(first, Some(Component::RootDir)) {
        return ChildExecKind::AbsoluteOrPrefixedPath;
    }

    // Windows 盘符前缀（如 C:python.exe / C:\Python\python.exe）不拼接 cwd
    if matches!(first, Some(Component::Prefix(_))) {
        return ChildExecKind::AbsoluteOrPrefixedPath;
    }

    // 其余都视作相对路径（如 ./x、../x、subdir/x）。
    ChildExecKind::RelativePath
}

/// 解析 agent 可执行入口路径：
/// - 空字符串 child_exec -> 原样返回（不拼接 cwd、不做规范化）
/// - 裸命令名（如 `python` / `node`）-> 原样返回，由系统 PATH 解析
/// - 相对路径型 child_exec -> 先基于 cwd 拼接，再通过 `normalize_path` 规范化
/// - 绝对路径 / 带盘符前缀路径 -> 不拼接 cwd，但会通过 `normalize_path` 规范化
///
/// 供 Agent 启动与 pretask 执行共用，确保相对入口的解析行为一致：Windows 下相对
/// 可执行路径会相对“父进程当前目录”而非子进程 `current_dir` 解析，因此必须在拼好
/// 绝对路径后再交给 `Command`，否则会出现“系统找不到指定的路径 (os error 3)”。
pub(crate) fn resolve_child_exec_path(child_exec: &str, cwd: &str) -> PathBuf {
    match classify_child_exec(child_exec) {
        // 空字符串由上层提前校验；这里保守返回原值，避免误拼 cwd。
        ChildExecKind::Empty => PathBuf::from(child_exec),
        // 裸命令名（例如 python / node）直接走 PATH，不做路径规范化。
        ChildExecKind::BareCommand => PathBuf::from(child_exec),
        ChildExecKind::RelativePath => {
            let joined = Path::new(cwd).join(child_exec);
            normalize_path(&joined.to_string_lossy())
        }
        ChildExecKind::AbsoluteOrPrefixedPath => normalize_path(child_exec),
    }
}

/// Windows Application Control policy rejection (Smart App Control).
///
/// `CreateProcess` returns this when an executable is blocked as untrusted.
/// Typical message: "An Application Control policy has blocked this file."
#[cfg(windows)]
const WINDOWS_ERROR_APPLICATION_CONTROL_BLOCKED: i32 = 4551;

fn agent_spawn_hint_tag(error: &std::io::Error) -> Option<&'static str> {
    if error.kind() == std::io::ErrorKind::NotFound {
        return Some(" [[hint:spawn_file_not_found]]");
    }
    #[cfg(windows)]
    if error.raw_os_error() == Some(WINDOWS_ERROR_APPLICATION_CONTROL_BLOCKED) {
        return Some(" [[hint:spawn_app_control]]");
    }
    None
}

/// 启动单个 Agent 子进程并完成连接
async fn start_single_agent(
    app: tauri::AppHandle,
    agent: AgentConfig,
    agent_index: usize,
    instance_id: String,
    cwd: String,
    tcp_compat_mode: bool,
    resource: Resource,
    controller: Controller,
    tasker: Tasker,
    pi_envs: Arc<HashMap<String, String>>,
) -> Result<(AgentClient, std::process::Child), String> {
    info!("[agent#{}] Starting agent: {:?}", agent_index, agent);

    // 将整个启动过程移入 spawn_blocking，避免阻塞 async runtime 线程
    tauri::async_runtime::spawn_blocking(move || {
        let mut client = if tcp_compat_mode {
            debug!("[agent#{}] Creating TCP agent client...", agent_index);
            AgentClient::create_tcp(0).or_else(|e| {
                warn!(
                    "[agent#{}] TCP compat mode requested but failed: {}, falling back to default (IPC)",
                    agent_index, e
                );
                AgentClient::new(None)
            }).map_err(|e| e.to_string())?
        } else {
            debug!("[agent#{}] Creating default agent client...", agent_index);
            AgentClient::new(None).map_err(|e| e.to_string())?
        };

        if let Err(e) = client.bind(resource.clone()) {
            warn!("[agent#{}] Failed to bind resource: {}", agent_index, e);
            return Err(e.to_string());
        }

        let socket_id = client
            .identifier()
            .ok_or_else(|| format!("Failed to get identifier for agent #{}", agent_index))?;
        info!("[agent#{}] Agent socket_id: {}", agent_index, socket_id);

        // 启动子进程
        let mut args = agent.child_args.clone().unwrap_or_default();
        args.push(socket_id.clone());

        let child_exec = agent.child_exec.trim();
        if child_exec.is_empty() {
            return Err(format!(
                "Failed to spawn agent #{}: child_exec is empty",
                agent_index
            ));
        }

        let exec_path = resolve_child_exec_path(child_exec, &cwd);

        info!(
            "[agent#{}] Spawning process: {:?} {:?} in {}",
            agent_index, exec_path, args, cwd
        );

        #[cfg(windows)]
        let mut cmd = {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x08000000;
            let mut c = Command::new(&exec_path);
            c.creation_flags(CREATE_NO_WINDOW);
            c
        };

        #[cfg(not(windows))]
        let mut cmd = Command::new(&exec_path);

        cmd.args(&args)
            .current_dir(&cwd)
            .env("PYTHONIOENCODING", "utf-8")
            .env("PYTHONUTF8", "1")
            .env("OMP_NUM_THREADS", "2")
            .env("OMP_WAIT_POLICY", "PASSIVE")
            .env("OPENBLAS_NUM_THREADS", "2")
            .env("MKL_NUM_THREADS", "2")
            .env("NUMEXPR_NUM_THREADS", "2")
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());

        super::utils::apply_agent_root_env(&mut cmd);
        // 发布构建的 Mac 客户端从不把开发开关传给 agent，即使用户的环境里有这个变量。
        #[cfg(all(target_os = "macos", not(debug_assertions)))]
        cmd.env_remove(super::utils::MACOS_EXTRA_TASKS_ENV);

        if let Some(overlay_dir) = super::data_update::active_slot_dir() {
            cmd.env("PI_MAANIKKI_DATA_OVERLAY", overlay_dir);
        }

        if let Some(guard_channel) = crate::mouse_guard::ensure_server_channel() {
            cmd.env("MAANIKKI_MOUSE_GUARD_CHANNEL", guard_channel);
        }

        // 本次运行的控制器只能来自这次提交：客户端自身环境里继承来的 PI_CONTROLLER 不传给 agent。
        cmd.env_remove("PI_CONTROLLER");

        // PI v2.5.0: 仅允许注入 PI_* 环境变量，避免覆盖宿主进程关键环境。
        let mut injected_count = 0usize;
        for (key, value) in pi_envs.iter() {
            // Windows 的环境变量名不分大小写：大小写不同的 PI_CONTROLLER 会被 agent 当作它，而准入只认
            // 精确的名字，两边会不一致。
            if key != "PI_CONTROLLER" && key.eq_ignore_ascii_case("PI_CONTROLLER") {
                warn!("[agent#{}] Skipping {}: only PI_CONTROLLER names the run's controller", agent_index, key);
                continue;
            }
            if !key.starts_with("PI_") {
                warn!(
                    "[agent#{}] Skipping non-PI_ env key from pi_envs: {}",
                    agent_index, key
                );
                continue;
            }

            cmd.env(key, value);
            injected_count += 1;
        }
        if injected_count > 0 {
            info!(
                "[agent#{}] Injected {} PI_* env vars (requested: {})",
                agent_index,
                injected_count,
                pi_envs.len()
            );
        } else if !pi_envs.is_empty() {
            warn!(
                "[agent#{}] No PI_* env vars were injected ({} entries provided)",
                agent_index,
                pi_envs.len()
            );
        }

        let mut child = cmd.spawn().map_err(|e| {
            let mut msg = format!(
                "Failed to spawn agent #{}: {} (path: {:?})",
                agent_index, e, exec_path
            );
            if let Some(tag) = agent_spawn_hint_tag(&e) {
                msg.push_str(tag);
            }
            msg
        })?;

        #[cfg(windows)]
        assign_child_to_kill_job(&child);

        // agent 日志文件路径（延迟创建：仅在有实际输出时才打开文件）
        let pid = child.id();
        let log_filename = format!("mxu-agent-{}-{}.log", agent_index, pid);
        let agent_log_path = Arc::new(get_logs_dir().join(&log_filename));
        let log_file: Arc<Mutex<Option<std::fs::File>>> = Arc::new(Mutex::new(None));
        let output_batcher = AgentOutputBatcher::new(app.clone(), instance_id.clone());

        // 在单独线程中读取 stdout
        if let Some(stdout) = child.stdout.take() {
            let lf = log_file.clone();
            let lf_path = agent_log_path.clone();
            let batcher = output_batcher.clone();
            thread::spawn(move || {
                let mut reader = BufReader::new(stdout);
                let mut buffer = Vec::new();
                loop {
                    buffer.clear();
                    match reader.read_until(b'\n', &mut buffer) {
                        Ok(0) => break,
                        Ok(_) => {
                            let line = String::from_utf8_lossy(&buffer);
                            let clean_line = line.trim_end();
                            if let Ok(mut guard) = lf.lock() {
                                if guard.is_none() {
                                    *guard = OpenOptions::new()
                                        .create(true)
                                        .append(true)
                                        .open(lf_path.as_ref())
                                        .ok();
                                }
                                if let Some(file) = guard.as_mut() {
                                    let timestamp = Local::now().format("%Y-%m-%d %H:%M:%S");
                                    let _ = writeln!(file, "{} [stdout] {}", timestamp, clean_line);
                                }
                            }
                            info!(target: "agent", "[agent#{}][stdout] {}", agent_index, clean_line);
                            batcher.enqueue("stdout", clean_line);
                        }
                        Err(_) => break,
                    }
                }
            });
        }

        // Stderr thread
        if let Some(stderr) = child.stderr.take() {
            let lf = log_file.clone();
            let lf_path = agent_log_path.clone();
            let batcher = output_batcher.clone();
            thread::spawn(move || {
                let mut reader = BufReader::new(stderr);
                let mut buffer = Vec::new();
                loop {
                    buffer.clear();
                    match reader.read_until(b'\n', &mut buffer) {
                        Ok(0) => break,
                        Ok(_) => {
                            let line = String::from_utf8_lossy(&buffer);
                            let clean_line = line.trim_end();
                            if let Ok(mut guard) = lf.lock() {
                                if guard.is_none() {
                                    *guard = OpenOptions::new()
                                        .create(true)
                                        .append(true)
                                        .open(lf_path.as_ref())
                                        .ok();
                                }
                                if let Some(file) = guard.as_mut() {
                                    let timestamp = Local::now().format("%Y-%m-%d %H:%M:%S");
                                    let _ = writeln!(file, "{} [stderr] {}", timestamp, clean_line);
                                }
                            }
                            warn!(target: "agent", "[agent#{}][stderr] {}", agent_index, clean_line);
                            batcher.enqueue("stderr", clean_line);
                        }
                        Err(_) => break,
                    }
                }
            });
        }

        // 设置连接超时
        let timeout = agent.timeout.unwrap_or(-1);
        // macOS：握手最多等 2 分钟，避免一个始终不回应的 agent 让这一轮一直停在启动中；
        // 连接后恢复配置的超时（动作可能运行很久）。
        #[cfg(target_os = "macos")]
        let connect_timeout = if timeout < 0 { 120_000 } else { timeout.min(120_000) };
        #[cfg(not(target_os = "macos"))]
        let connect_timeout = timeout;
        if let Err(e) = client.set_timeout(connect_timeout) {
            warn!("Failed to set timeout for agent #{}: {}", agent_index, e);
        }

        info!("[agent#{}] Connecting to agent...", agent_index);

        if let Err(e) = client.connect() {
             error!("[agent#{}] Connection failed: {}", agent_index, e);
             let _ = child.kill();
             let _ = child.wait();
             return Err(e.to_string());
        }
        if connect_timeout != timeout {
            if let Err(e) = client.set_timeout(timeout) {
                warn!("Failed to restore timeout for agent #{}: {}", agent_index, e);
            }
        }

        info!("[agent#{}] Connected successfully!", agent_index);

        // 注册 Agent sink
        if let Err(e) = client.register_sinks(resource, controller, tasker) {
            error!("[agent#{}] Failed to register sinks: {}", agent_index, e);
            let _ = child.kill();
            let _ = child.wait();
            return Err(e.to_string());
        }

        Ok((client, child))
    }).await.map_err(|e| e.to_string())?
}

/// 启动任务的核心实现（Tauri invoke 和 HTTP handler 共享）
pub async fn start_tasks_impl(
    app: tauri::AppHandle,
    maa_state: &Arc<MaaState>,
    instance_id: String,
    tasks: Vec<TaskConfig>,
    agent_configs: Option<Vec<AgentConfig>>,
    cwd: String,
    tcp_compat_mode: bool,
    mut pi_envs: Option<HashMap<String, String>>,
    reset_state: bool,
    controller_info: Option<ControllerInfo>,
) -> Result<Vec<i64>, String> {
    info!("start_tasks_impl called");

    // 在重新连接或启动 agent 之前按当前系统筛掉不能运行的任务：全部被拒绝时直接返回原因；
    // 部分被拒绝时，交给 agent 的运行计划只列出会执行的任务，避免被跳过的任务影响其余任务的行为。
    // Windows 上没有任务被拒绝，不做任何改动。
    // 按已连接的控制器决定游戏目标，并核对它与本次提交的控制器一致（与 agent 的 host.game_backend 相同）；
    // 两者都没有时按当前系统。控制器本身不能运行时整批拒绝，即使没有任务。
    let connected = {
        let instances = maa_state.instances.lock().map_err(|e| e.to_string())?;
        super::utils::ControllerKind::of_instance(
            instances.get(&instance_id).and_then(|inst| inst.controller_config.as_ref()),
        )
    };
    let submitted = super::utils::submitted_controller_kind(
        pi_envs.as_ref(),
        controller_info.as_ref().and_then(|info| info.type_name.as_deref()),
    );
    let entries: Vec<&str> = tasks.iter().map(|task| task.entry.as_str()).collect();
    let refusals = match super::utils::admit_tasks(connected.as_ref(), submitted.as_ref(), &entries) {
        Ok(refusals) => refusals,
        Err(reason) => {
            emit_agent_output(&app, &instance_id, "stderr", &reason);
            return Err(reason);
        }
    };
    // 安卓模拟器：通过准入之后才查的开始条件（客户端放开 Adb 准入之前不会走到这里）。
    if super::utils::targets_android(connected.as_ref(), submitted.as_ref()) {
        let has_agent = agent_configs.as_ref().is_some_and(|configs| !configs.is_empty());
        if let Some(reason) =
            super::utils::android_start_refusal(reset_state, pi_envs.as_ref(), tasks.len(), has_agent)
        {
            emit_agent_output(&app, &instance_id, "stderr", &reason);
            return Err(reason);
        }
    }
    // 绑定控制器之前按新的连接重新准入时用（见下方）。
    let admitted_names: Vec<String> = entries.iter().map(|e| e.to_string()).collect();
    let admitted_entries: Vec<&str> = admitted_names.iter().map(String::as_str).collect();
    let admitted_refusals = refusals.clone();
    if refusals.iter().any(Option::is_some) {
        if refusals.iter().all(Option::is_some) {
            let reason = refusals.into_iter().flatten().next().unwrap_or_default();
            emit_agent_output(&app, &instance_id, "stderr", &reason);
            return Err(reason);
        }
        let accepted: Vec<&str> = tasks
            .iter()
            .zip(&refusals)
            .filter(|(_, refused)| refused.is_none())
            .map(|(task, _)| task.entry.as_str())
            .collect();
        pi_envs = pi_envs.map(|mut envs| {
            if let Some(plan) = envs.get_mut("PI_MAANIKKI_RUN_PLAN") {
                *plan = super::utils::run_plan_with_entries(plan, &accepted);
            }
            envs
        });
    }

    info!("instance_id: {}", instance_id);
    info!("tasks: {:?}", tasks);
    info!("agent_configs: {:?}", agent_configs);
    info!("cwd: {}, tcp_compat_mode: {}", cwd, tcp_compat_mode);

    // Mac：同时只有一轮运行。在一个临界区里取得这一轮（上一轮仍在启动、运行或收尾时在锁外等待，
    // 最多 3 秒），之后才决定是否重连；启动失败、取消或没有提交任务时 `mac_claim` 撤销自己的那一轮。
    let tasks_mapped = tasks.iter().all(|task| task.selected_task_id.is_some());
    let mac_claim = claim_macos_run(maa_state, &instance_id, reset_state, tasks_mapped).await?;
    let mac_token = mac_claim.as_ref().map(|claim| claim.token);

    let reconnect_config = {
        let mut instances = maa_state.instances.lock().map_err(|e| e.to_string())?;
        instances.get_mut(&instance_id).and_then(|inst| {
            inst.start_generation = inst.start_generation.wrapping_add(1);
            // Mac 运行总是用新的连接和新的 Tasker：上一轮的 Tasker 交给退役，它的回调不再算数。
            if inst.controller_stopped || (mac_token.is_some() && inst.tasker.is_some()) {
                inst.controller_config.clone()
            } else {
                None
            }
        })
    };
    if let Some(cfg) = reconnect_config {
        info!(
            "[start_tasks] Refreshing stopped controller before starting tasks for instance {}",
            instance_id
        );
        let app_for_ctrl = app.clone();
        let conn_id = super::maa_core::connect_controller_impl(
            Arc::clone(maa_state),
            instance_id.clone(),
            cfg,
            Arc::new(move |msg, detail| emit_callback_event(&app_for_ctrl, msg, detail)),
        )
        .await?;
        let ctrl_to_wait = {
            let instances = maa_state.instances.lock().map_err(|e| e.to_string())?;
            instances
                .get(&instance_id)
                .and_then(|inst| inst.controller.clone())
        };
        if let Some(ctrl) = ctrl_to_wait {
            let connected = tokio::task::spawn_blocking(move || {
                if conn_id > 0 {
                    ctrl.wait(conn_id).succeeded() && ctrl.connected()
                } else {
                    ctrl.connected()
                }
            })
            .await
            .map_err(|e| e.to_string())?;
            if !connected {
                return Err("游戏截图控制器重连失败，请检查游戏窗口后重试".to_string());
            }
        }
    }

    let (resource, controller, tasker, start_generation, stale_clients, stale_children) = {
        debug!("[start_tasks] Acquiring instances lock...");
        let mut instances = maa_state.instances.lock().map_err(|e| e.to_string())?;
        debug!("[start_tasks] Instances lock acquired");
        let instance = instances
            .get_mut(&instance_id)
            .ok_or("Instance not found")?;
        debug!("[start_tasks] Instance found: {}", instance_id);

        let res = instance
            .resource
            .as_ref()
            .ok_or("Resource not loaded")?
            .clone();
        debug!("[start_tasks] Resource acquired");

        let ctrl = instance
            .controller
            .as_ref()
            .ok_or("Controller not connected")?
            .clone();
        debug!("[start_tasks] Controller acquired");

        // 准入看的是开始时的连接；在绑定控制器、改动任何状态之前，在这把锁里核对：连接变了就按新的连接
        // 重新准入，结果相同才继续（同一份配置的刷新、准入后才完成的第一次 Win32 连接都照常运行）。
        super::utils::readmit_on_connection(
            connected.as_ref(),
            instance.controller_config.as_ref(),
            submitted.as_ref(),
            &admitted_entries,
            &admitted_refusals,
        )?;

        // Mac：重连期间用户已停止这次启动，不能清掉停止标记把新的代数当作自己的。
        if mac_token.is_some_and(|token| !super::mac_connection::start_owned(instance.macos_run, token)) {
            return Err("MXU_PRE_ACTION_CANCELLED".to_string());
        }
        instance.stop_in_progress = false;
        instance.stop_started_at = None;
        instance.start_generation = instance.start_generation.wrapping_add(1);
        let start_generation = instance.start_generation;
        let (stale_clients, stale_children) = if reset_state {
            (
                std::mem::take(&mut instance.agent_clients),
                std::mem::take(&mut instance.agent_children),
            )
        } else {
            (Vec::new(), Vec::new())
        };

        // 创建或获取 tasker（若已有 tasker 但未初始化则自动丢弃并重建）
        let needs_new_tasker = match instance.tasker.as_ref() {
            None => true,
            Some(t) => !t.inited(),
        };
        if needs_new_tasker {
            if instance.tasker.is_some() {
                warn!("[start_tasks] Existing tasker is not initialized, discarding and rebuilding...");
                instance.tasker = None;
            }

            debug!("[start_tasks] Creating new tasker...");
            let t = Tasker::new().map_err(|e| e.to_string())?;
            debug!("[start_tasks] Tasker created");
            instance.tasker_serial = instance.tasker_serial.wrapping_add(1);
            let tasker_serial = instance.tasker_serial;

            // 添加回调 Sink，用于接收任务状态通知并更新后端 TaskRunState
            debug!("[start_tasks] Adding tasker sink...");
            let app_handle = app.clone();
            let maa_state_for_sink = Arc::clone(maa_state);
            let inst_id_for_sink = instance_id.clone();
            t.add_sink(move |msg, detail| {
                // 先更新后端 TaskRunState（单一真相来源）
                handle_task_callback(
                    &maa_state_for_sink,
                    &app_handle,
                    &inst_id_for_sink,
                    tasker_serial,
                    msg,
                    detail,
                );
                // 再转发原始回调到前端
                emit_callback_event(&app_handle, msg, detail);
            })
            .map_err(|e| e.to_string())?;
            debug!("[start_tasks] Tasker sink added");

            // 添加 Context Sink，用于接收 Node 级别的通知（包含 focus 消息）
            debug!("[start_tasks] Adding tasker context sink...");
            let app_handle = app.clone();
            let inst_id_for_ctx_sink = instance_id.clone();
            t.add_context_sink(move |msg, detail| {
                // 遥测：把失败节点挂到任务 Span 下，形成失败链路
                super::telemetry::on_node_event(&inst_id_for_ctx_sink, msg, detail);
                emit_callback_event(&app_handle, msg, detail);
            })
            .map_err(|e| e.to_string())?;
            debug!("[start_tasks] Tasker context sink added");

            debug!("[start_tasks] Binding resource and controller...");
            t.bind(&res, &ctrl).map_err(|e| e.to_string())?;
            debug!("[start_tasks] Resource and controller bound");

            instance.tasker = Some(t);
            debug!("[start_tasks] Tasker created and stored");
        } else {
            debug!("[start_tasks] Using existing initialized tasker");
        }

        let t = instance.tasker.as_ref().unwrap().clone();
        (
            res,
            ctrl,
            t,
            start_generation,
            stale_clients,
            stale_children,
        )
    };
    debug!("[start_tasks] Resource, controller and tasker acquired, proceeding...");

    if !stale_clients.is_empty() || !stale_children.is_empty() {
        info!(
            "[start_tasks] Cleaning up {} leftover agent client(s) and {} child process(es) before start...",
            stale_clients.len(),
            stale_children.len()
        );
        for client in &stale_clients {
            disconnect_agent(client);
        }
        drop(stale_clients);
        for mut child in stale_children {
            let _ = child.kill();
            let _ = child.wait();
        }
    }

    // 检查 Tasker 初始化状态
    if !tasker.inited() {
        error!("[start_tasks] Tasker not properly initialized");
        return Err("Tasker not properly initialized".to_string());
    }

    // 启动所有 Agent（如果配置了）
    debug!("[start_tasks] Checking agent configs...");
    let pi_envs = Arc::new(pi_envs.unwrap_or_default());
    if let Some(configs) = agent_configs {
        if configs.is_empty() {
            debug!("[start_tasks] Agent configs list is empty, skipping agent setup");
        } else {
            info!("[start_tasks] Starting {} agent(s)...", configs.len());

            // 用于收集所有成功启动的 agent，失败时需要回滚清理
            let mut new_clients: Vec<AgentClient> = Vec::new();
            let mut new_children: Vec<std::process::Child> = Vec::new();

            for (idx, config) in configs.iter().enumerate() {
                let cancelled = {
                    let instances = maa_state.instances.lock().map_err(|e| e.to_string())?;
                    !matches!(
                        instances.get(&instance_id),
                        Some(instance)
                            if instance.start_generation == start_generation
                                && !instance.stop_in_progress
                    )
                };
                if cancelled {
                    info!(
                        "[start_tasks] Start cancelled before agent #{} for instance {}, cleaning up...",
                        idx, instance_id
                    );
                    for client in &new_clients {
                        disconnect_agent(client);
                    }
                    drop(new_clients);
                    for mut child in new_children {
                        let _ = child.kill();
                        let _ = child.wait();
                    }
                    return Err("MXU_PRE_ACTION_CANCELLED".to_string());
                }

                let res_clone = resource.clone();
                let ctrl_clone = controller.clone();
                let tasker_clone = tasker.clone();
                let app_handle = app.clone();
                let inst_id = instance_id.clone();
                let cwd_clone = cwd.clone();
                let pi_envs_clone = Arc::clone(&pi_envs);

                match start_single_agent(
                    app_handle,
                    config.clone(),
                    idx,
                    inst_id,
                    cwd_clone,
                    tcp_compat_mode,
                    res_clone,
                    ctrl_clone,
                    tasker_clone,
                    pi_envs_clone,
                )
                .await
                {
                    Ok((client, child)) => {
                        new_clients.push(client);
                        new_children.push(child);
                    }
                    Err(e) => {
                        error!(
                            "[start_tasks] Agent #{} failed to start: {}, cleaning up previously started agents...",
                            idx, e
                        );

                        // 回滚：清理已启动的 agent
                        for client in &new_clients {
                            disconnect_agent(client);
                        }
                        for mut child in new_children {
                            let _ = child.kill();
                            let _ = child.wait();
                        }
                        return Err(format!("Agent start failed: {}", e));
                    }
                }
            }

            // 保存所有 agent 状态到 instance（若启动期间收到 stop/新启动请求则回滚）
            let cancelled = {
                let mut instances = maa_state.instances.lock().map_err(|e| e.to_string())?;
                match instances.get_mut(&instance_id) {
                    Some(instance)
                        if instance.start_generation == start_generation
                            && !instance.stop_in_progress =>
                    {
                        instance
                            .agent_clients
                            .extend(std::mem::take(&mut new_clients));
                        instance
                            .agent_children
                            .extend(std::mem::take(&mut new_children));
                        false
                    }
                    _ => true,
                }
            };
            if cancelled {
                info!(
                    "[start_tasks] Start cancelled during agent startup for instance {}, cleaning up new agent(s)...",
                    instance_id
                );
                for client in &new_clients {
                    disconnect_agent(client);
                }
                drop(new_clients);
                for mut child in new_children {
                    let _ = child.kill();
                    let _ = child.wait();
                }
                return Err("MXU_PRE_ACTION_CANCELLED".to_string());
            }

            info!(
                "[start_tasks] All {} agent(s) started successfully",
                configs.len()
            );

            info!("[start_tasks] Tasks started with agent(s)");
        }
    } else {
        debug!("[start_tasks] No agent configs, skipping agent setup");
    };

    // 提交前再次确认未被取消，避免向已停止的实例创建遥测事务或提交任务
    {
        let instances = maa_state.instances.lock().map_err(|e| e.to_string())?;
        let valid = matches!(
            instances.get(&instance_id),
            Some(instance)
                if instance.start_generation == start_generation && !instance.stop_in_progress
                    && mac_token.map_or(true, |token| super::mac_connection::start_owned(instance.macos_run, token))
        );
        if !valid {
            drop(instances);
            match mac_token {
                Some(token) => abandon_macos_start(maa_state, &instance_id, token),
                None => {
                    let _ = stop_agent_impl(maa_state, &instance_id);
                }
            }
            return Err("MXU_PRE_ACTION_CANCELLED".to_string());
        }
    }

    // 遥测：整批运行开始（仅首批；追加批次沿用已有 Transaction）
    // 必须在 post_task 之前，否则首个任务的开始回调会早于 Transaction 创建、丢掉它的 Span
    if reset_state {
        let task_names: Vec<String> = tasks
            .iter()
            .map(|task| task.task_name.clone().unwrap_or_else(|| task.entry.clone()))
            .collect();
        super::telemetry::on_run_start(&instance_id, &task_names, controller_info.as_ref());
    }

    debug!("[start_tasks] Submitting {} tasks...", tasks.len());
    // (maa_task_id, selected_task_id) 配对列表，用于后续初始化 TaskRunState
    let mut task_id_pairs: Vec<(i64, Option<String>)> = Vec::new();
    let task_ids_ret;
    {
        // 初始化/追加后端 TaskRunState（单一真相来源）并缓存 task_ids
        debug!(
            "[start_tasks] Updating TaskRunState (reset_state={})...",
            reset_state
        );
        let mut instances = maa_state.instances.lock().map_err(|e| e.to_string())?;
        let instance = instances
            .get_mut(&instance_id)
            .ok_or("Instance not found")?;
        // Mac：与提交在同一个临界区里最后确认这次启动未被停止。
        if let Some(token) = mac_token {
            if !super::mac_connection::start_owned(instance.macos_run, token) {
                drop(instances);
                abandon_macos_start(maa_state, &instance_id, token);
                super::telemetry::on_run_cancelled(&instance_id);
                return Err("MXU_PRE_ACTION_CANCELLED".to_string());
            }
        }
        // 持锁提交，确保任务开始回调不会早于遥测元数据登记，且遵循 instances -> RUNS 锁顺序
        let mut telemetry_posting = super::telemetry::begin_posting(&instance_id);
        if reset_state {
            // 首批：重置任务运行状态
            instance.task_ids.clear();
            let state = &mut instance.task_run_state;
            state.statuses.clear();
            state.mappings.clear();
            state.pending_task_ids.clear();
            state.current_task_index = 0;
        }
        for (idx, task) in tasks.iter().enumerate() {
            debug!("[start_tasks] Preparing task {}: entry={}", idx, task.entry);

            if let Some(reason) = refusals[idx].as_ref() {
                warn!("[start_tasks] Refusing task on this platform: {}", task.entry);
                emit_agent_output(&app, &instance_id, "stderr", reason);
                if let Some(sel_id) = &task.selected_task_id {
                    instance
                        .task_run_state
                        .statuses
                        .insert(sel_id.clone(), "failed".to_string());
                }
                continue;
            }

            info!(
                "[start_tasks] Calling post_task: entry={}, override_len={}",
                task.entry,
                task.pipeline_override.len()
            );
            match tasker.post_task(&task.entry, &task.pipeline_override) {
                Ok(job) => {
                    info!("[start_tasks] post_task returned task_id: {}", job.id);
                    task_id_pairs.push((job.id, task.selected_task_id.clone()));
                    // 立即登记，保证回调不会先于状态
                    let state = &mut instance.task_run_state;
                    state.pending_task_ids.push(job.id);
                    if let Some(sel_id) = &task.selected_task_id {
                        state.mappings.insert(job.id, sel_id.clone());
                        state.statuses.insert(sel_id.clone(), "pending".to_string());
                    }
                    let task_name_for_stats =
                        task.task_name.clone().unwrap_or_else(|| task.entry.clone());
                    super::task_stats::register_posted_task(
                        &instance_id,
                        job.id,
                        &task_name_for_stats,
                    );
                    if let Some(posting) = telemetry_posting.as_mut() {
                        posting.register(
                            job.id,
                            super::telemetry::TaskMeta {
                                // 缺少 interface 任务名时退回 entry，保证 Span 有可读的名字
                                name: task.task_name.clone().unwrap_or_else(|| task.entry.clone()),
                                options: task.options.clone().unwrap_or_default(),
                            },
                        );
                    }
                    debug!(
                        "[start_tasks] Task {} submitted successfully, task_id: {}",
                        idx, job.id
                    );
                }
                Err(_e) => {
                    warn!("[start_tasks] Failed to post task: {}", task.entry);
                }
            }
        }
        let task_ids: Vec<i64> = task_id_pairs.iter().map(|(id, _)| *id).collect();
        task_ids_ret = task_ids.clone();
        debug!(
            "[start_tasks] All tasks submitted, total: {} task_ids",
            task_ids.len()
        );
        let state = &mut instance.task_run_state;
        if reset_state && task_ids.is_empty() {
            state.overall_status = Some("Failed".to_string());
            instance.task_ids.clear();
            drop(telemetry_posting);
            super::telemetry::on_run_cancelled(&instance_id);
        } else {
            state.overall_status = Some("Running".to_string());
            if let Some(token) = mac_token {
                super::mac_connection::start_running(&mut instance.macos_run, token);
            }
            if reset_state {
                instance.task_ids = task_ids;
            } else {
                instance.task_ids.extend(task_ids);
            }
            drop(telemetry_posting);
        }
        debug!("[start_tasks] TaskRunState updated");
    }

    if reset_state && task_ids_ret.is_empty() {
        super::maa_core::schedule_release_idle_desktop_controller(&app, maa_state, &instance_id);
    }

    // 通知所有客户端：任务已启动，需刷新运行时状态
    super::utils::emit_state_changed(&app, &instance_id, "task-started");

    info!(
        "[start_tasks] start_tasks_impl completed successfully, returning {} task_ids",
        task_ids_ret.len()
    );

    Ok(task_ids_ret)
}

/// 被取消的 Mac 启动收尾：只有这一轮仍属于它时，才在一个临界区里取走实例上的 agent（只可能是它
/// 自己启动的）；锁外断开后再撤销自己的那一轮，反注册完成前这一轮一直被占用。
fn abandon_macos_start(maa_state: &Arc<MaaState>, instance_id: &str, token: u64) {
    let taken = maa_state.instances.lock().ok().and_then(|mut instances| {
        let instance = instances.get_mut(instance_id)?;
        if !super::mac_connection::start_owned_or_cancelled(instance.macos_run, token) {
            return None;
        }
        super::mac_connection::cancel_start(&mut instance.macos_run);
        Some((
            std::mem::take(&mut instance.agent_clients),
            std::mem::take(&mut instance.agent_children),
        ))
    });
    if let Some((clients, children)) = taken {
        stop_taken_agents(clients, children);
    }
    if let Ok(mut instances) = maa_state.instances.lock() {
        if let Some(instance) = instances.get_mut(instance_id) {
            super::mac_connection::abandon_start(&mut instance.macos_run, token);
        }
    }
}

/// 一次 Mac 启动取得的那一轮；丢弃时撤销仍处于启动中的那一轮（任务已提交则什么也不做）。
/// 必须在不持有实例锁时丢弃。
struct MacRunClaim {
    state: Arc<MaaState>,
    instance_id: String,
    token: u64,
}

impl Drop for MacRunClaim {
    fn drop(&mut self) {
        if let Ok(mut instances) = self.state.instances.lock() {
            if let Some(instance) = instances.get_mut(&self.instance_id) {
                super::mac_connection::abandon_start(&mut instance.macos_run, self.token);
            }
        }
    }
}

/// Mac 连接上取得这一轮运行；不是 Mac 连接时返回 None。上一轮仍在启动、运行或收尾时在锁外等待，
/// 最多 3 秒。Mac 测试预览不在运行中追加任务（`reset_state` 为 false），也不运行没有任务列表条目
/// （`selected_task_id`）的任务。
async fn claim_macos_run(
    maa_state: &Arc<MaaState>,
    instance_id: &str,
    reset_state: bool,
    tasks_mapped: bool,
) -> Result<Option<MacRunClaim>, String> {
    static NEXT_TOKEN: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(1);
    let token = NEXT_TOKEN.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    let deadline = Instant::now() + Duration::from_secs(3);
    loop {
        let claimed = {
            let mut instances = maa_state.instances.lock().map_err(|e| e.to_string())?;
            let Some(instance) = instances.get_mut(instance_id) else {
                return Ok(None);
            };
            if !matches!(
                instance.controller_config,
                Some(super::types::ControllerConfig::MacOS { .. })
            ) {
                return Ok(None);
            }
            if !reset_state {
                return Err(super::maa_core::MACOS_APPEND_REFUSED.to_string());
            }
            // 这一轮的结束按任务列表中的条目判定；没有对应条目的任务结束后无法收尾。
            if !tasks_mapped {
                return Err("Mac 测试预览只能从任务列表启动任务。".to_string());
            }
            let active = instance.task_run_state.overall_status.as_deref() == Some("Running");
            super::mac_connection::claim_start(
                &mut instance.macos_run,
                active,
                token,
                &mut instance.tasker_serial,
            )
        };
        if claimed {
            return Ok(Some(MacRunClaim {
                state: Arc::clone(maa_state),
                instance_id: instance_id.to_string(),
                token,
            }));
        }
        if Instant::now() >= deadline {
            return Err("上一轮任务仍在运行或收尾，请稍后再开始。".to_string());
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
}

/// 启动任务（支持多个 Agent）— Tauri invoke 入口，委托给 start_tasks_impl
#[tauri::command]
pub async fn maa_start_tasks(
    app: tauri::AppHandle,
    state: State<'_, Arc<MaaState>>,
    instance_id: String,
    tasks: Vec<TaskConfig>,
    agent_configs: Option<Vec<AgentConfig>>,
    cwd: String,
    tcp_compat_mode: bool,
    pi_envs: Option<HashMap<String, String>>,
    reset_state: Option<bool>,
    controller_info: Option<ControllerInfo>,
) -> Result<Vec<i64>, String> {
    start_tasks_impl(
        app,
        &state,
        instance_id,
        tasks,
        agent_configs,
        cwd,
        tcp_compat_mode,
        pi_envs,
        reset_state.unwrap_or(true),
        controller_info,
    )
    .await
}

/// 停止所有 Agent 的核心实现（Tauri invoke 和 HTTP handler 共享）
/// 返回是否真的停止了（Mac 这一轮仍在进行、它的 agent 留给它自己时返回 false，调用方也不应释放连接）。
pub fn stop_agent_impl(maa_state: &Arc<MaaState>, instance_id: &str) -> Result<bool, String> {
    info!("stop_agent_impl called for instance: {}", instance_id);

    let (clients, children) = {
        let mut instances = maa_state.instances.lock().map_err(|e| e.to_string())?;
        let instance = instances.get_mut(instance_id).ok_or("Instance not found")?;
        // Mac：这一轮仍在启动、运行或收尾时，它的 agent 由它自己收尾；按实例停止 agent 的请求
        // （例如另一个被拒绝的启动的回滚，或停止后晚到的清理）不能取走它们。
        if matches!(
            instance.controller_config,
            Some(super::types::ControllerConfig::MacOS { .. })
        ) && super::mac_connection::run_busy(
            instance.macos_run,
            instance.task_run_state.overall_status.as_deref() == Some("Running"),
        ) {
            info!("[stop_agent] Mac run in progress on instance {}, leaving its agents to it", instance_id);
            return Ok(false);
        }
        instance.start_generation = instance.start_generation.wrapping_add(1);

        (
            std::mem::take(&mut instance.agent_clients),
            std::mem::take(&mut instance.agent_children),
        )
    };

    stop_taken_agents(clients, children);
    Ok(true)
}

/// 断开一个 agent client。macOS 上先把超时改为 5 秒：关闭请求默认无限等待，已握手但不再回应的
/// agent 会让收尾一直卡住（这一轮停在收尾中，之后无法再开始）。断开失败时，随后丢弃 client
/// 仍会在本地清除它注册的动作。
pub(crate) fn disconnect_agent(client: &AgentClient) {
    #[cfg(target_os = "macos")]
    if let Err(e) = client.set_timeout(5_000) {
        warn!("[stop_agent] Failed to bound the agent disconnect: {}", e);
    }
    let _ = client.disconnect();
}

/// 停止已经从实例上取走的 agent：同步断开 client，后台等待子进程退出（超时强杀）。
pub(crate) fn stop_taken_agents(clients: Vec<AgentClient>, children: Vec<std::process::Child>) {
    if clients.is_empty() && children.is_empty() {
        debug!("[stop_agent] No agents to stop");
        return;
    }

    info!(
        "[stop_agent] Stopping {} agent client(s) and {} child process(es)...",
        clients.len(),
        children.len()
    );

    // 同步断开所有 client：确保 custom 反注册在返回前完成，
    // 避免旧 agent 的反注册晚于新 agent 的注册，误删新 agent 的同名 custom 条目。
    for client in &clients {
        disconnect_agent(client);
    }
    drop(clients); // Drop 同步执行 clear_custom_registration()

    // 子进程收尾放到后台：等待自然退出，超时强杀兜底，避免进程泄漏。
    thread::spawn(move || {
        for (i, mut child) in children.into_iter().enumerate() {
            debug!("Waiting for agent process #{} to exit...", i);

            let start = std::time::Instant::now();
            let timeout = std::time::Duration::from_secs(5);
            let mut exited = false;

            while start.elapsed() < timeout {
                match child.try_wait() {
                    Ok(Some(_)) => {
                        exited = true;
                        break;
                    }
                    Ok(None) => {
                        thread::sleep(std::time::Duration::from_millis(100));
                    }
                    Err(e) => {
                        error!("Error waiting for agent #{}: {}", i, e);
                        break;
                    }
                }
            }

            if !exited {
                warn!("Agent process #{} did not exit in time, killing it...", i);
                let _ = child.kill();
                let _ = child.wait();
            } else {
                info!("Background: Agent #{} child process exited", i);
            }
        }
    });
}

/// 停止所有 Agent 并断开连接 — Tauri invoke 入口，委托给 stop_agent_impl
#[tauri::command]
pub fn maa_stop_agent(
    app: tauri::AppHandle,
    state: State<'_, Arc<MaaState>>,
    instance_id: String,
) -> Result<(), String> {
    if stop_agent_impl(&state, &instance_id)? {
        super::maa_core::schedule_release_idle_desktop_controller(&app, &state, &instance_id);
    }
    Ok(())
}
