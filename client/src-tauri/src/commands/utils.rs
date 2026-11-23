//! 辅助函数
//!
//! 提供路径处理和其他通用工具函数

use super::types::{MaaCallbackEvent, MaaState, StateChangedEvent};
use crate::ws_broadcast::{WsBroadcast, WsEvent};
use std::path::PathBuf;
use std::sync::Arc;
use tauri::{AppHandle, Emitter, Manager};

fn details_have_focus(parsed: &serde_json::Value, message: &str) -> bool {
    match parsed.get("focus") {
        Some(serde_json::Value::Object(map)) => map.get(message).is_some_and(|v| !v.is_null()),
        Some(v) if !v.is_null() => true,
        _ => false,
    }
}

/// 过滤前端与 WebSocket 均未消费的高频节点与非连接控制器动作回调，降低 IPC 负载
fn should_emit_callback_to_clients(message: &str, details: &str) -> bool {
    if message.starts_with("Node.") {
        if !details.contains("\"focus\"") {
            return false;
        }
        return serde_json::from_str::<serde_json::Value>(details)
            .map(|v| details_have_focus(&v, message))
            .unwrap_or(false);
    }

    if message.starts_with("Controller.Action.") {
        let Ok(parsed) = serde_json::from_str::<serde_json::Value>(details) else {
            return true;
        };
        if details_have_focus(&parsed, message) {
            return true;
        }
        return match parsed.get("action").and_then(|v| v.as_str()) {
            Some(action) => action.eq_ignore_ascii_case("connect"),
            None => true,
        };
    }

    true
}

/// 发送回调事件到前端（Tauri WebView + WebSocket 浏览器客户端）
pub fn emit_callback_event<S: Into<String>>(app: &AppHandle, message: S, details: S) {
    let message = message.into();
    let details = details.into();

    if !should_emit_callback_to_clients(&message, &details) {
        return;
    }

    // 广播到所有 WebSocket 客户端
    if let Some(ws) = app.try_state::<Arc<WsBroadcast>>() {
        ws.send(WsEvent::MaaCallback {
            message: message.clone(),
            details: details.clone(),
        });
    }

    // 发送到 Tauri WebView
    let event = MaaCallbackEvent { message, details };
    if let Err(e) = app.emit("maa-callback", event) {
        log::error!("Failed to emit maa-callback: {}", e);
    }
}

/// 发送实例状态变更事件（双通道：WS 浏览器客户端 + Tauri WebView）
///
/// Tauri 端和 WebUI 端都会收到此事件，用于刷新 `isRunning`、连接状态等运行时信息。
pub fn emit_state_changed(app: &AppHandle, instance_id: &str, kind: &str) {
    // 广播到所有 WebSocket 客户端
    if let Some(ws) = app.try_state::<Arc<WsBroadcast>>() {
        ws.send(WsEvent::StateChanged {
            instance_id: instance_id.to_string(),
            kind: kind.to_string(),
        });
    }

    // 发送到 Tauri WebView
    let event = StateChangedEvent {
        instance_id: instance_id.to_string(),
        kind: kind.to_string(),
    };
    if let Err(e) = app.emit("state-changed", event) {
        log::error!("Failed to emit state-changed: {}", e);
    }
}

/// 处理 MaaFramework 任务回调，在 Rust 侧更新 TaskRunState（单一真相来源）
///
/// 应在 tasker sink 中调用，在 `emit_callback_event` 之前处理任务状态变更。
/// 负责跟踪 Tasker.Task.Starting / Succeeded / Failed，并在所有任务完成后
/// 更新 `overall_status` 并发射 `tasks-completed` 事件。
///
/// `tasker_serial` 是发出回调的 Tasker 创建时的序号。Mac 连接上，只有当前 Tasker、且属于当前这一轮
/// 的回调能改动实例（`mac_connection::callback_is_current`）；已结束或已被换掉的运行的迟到回调一律
/// 忽略，不会结束下一轮或取走它的 agent。Windows 连接不做这些判断，行为不变。
pub fn handle_task_callback(
    maa_state: &Arc<MaaState>,
    app: &AppHandle,
    instance_id: &str,
    tasker_serial: u64,
    message: &str,
    details: &str,
) {
    let is_started = message == "Tasker.Task.Starting";
    let is_succeeded = message == "Tasker.Task.Succeeded";
    let is_failed = message == "Tasker.Task.Failed";

    if !is_started && !is_succeeded && !is_failed {
        return;
    }

    // 解析 task_id 与内部停止伪任务标识（MaaTaskerPostStop）
    let parsed_details = match serde_json::from_str::<serde_json::Value>(details) {
        Ok(v) => v,
        Err(_) => return,
    };
    let task_id: i64 = match parsed_details.get("task_id").and_then(|id| id.as_i64()) {
        Some(id) => id,
        None => return,
    };
    if parsed_details.get("entry").and_then(|e| e.as_str()) == Some("MaaTaskerPostStop") {
        // MaaFramework 调用 post_stop 时会派发内部伪任务 MaaTaskerPostStop，并直接清空后续排队任务而不逐个触发 Task.Failed。
        // 1. 忽略其 Starting，避免递增 EVIDENCE_EPOCH 打断刚失败任务的截图与诊断日志冻结，也避免产生空名 Span；
        // 2. 在非用户手动取消的内部 post_stop 收尾时，将队列中尚未执行的剩余任务一并收尾，结束整轮状态与遥测。
        if is_started {
            return;
        }
        let aborted_remaining = {
            let mut instances = match maa_state.instances.lock() {
                Ok(g) => g,
                Err(_) => return,
            };
            let instance = match instances.get_mut(instance_id) {
                Some(i) => i,
                None => return,
            };
            if instance.stop_in_progress
                || instance.task_run_state.overall_status.as_deref() != Some("Running")
                || is_stale_macos_callback(instance, tasker_serial)
            {
                (false, None)
            } else {
                let state = &mut instance.task_run_state;
                for status in state.statuses.values_mut() {
                    if status == "pending" || status == "running" {
                        *status = "failed".to_string();
                    }
                }
                state.overall_status = Some("Failed".to_string());
                instance.task_ids.clear();
                // Mac：在判定结束的同一个临界区里取走这次运行的 agent 和代数。
                (true, super::maa_core::finish_macos_run(instance))
            }
        };
        let (aborted_remaining, finished_macos) = aborted_remaining;
        if aborted_remaining {
            super::telemetry::on_run_finished(instance_id);
            if let Some(finished) = finished_macos {
                super::maa_core::complete_macos_run(app, maa_state, instance_id, finished);
                return;
            }
            emit_state_changed(app, instance_id, "task-progress");
            emit_state_changed(app, instance_id, "tasks-completed");
            if let Err(e) = super::maa_agent::stop_agent_impl(maa_state, instance_id) {
                log::warn!(
                    "[handle_task_callback] Failed to stop agents after post_stop abort: {}",
                    e
                );
            }
            super::maa_core::schedule_release_idle_desktop_controller(app, maa_state, instance_id);
        }
        return;
    }

    let (all_done, finished_macos) = {
        let mut instances = match maa_state.instances.lock() {
            Ok(g) => g,
            Err(_) => return,
        };
        let instance = match instances.get_mut(instance_id) {
            Some(i) => i,
            None => return,
        };
        // Mac：已结束或已被换掉的运行的回调，或不属于当前这一轮的结束回调，不改动任何状态。
        if is_stale_macos_callback(instance, tasker_serial)
            || (is_macos_connection(instance)
                && !is_started
                && !instance.task_run_state.pending_task_ids.contains(&task_id))
        {
            return;
        }
        let state = &mut instance.task_run_state;

        if is_started {
            // 任务开始：定位该任务在队列中的索引，更新当前索引和状态
            if let Some(idx) = state.pending_task_ids.iter().position(|&id| id == task_id) {
                state.current_task_index = idx;
            }
            if let Some(selected_id) = state.mappings.get(&task_id).cloned() {
                state.statuses.insert(selected_id, "running".to_string());
            }
            (false, None) // 未完成
        } else {
            // 任务成功或失败
            let status_str = if is_succeeded { "succeeded" } else { "failed" };
            if let Some(selected_id) = state.mappings.get(&task_id).cloned() {
                state.statuses.insert(selected_id, status_str.to_string());
            }

            // 检查所有已入队任务是否均已完成
            let all_completed = state.pending_task_ids.iter().all(|id| {
                state
                    .mappings
                    .get(id)
                    .and_then(|sel_id| state.statuses.get(sel_id))
                    .map(|s| s == "succeeded" || s == "failed")
                    .unwrap_or(false)
            });

            // 只有属于当前这一轮的回调才能结束这一轮（Mac 收尾据此取走 agent）。
            let belongs_to_run = state.pending_task_ids.contains(&task_id);
            if all_completed {
                let has_failed = state.statuses.values().any(|s| s == "failed");
                state.overall_status =
                    Some(if has_failed { "Failed" } else { "Succeeded" }.to_string());
                instance.task_ids.clear();
            }
            // Mac：在判定结束的同一个临界区里取走这次运行的 agent 和代数。
            let finished = if all_completed && belongs_to_run {
                super::maa_core::finish_macos_run(instance)
            } else {
                None
            };

            (all_completed, finished)
        }
    }; // 锁在此处释放

    // 遥测埋点与任务运行统计（锁外调用）
    if is_started {
        super::task_stats::on_task_start(instance_id, task_id);
        super::telemetry::on_task_start(instance_id, task_id);
    } else {
        super::task_stats::on_task_finished(app, instance_id, task_id, is_succeeded);
        super::telemetry::on_task_finished(instance_id, task_id, is_succeeded);
        let _ = app.emit("adventure-report-changed", ());
    }
    if all_done {
        super::telemetry::on_run_finished(instance_id);
    }

    // 通知前端刷新状态
    // Mac：先断开这次运行的 agent（反注册其动作），再通知结束，之后的清理只针对这次运行。
    if let Some(finished) = finished_macos {
        super::maa_core::complete_macos_run(app, maa_state, instance_id, finished);
        return;
    }
    emit_state_changed(app, instance_id, "task-progress");
    if all_done {
        emit_state_changed(app, instance_id, "tasks-completed");

        // 实例内全部任务结束后清理旧 agent，避免 agent 进程跨轮累积
        if let Err(e) = super::maa_agent::stop_agent_impl(maa_state, instance_id) {
            log::warn!(
                "[handle_task_callback] Failed to stop agents after tasks completed: {}",
                e
            );
        }
        super::maa_core::schedule_release_idle_desktop_controller(app, maa_state, instance_id);
    }
}

fn is_macos_connection(instance: &super::types::InstanceRuntime) -> bool {
    matches!(
        instance.controller_config,
        Some(super::types::ControllerConfig::MacOS { .. })
    )
}

/// Mac 连接上，回调来自已结束或已被换掉的 Tasker。
fn is_stale_macos_callback(instance: &super::types::InstanceRuntime, tasker_serial: u64) -> bool {
    is_macos_connection(instance)
        && !super::mac_connection::callback_is_current(
            instance.tasker_serial,
            instance.tasker.is_some(),
            tasker_serial,
        )
}

/// 发送配置变更事件（双通道：WS 浏览器客户端 + Tauri WebView）
///
/// 各客户端收到后应重新拉取配置并 `importConfig`（需配合 `consumeSelfSave` 跳过自身触发）。
pub fn emit_config_changed(app: &AppHandle) {
    // 广播到所有 WebSocket 客户端
    if let Some(ws) = app.try_state::<Arc<WsBroadcast>>() {
        ws.send(WsEvent::ConfigChanged);
    }

    // 发送到 Tauri WebView
    if let Err(e) = app.emit("config-changed-external", ()) {
        log::error!("Failed to emit config-changed-external: {}", e);
    }
}

/// 把只读资源根目录和可写数据根目录传给 agent 及其 Python 子进程。
/// agent 从 `MAANIKKI_RESOURCE_ROOT` 读取 interface.json、resource/、tasks/、maafw/，
/// 向 `MAANIKKI_DATA_ROOT` 写入 config/、logs/、debug/、cache/、data_overlay/。
/// Windows 上两者都是程序目录（便携式部署），与 agent 自行推导的目录相同。
pub fn apply_agent_root_env(cmd: &mut std::process::Command) {
    match get_exe_directory() {
        Ok(dir) => {
            cmd.env("MAANIKKI_RESOURCE_ROOT", dir);
        }
        Err(e) => log::warn!("无法确定资源目录，agent 将自行推导: {}", e),
    }
    match get_app_data_dir() {
        Ok(dir) => {
            cmd.env("MAANIKKI_DATA_ROOT", dir);
        }
        Err(e) => log::warn!("无法确定数据目录，agent 将自行推导: {}", e),
    }
}

/// macOS 数据目录名，使用 MaaNikki 自己的目录，不与其他基于 MXU 的应用共用 `MXU`。
#[cfg(target_os = "macos")]
const MACOS_DATA_DIR_NAME: &str = "MaaNikki";

/// macOS 测试预览版允许运行的任务入口清单（与 agent 共用一份）。
#[cfg(target_os = "macos")]
const MACOS_PREVIEW_TASKS: &str = "agent/host/macos/preview_tasks.json";

/// 开发专用开关：逗号分隔的任务入口，只在开发构建的 macOS 客户端里额外放行（agent 侧同名变量）。
/// 发布构建既不读取，也会从 agent 的环境里去掉，已验证的白名单仍是 preview_tasks.json。
pub const MACOS_EXTRA_TASKS_ENV: &str = "MAANIKKI_MACOS_EXTRA_TASKS";

/// 开发开关的取值（逗号分隔，忽略空白和空项）是否包含该入口。
pub fn extra_tasks_allow(list: &str, entry: &str) -> bool {
    !entry.is_empty() && list.split(',').map(str::trim).any(|item| item == entry)
}

/// 判断清单 JSON（`{"entries": [...]}`）是否包含该任务入口；清单无法解析时报错。
pub fn preview_task_allowed(manifest_json: &str, entry: &str) -> Result<bool, String> {
    let manifest: serde_json::Value =
        serde_json::from_str(manifest_json).map_err(|e| format!("任务清单无法解析: {}", e))?;
    let entries = manifest
        .get("entries")
        .and_then(|v| v.as_array())
        .ok_or("任务清单缺少 entries")?;
    Ok(entries.iter().any(|item| item.as_str() == Some(entry)))
}

/// 把 agent 的运行计划（PI_MAANIKKI_RUN_PLAN 的 JSON）中的任务入口换成实际会执行的那些；
/// 无法解析时原样返回。
pub fn run_plan_with_entries(plan: &str, entries: &[&str]) -> String {
    match serde_json::from_str::<serde_json::Value>(plan) {
        Ok(mut value) if value.is_object() => {
            value["entries"] = serde_json::json!(entries);
            value.to_string()
        }
        _ => plan.to_string(),
    }
}

/// 当前系统不能运行该任务入口时返回原因（直接显示给用户）；能运行时返回 None。
///
/// Windows 上始终放行，行为不变。macOS 只放行 agent/host/macos/preview_tasks.json 中的入口，
/// 清单缺失或无法解析时一律拒绝。在下发任务之前检查，覆盖已保存的预设、定时运行、功能入口和
/// MXU 内置任务（例如 MXU_LAUNCH），这些都不一定经过 agent。
pub fn platform_refuses_task(entry: &str) -> Option<String> {
    #[cfg(target_os = "macos")]
    {
        // 开发构建（tauri dev）专用：在有人值守的实机测试中放行尚未验证的任务。发布构建忽略它，
        // 也不把它传给 agent（见 maa_agent.rs）。
        #[cfg(debug_assertions)]
        if let Ok(extra) = std::env::var(MACOS_EXTRA_TASKS_ENV) {
            if extra_tasks_allow(&extra, entry) {
                log::warn!("[dev] {} 放行未验证的 Mac 任务：{}", MACOS_EXTRA_TASKS_ENV, entry);
                return None;
            }
        }
        let allowed = get_exe_directory()
            .and_then(|root| {
                std::fs::read_to_string(root.join(MACOS_PREVIEW_TASKS)).map_err(|e| e.to_string())
            })
            .and_then(|json| preview_task_allowed(&json, entry));
        match allowed {
            Ok(true) => None,
            Ok(false) => Some(format!(
                "{} 暂不支持 Mac 测试预览版（只开放已验证的任务），已跳过。",
                entry
            )),
            Err(e) => Some(format!("无法读取 Mac 任务清单（{}），已跳过 {}。", e, entry)),
        }
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = entry;
        None
    }
}

/// 获取应用数据目录
/// - macOS: ~/Library/Application Support/MaaNikki/
/// - Windows/Linux: exe 所在目录（保持便携式部署）
pub fn get_app_data_dir() -> Result<PathBuf, String> {
    #[cfg(target_os = "macos")]
    {
        let home = std::env::var("HOME").map_err(|_| "无法获取 HOME 环境变量".to_string())?;
        let path = PathBuf::from(home)
            .join("Library")
            .join("Application Support")
            .join(MACOS_DATA_DIR_NAME);
        Ok(path)
    }

    #[cfg(not(target_os = "macos"))]
    {
        // Windows/Linux 保持便携式，使用 exe 所在目录
        get_exe_directory()
    }
}

/// 规范化路径：移除冗余的 `.`、处理 `..`、统一分隔符
/// 使用 Path::components() 解析，不需要路径实际存在
pub fn normalize_path(path: &str) -> PathBuf {
    use std::path::{Component, Path};

    let path = Path::new(path);
    let mut components = Vec::new();

    for component in path.components() {
        match component {
            // 跳过当前目录标记 "."
            Component::CurDir => {}
            // 处理父目录 ".."：如果栈顶是普通目录则弹出，否则保留
            Component::ParentDir => {
                if matches!(components.last(), Some(Component::Normal(_))) {
                    components.pop();
                } else {
                    components.push(component);
                }
            }
            // 保留其他组件（Prefix、RootDir、Normal）
            _ => components.push(component),
        }
    }

    // 重建路径
    components.into_iter().collect()
}

/// 获取日志目录（应用数据目录下的 debug 子目录）
pub fn get_logs_dir() -> PathBuf {
    get_app_data_dir()
        .unwrap_or_else(|_| {
            // 回退到 exe 目录
            let exe_path = std::env::current_exe().unwrap_or_default();
            exe_path
                .parent()
                .unwrap_or(std::path::Path::new("."))
                .to_path_buf()
        })
        .join("debug")
}

/// 获取 exe 所在目录路径（内部使用）
pub fn get_exe_directory() -> Result<PathBuf, String> {
    #[cfg(debug_assertions)]
    {
        // Lets a debug build run against a separate home, such as sample data for screenshots.
        if let Some(home) = std::env::var_os("MAANIKKI_DEV_HOME") {
            return Ok(PathBuf::from(home));
        }
        Ok(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../.."))
    }
    #[cfg(not(debug_assertions))]
    {
        let exe_path = std::env::current_exe().map_err(|e| format!("获取 exe 路径失败: {}", e))?;
        exe_path
            .parent()
            .map(|p| p.to_path_buf())
            .ok_or_else(|| "无法获取 exe 所在目录".to_string())
    }
}

/// 获取可执行文件所在目录下的 maafw 子目录
pub fn get_maafw_dir() -> Result<PathBuf, String> {
    Ok(get_exe_directory()?.join("maafw"))
}

/// 构建 User-Agent 字符串
pub fn build_user_agent() -> String {
    let version = env!("CARGO_PKG_VERSION");
    let os = std::env::consts::OS;
    let arch = std::env::consts::ARCH;
    let tauri_version = tauri::VERSION;
    format!("MXU/{} ({}; {}) Tauri/{}", version, os, arch, tauri_version)
}

/// 原子写入文件：先写入同目录 `.tmp` 临时文件并落盘 (`sync_all`)，再原子重命名覆盖目标文件。
pub fn write_atomic(path: &std::path::Path, bytes: impl AsRef<[u8]>) -> Result<(), std::io::Error> {
    use std::io::Write;

    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent)?;
        }
    }
    let tmp_name = match path.file_name().and_then(|n| n.to_str()) {
        Some(name) => format!("{name}.tmp"),
        None => "write.tmp".to_string(),
    };
    let tmp_path = path.with_file_name(tmp_name);
    let write_res = (|| -> Result<(), std::io::Error> {
        let mut file = std::fs::File::create(&tmp_path)?;
        file.write_all(bytes.as_ref())?;
        file.sync_all()?;
        drop(file);
        std::fs::rename(&tmp_path, path)?;
        Ok(())
    })();
    if write_res.is_err() {
        let _ = std::fs::remove_file(&tmp_path);
    }
    write_res
}

/// 构建启动程序的 Command
///
/// - 子进程的 stdout/stderr 设为 null，避免继承父进程的标准流。
/// - 当 `use_cmd` 为 true 时（仅 Windows），通过 `cmd /c` 启动并设置
///   `CREATE_NO_WINDOW` 标志隐藏控制台窗口。
///
/// 注意：曾经使用 `CREATE_BREAKAWAY_FROM_JOB` 使子进程脱离父进程 Job 对象，
/// 但 Windows 计划任务创建的 Job 默认不允许 breakaway（未设置
/// `JOB_OBJECT_LIMIT_BREAKAWAY_OK`），导致 `CreateProcessW` 返回
/// `ERROR_ACCESS_DENIED (os error 5)`。
///
/// 手动启动 MXU 时，MXU 不在任何 Job Object 中，该标志没有 Job 可脱离，
/// 被 Windows 静默忽略，因此不会报错——但这只是巧合，不代表该标志真正生效。
/// 计划任务启动时，MXU 被关在限制性 Job 中，该标志才真正触发错误。
///
/// `cmd /c` 本身已改变了 PPID 链，进程树隔离目的已达成，因此移除该标志。
pub fn build_launch_command(
    program: &str,
    args: &[String],
    use_cmd: bool,
) -> std::process::Command {
    use std::process::Stdio;

    let mut cmd = if cfg!(target_os = "windows") && use_cmd {
        let mut c = std::process::Command::new("cmd.exe");
        c.arg("/c").arg(program);
        if !args.is_empty() {
            c.args(args);
        }
        c
    } else {
        let mut c = std::process::Command::new(program);
        if !args.is_empty() {
            c.args(args);
        }
        c
    };

    // 不继承父进程的标准流
    cmd.stdout(Stdio::null()).stderr(Stdio::null());

    #[cfg(target_os = "windows")]
    if use_cmd {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }

    cmd
}

#[cfg(test)]
mod preview_task_tests {
    use super::{extra_tasks_allow, preview_task_allowed};

    #[test]
    fn the_shared_manifest_parses_and_allows_only_listed_entries() {
        let manifest = include_str!("../../../../agent/host/macos/preview_tasks.json");
        assert_eq!(preview_task_allowed(manifest, "MaaNikki_Mailbox"), Ok(true));
        for entry in ["MXU_LAUNCH", "MaaNikki_StartGame", "MaaNikki_Fishing", ""] {
            assert_eq!(preview_task_allowed(manifest, entry), Ok(false), "{}", entry);
        }
    }

    #[test]
    fn the_run_plan_lists_only_the_tasks_that_will_run() {
        use super::run_plan_with_entries;
        let plan = r#"{"schema_version":1,"id":"x","instance":"i","entries":["MaaNikki_Mailbox","MaaNikki_RealmChallenge"]}"#;
        let rewritten: serde_json::Value =
            serde_json::from_str(&run_plan_with_entries(plan, &["MaaNikki_Mailbox"])).unwrap();
        assert_eq!(rewritten["entries"], serde_json::json!(["MaaNikki_Mailbox"]));
        assert_eq!(rewritten["id"], "x");
        assert_eq!(run_plan_with_entries("not json", &["a"]), "not json");
    }

    #[test]
    fn the_dev_switch_lists_exact_entries() {
        assert!(extra_tasks_allow("MaaNikki_Dig, MaaNikki_MonthlyPass", "MaaNikki_MonthlyPass"));
        assert!(!extra_tasks_allow("MaaNikki_Dig", "MaaNikki_Di"));
        assert!(!extra_tasks_allow("", ""));
        assert!(!extra_tasks_allow(" , ", ""));
    }

    #[test]
    fn a_broken_manifest_is_an_error() {
        assert!(preview_task_allowed("{}", "MaaNikki_Mailbox").is_err());
        assert!(preview_task_allowed("not json", "MaaNikki_Mailbox").is_err());
    }
}
