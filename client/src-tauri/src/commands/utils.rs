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

/// 任务清单的 JSON：与 agent 一样拒绝重复的键（serde_json 默认保留最后一个，悄悄覆盖）。
/// NaN/Infinity 本来就不是合法的 JSON，serde_json 已经拒绝。
fn load_task_list(text: &str) -> Result<serde_json::Value, String> {
    struct Strict(serde_json::Value);
    struct StrictVisitor;
    impl<'de> serde::de::Visitor<'de> for StrictVisitor {
        type Value = serde_json::Value;
        fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
            f.write_str("JSON")
        }
        fn visit_bool<E>(self, v: bool) -> Result<Self::Value, E> {
            Ok(v.into())
        }
        fn visit_i64<E>(self, v: i64) -> Result<Self::Value, E> {
            Ok(v.into())
        }
        fn visit_u64<E>(self, v: u64) -> Result<Self::Value, E> {
            Ok(v.into())
        }
        fn visit_f64<E>(self, v: f64) -> Result<Self::Value, E> {
            Ok(serde_json::Number::from_f64(v).map(serde_json::Value::Number).unwrap_or(serde_json::Value::Null))
        }
        fn visit_str<E>(self, v: &str) -> Result<Self::Value, E> {
            Ok(v.into())
        }
        fn visit_unit<E>(self) -> Result<Self::Value, E> {
            Ok(serde_json::Value::Null)
        }
        fn visit_seq<A: serde::de::SeqAccess<'de>>(self, mut seq: A) -> Result<Self::Value, A::Error> {
            let mut out = Vec::new();
            while let Some(Strict(item)) = seq.next_element()? {
                out.push(item);
            }
            Ok(serde_json::Value::Array(out))
        }
        fn visit_map<A: serde::de::MapAccess<'de>>(self, mut map: A) -> Result<Self::Value, A::Error> {
            let mut out = serde_json::Map::new();
            while let Some((key, Strict(value))) = map.next_entry::<String, Strict>()? {
                if out.contains_key(&key) {
                    return Err(serde::de::Error::custom(format!("重复的键 {}", key)));
                }
                out.insert(key, value);
            }
            Ok(serde_json::Value::Object(out))
        }
    }
    impl<'de> serde::Deserialize<'de> for Strict {
        fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
            d.deserialize_any(StrictVisitor).map(Strict)
        }
    }
    serde_json::from_str::<Strict>(text).map(|Strict(value)| value).map_err(|e| format!("任务清单无法解析: {}", e))
}

/// macOS 任务清单的三个档位：已验证（entries）、试用（experimental）、不开放（excluded，入口到原因）。
#[derive(Debug, Clone, PartialEq, Eq, Default, serde::Serialize)]
pub struct TaskTiers {
    pub verified: Vec<String>,
    pub experimental: Vec<String>,
    pub excluded: std::collections::BTreeMap<String, String>,
}

/// 严格解析任务清单（与 agent/host/macos/__init__.py 的 parse_task_tiers 规则相同）：entries 必需，
/// experimental、excluded 可选（旧清单等于没有试用任务）；类型不对、空名字、重复、同一入口出现在多个档位都报错。
pub fn parse_task_tiers(manifest_json: &str) -> Result<TaskTiers, String> {
    let manifest: serde_json::Value =
        load_task_list(manifest_json)?;
    let object = manifest.as_object().ok_or("任务清单不是对象")?;
    let names = |key: &str, required: bool| -> Result<Vec<String>, String> {
        let value = match object.get(key) {
            Some(value) => value,
            None if required => return Err(format!("任务清单缺少 {}", key)),
            None => return Ok(Vec::new()),
        };
        let items = value.as_array().ok_or(format!("任务清单的 {} 无效", key))?;
        let mut out: Vec<String> = Vec::new();
        for item in items {
            let name = item.as_str().filter(|s| !s.is_empty()).ok_or(format!("任务清单的 {} 无效", key))?;
            if out.iter().any(|seen| seen == name) {
                return Err(format!("任务清单的 {} 有重复", key));
            }
            out.push(name.to_string());
        }
        Ok(out)
    };
    let verified = names("entries", true)?;
    let experimental = names("experimental", false)?;
    let mut excluded = std::collections::BTreeMap::new();
    if let Some(value) = object.get("excluded") {
        let map = value.as_object().ok_or("任务清单的 excluded 无效")?;
        for (entry, reason) in map {
            let reason = reason.as_str().filter(|s| !s.is_empty()).ok_or("任务清单的 excluded 无效")?;
            if entry.is_empty() {
                return Err("任务清单的 excluded 无效".to_string());
            }
            excluded.insert(entry.clone(), reason.to_string());
        }
    }
    let overlaps = verified.iter().any(|e| experimental.contains(e))
        || verified.iter().chain(experimental.iter()).any(|e| excluded.contains_key(e));
    if overlaps {
        return Err("任务清单里同一入口出现在多个档位".to_string());
    }
    Ok(TaskTiers { verified, experimental, excluded })
}

/// 清单里给用户看的任务说明（notes，入口到说明），可选；类型不对、空名字或空说明时报错。
/// 只用于界面标记，不影响哪些任务能运行（运行与否只看 parse_task_tiers）。
pub fn parse_task_notes(manifest_json: &str) -> Result<std::collections::BTreeMap<String, String>, String> {
    let manifest: serde_json::Value =
        load_task_list(manifest_json)?;
    let mut notes = std::collections::BTreeMap::new();
    if let Some(value) = manifest.get("notes") {
        let map = value.as_object().ok_or("任务清单的 notes 无效")?;
        for (entry, note) in map {
            let note = note.as_str().filter(|s| !s.is_empty()).ok_or("任务清单的 notes 无效")?;
            if entry.is_empty() {
                return Err("任务清单的 notes 无效".to_string());
            }
            notes.insert(entry.clone(), note.to_string());
        }
    }
    Ok(notes)
}

/// 界面用来标记任务的 Mac 任务档位（桌面命令 get_macos_task_tiers 与 GET /api/macos/task-tiers 返回）。
/// 四个字段全空表示没有档位、不做任何标记（Windows/Linux 始终如此）；error 存在时表示 Mac 上清单读不出来，
/// 界面显示它，此时档位为空，任务仍由 platform_refuses_task 在下发前拒绝。
#[derive(Debug, Clone, PartialEq, Eq, Default, serde::Serialize)]
pub struct TaskTiersView {
    pub verified: Vec<String>,
    pub experimental: Vec<String>,
    pub excluded: std::collections::BTreeMap<String, String>,
    pub notes: std::collections::BTreeMap<String, String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

impl TaskTiersView {
    fn unreadable(reason: &str) -> Self {
        TaskTiersView { error: Some(format!("无法读取 Mac 任务清单（{}）", reason)), ..Default::default() }
    }
}

/// 从清单内容得到界面用的档位；清单或其中的 notes 无效时返回只带 error 的结果。
pub fn task_tiers_view(manifest_json: &str) -> TaskTiersView {
    match parse_task_tiers(manifest_json).and_then(|tiers| Ok((tiers, parse_task_notes(manifest_json)?))) {
        Ok((tiers, notes)) => TaskTiersView {
            verified: tiers.verified,
            experimental: tiers.experimental,
            excluded: tiers.excluded,
            notes,
            error: None,
        },
        Err(e) => TaskTiersView::unreadable(&e),
    }
}

/// 当前后端系统的任务档位：macOS 读取 agent/host/macos/preview_tasks.json，其他系统返回全空（不做标记）。
pub fn macos_task_tiers() -> TaskTiersView {
    #[cfg(target_os = "macos")]
    {
        match get_exe_directory().and_then(|root| {
            std::fs::read_to_string(root.join(MACOS_PREVIEW_TASKS)).map_err(|e| e.to_string())
        }) {
            Ok(json) => task_tiers_view(&json),
            Err(e) => TaskTiersView::unreadable(&e),
        }
    }
    #[cfg(not(target_os = "macos"))]
    {
        TaskTiersView::default()
    }
}

/// 该入口在 macOS 上是否可以运行（已验证或试用）；清单无效时报错。
pub fn preview_task_allowed(manifest_json: &str, entry: &str) -> Result<bool, String> {
    let tiers = parse_task_tiers(manifest_json)?;
    Ok(tiers.verified.iter().chain(tiers.experimental.iter()).any(|e| e == entry))
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
        let tiers = get_exe_directory()
            .and_then(|root| {
                std::fs::read_to_string(root.join(MACOS_PREVIEW_TASKS)).map_err(|e| e.to_string())
            })
            .and_then(|json| parse_task_tiers(&json));
        match tiers {
            Ok(tiers) if tiers.verified.iter().chain(tiers.experimental.iter()).any(|e| e == entry) => None,
            Ok(tiers) => Some(match tiers.excluded.get(entry) {
                Some(reason) => format!("{} 不在 Mac 测试预览版开放：{}已跳过。", entry, reason),
                None => format!("{} 暂不支持 Mac 测试预览版，已跳过。", entry),
            }),
            Err(e) => Some(format!("无法读取 Mac 任务清单（{}），已跳过 {}。", e, entry)),
        }
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = entry;
        None
    }
}

/// 本次运行的游戏目标：由控制器类型（interface 中 controller.type）决定，与 agent 的 host.game_backend 规则相同。
/// 只有 Win32、MacOS、Adb 三种类型决定目标，其他类型（Gamepad、PlayCover 等）按当前系统的原规则处理。
/// Windows 上只会出现 Win32 控制器，macOS 上只会出现 MacOS 控制器，这两种情况下结果与原来完全相同。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum GameTarget {
    Win32,
    MacOS,
    Adb,
    /// 无效的控制器定义（PI_CONTROLLER 不是带非空 type 的 JSON 对象）。
    Malformed,
}

impl GameTarget {
    fn from_kind(kind: &ControllerKind) -> Option<GameTarget> {
        match kind {
            ControllerKind::Malformed => Some(GameTarget::Malformed),
            ControllerKind::Type(t) => match t.as_str() {
                "Win32" => Some(GameTarget::Win32),
                "MacOS" => Some(GameTarget::MacOS),
                "Adb" => Some(GameTarget::Adb),
                _ => None,
            },
        }
    }

    /// 当前系统自己的桌面目标。
    fn host() -> Option<GameTarget> {
        if cfg!(target_os = "windows") {
            Some(GameTarget::Win32)
        } else if cfg!(target_os = "macos") {
            Some(GameTarget::MacOS)
        } else {
            None
        }
    }
}

/// 控制器的类型：提交的控制器定义里的 type，或已连接控制器的种类。比较是否一致时保留原样，
/// 包括不决定目标的类型（Gamepad 等），这样「提交 Win32、连接 Gamepad」也算不一致。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ControllerKind {
    Malformed,
    Type(String),
}

impl ControllerKind {
    /// 已连接的控制器的种类，名称与 interface 的 controller.type 相同。
    pub fn of_connection(config: &super::types::ControllerConfig) -> ControllerKind {
        use super::types::ControllerConfig;
        ControllerKind::Type(
            match config {
                ControllerConfig::Adb { .. } => "Adb",
                ControllerConfig::Win32 { .. } => "Win32",
                ControllerConfig::MacOS { .. } => "MacOS",
                ControllerConfig::WlRoots { .. } => "WlRoots",
                ControllerConfig::Gamepad { .. } => "Gamepad",
                ControllerConfig::PlayCover { .. } => "PlayCover",
                ControllerConfig::Linux { .. } => "Linux",
            }
            .to_string(),
        )
    }

    pub fn of_instance(config: Option<&super::types::ControllerConfig>) -> Option<ControllerKind> {
        config.map(ControllerKind::of_connection)
    }
}

/// 提交的控制器的类型：优先用传给 agent 的 PI_CONTROLLER（agent 按它判断），其次用前端传来的类型。
/// PI_CONTROLLER 存在但不是带非空 type 的 JSON 对象时为 Malformed（拒绝运行，与 agent 相同）。
pub fn submitted_controller_kind(
    pi_envs: Option<&std::collections::HashMap<String, String>>,
    controller_type: Option<&str>,
) -> Option<ControllerKind> {
    if let Some(raw) = pi_envs.and_then(|envs| envs.get("PI_CONTROLLER")) {
        let kind = serde_json::from_str::<serde_json::Value>(raw)
            .ok()
            .and_then(|value| value.get("type").and_then(|t| t.as_str()).map(str::to_owned));
        return Some(match kind {
            Some(kind) if !kind.is_empty() => ControllerKind::Type(kind),
            _ => ControllerKind::Malformed,
        });
    }
    controller_type.filter(|t| !t.is_empty()).map(|t| ControllerKind::Type(t.to_string()))
}

/// 控制器本身就不能运行任务时的原因（与任务入口无关）；能运行时返回 None。
fn controller_refusal(host: Option<GameTarget>, target: Option<GameTarget>) -> Option<String> {
    match target {
        None => None,
        Some(t) if Some(t) == host && matches!(t, GameTarget::Win32 | GameTarget::MacOS) => None,
        Some(GameTarget::Adb) => Some("安卓模拟器支持还在开发中，暂不运行任务。".to_string()),
        Some(GameTarget::Malformed) => Some("所选的控制器定义无效，无法运行任务，请重新选择控制器。".to_string()),
        Some(t) => Some(format!("所选的控制器（{:?}）不能在当前系统上运行任务。", t)),
    }
}

/// 一批任务的准入：以已连接的控制器为准，提交的控制器与它的类型不一致时整批拒绝；控制器本身不能运行时
/// 整批拒绝（即使没有任务）；否则逐个入口按当前系统的规则判断。
fn admit_batch_on(
    host: Option<GameTarget>,
    connected: Option<&ControllerKind>,
    submitted: Option<&ControllerKind>,
    entries: &[&str],
    host_rules: impl Fn(&str) -> Option<String>,
) -> Result<Vec<Option<String>>, String> {
    if let (Some(connected), Some(submitted)) = (connected, submitted) {
        if connected != submitted {
            return Err("所选的控制器与当前连接的不一致，请重新连接后再运行。".to_string());
        }
    }
    if let Some(reason) = controller_refusal(host, connected.or(submitted).and_then(GameTarget::from_kind)) {
        return Err(reason);
    }
    Ok(entries.iter().map(|entry| host_rules(entry)).collect())
}

/// start_tasks 的准入（手动开始和定时运行都经过这里）。Ok 里是每个入口的拒绝原因（None 表示可以运行）。
pub fn admit_tasks(
    connected: Option<&ControllerKind>,
    submitted: Option<&ControllerKind>,
    entries: &[&str],
) -> Result<Vec<Option<String>>, String> {
    admit_batch_on(GameTarget::host(), connected, submitted, entries, platform_refuses_task)
}

/// 运行中追加单个任务的准入：只看已连接的控制器。
pub fn target_refuses_task(connected: Option<&ControllerKind>, entry: &str) -> Option<String> {
    match admit_batch_on(GameTarget::host(), connected, None, &[entry], platform_refuses_task) {
        Ok(mut reasons) => reasons.pop().flatten(),
        Err(reason) => Some(format!("{}已跳过 {}。", reason, entry)),
    }
}

/// 准入之后、真正绑定控制器下发任务之前（在同一把锁里）再核对一次：连接的控制器种类变了（包括准入时
/// 还没有连接、之后才连上）就按新的连接重新准入，结果必须与原来相同，否则拒绝。这样「请求 Win32、
/// 第一次连接在准入后才完成」照常运行，而换成别的控制器（例如 Adb）会被拒绝。
pub const CONNECTION_CHANGED: &str = "开始之前当前连接的控制器发生了变化，请重新开始。";

pub fn readmit_on_connection(
    admitted: Option<&ControllerKind>,
    now: Option<&super::types::ControllerConfig>,
    submitted: Option<&ControllerKind>,
    entries: &[&str],
    refusals: &[Option<String>],
) -> Result<(), String> {
    readmit_on(GameTarget::host(), admitted, now, submitted, entries, refusals, platform_refuses_task)
}

fn readmit_on(
    host: Option<GameTarget>,
    admitted: Option<&ControllerKind>,
    now: Option<&super::types::ControllerConfig>,
    submitted: Option<&ControllerKind>,
    entries: &[&str],
    refusals: &[Option<String>],
    host_rules: impl Fn(&str) -> Option<String>,
) -> Result<(), String> {
    let now = ControllerKind::of_instance(now);
    if now.as_ref() == admitted {
        return Ok(());
    }
    match admit_batch_on(host, now.as_ref(), submitted, entries, host_rules) {
        Ok(again) if again == refusals => Ok(()),
        Ok(_) => Err(CONNECTION_CHANGED.to_string()),
        Err(reason) => Err(reason),
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
        let exe_dir = exe_path
            .parent()
            .map(|p| p.to_path_buf())
            .ok_or_else(|| "无法获取 exe 所在目录".to_string())?;
        // macOS 应用包：只读资源在 Contents/Resources，Contents/MacOS 只放可执行文件（签名要求）。
        // 先解析符号链接（经链接启动时 current_exe 可能是链接本身的路径）。
        #[cfg(target_os = "macos")]
        if let Some(resources) = std::fs::canonicalize(&exe_path)
            .ok()
            .and_then(|real| real.parent().and_then(macos_bundle_resources_dir))
        {
            return Ok(resources);
        }
        Ok(exe_dir)
    }
}

/// 可执行文件位于 `<名称>.app/Contents/MacOS/` 时，返回同一应用包的 `Contents/Resources`；
/// 否则（例如直接运行 target/release 下的程序）返回 None。只在 macOS 发布构建中使用。
#[cfg(any(target_os = "macos", test))]
fn macos_bundle_resources_dir(exe_dir: &std::path::Path) -> Option<PathBuf> {
    let contents = exe_dir.parent()?;
    let bundle = contents.parent()?;
    let is_bundle = exe_dir.file_name()? == "MacOS"
        && contents.file_name()? == "Contents"
        && bundle.extension()? == "app";
    is_bundle.then(|| contents.join("Resources"))
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
        for entry in ["MaaNikki_Mailbox", "MaaNikki_WeeklyFreeGifts", "MaaNikki_MonthlyPass", "MaaNikki_CollectStarlightCrystals",
                      "MaaNikki_Home", "MaaNikki_Dig", "MaaNikki_RedeemCodes"] {
            assert_eq!(preview_task_allowed(manifest, entry), Ok(true), "{}", entry);
        }
        for entry in ["MXU_LAUNCH", "MaaNikki_StartGame", "MaaNikki_CloseGame", "MaaNikki_Fishing",
                      "MaaNikki_MonthlyCrystalExchange", "MaaNikki_RouteTest", ""] {
            assert_eq!(preview_task_allowed(manifest, entry), Ok(false), "{}", entry);
        }
        let tiers = super::parse_task_tiers(manifest).unwrap();
        assert_eq!(tiers.verified.len(), 4);
        assert!(tiers.excluded.contains_key("MaaNikki_MonthlyCrystalExchange"));
    }

    #[test]
    fn the_list_file_refuses_duplicate_keys_and_nan() {
        use super::parse_task_tiers;
        for raw in [r#"{"entries":["A"],"notes":NaN}"#, r#"{"entries":["A"],"excluded":{"A":"why"},"excluded":{}}"#,
                    r#"{"entries":["A"],"entries":["B"]}"#, r#"{"entries":["A"],"excluded":{"B":"x","B":"y"}}"#] {
            assert!(parse_task_tiers(raw).is_err(), "{}", raw);
        }
        assert_eq!(parse_task_tiers(r#"{"entries":["A"]}"#).unwrap().verified, vec!["A".to_string()]);
    }

    #[test]
    fn the_tier_list_is_parsed_strictly() {
        use super::parse_task_tiers;
        assert_eq!(parse_task_tiers(r#"{"entries":["A"]}"#).unwrap().experimental.len(), 0);
        for bad in [r#"null"#, r#"[]"#, r#"{}"#, r#"{"entries":"A"}"#, r#"{"entries":["A",1]}"#, r#"{"entries":["A","A"]}"#,
                    r#"{"entries":[""]}"#, r#"{"entries":["A"],"experimental":"B"}"#, r#"{"entries":["A"],"experimental":["A"]}"#,
                    r#"{"entries":["A"],"excluded":{"A":"why"}}"#, r#"{"entries":["A"],"excluded":{"B":""}}"#,
                    r#"{"entries":["A"],"excluded":["B"]}"#] {
            assert!(parse_task_tiers(bad).is_err(), "{}", bad);
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
    fn the_shared_manifest_gives_the_ui_its_tiers_and_notes() {
        let manifest = include_str!("../../../../agent/host/macos/preview_tasks.json");
        let view = super::task_tiers_view(manifest);
        assert_eq!(view.error, None);
        assert_eq!(view.verified, super::parse_task_tiers(manifest).unwrap().verified);
        assert!(view.experimental.iter().any(|e| e == "MaaNikki_MinigameBling"));
        assert!(view.excluded.contains_key("MaaNikki_StartGame"));
        assert!(view.notes.contains_key("MaaNikki_CollectStarlightCrystals"));
        // Every experimental entry with consequences carries its note, so the UI has one source for them.
        for entry in ["MaaNikki_MinigameBling", "MaaNikki_Crown", "MaaNikki_Home", "MaaNikki_Zhaoxi",
                      "MaaNikki_OpenWorldCollect", "MaaNikki_PersistentAbilities", "MaaNikki_RouteRun",
                      "MaaNikki_Xinghai", "MaaNikki_RedeemCodes", "MaaNikki_RealmChallenge"] {
            assert!(view.experimental.iter().any(|e| e == entry), "{}", entry);
            assert!(view.notes.contains_key(entry), "{}", entry);
        }
        let json = serde_json::to_value(&view).unwrap();
        assert!(json.get("error").is_none());
        assert!(json["notes"].is_object() && json["excluded"].is_object());
    }

    #[test]
    fn an_unreadable_manifest_gives_the_ui_an_error_and_no_tiers() {
        use super::task_tiers_view;
        for bad in ["not json", "{}", r#"{"entries":["A"],"notes":["x"]}"#, r#"{"entries":["A"],"notes":{"A":""}}"#,
                    r#"{"entries":["A"],"notes":{"A":1}}"#] {
            let view = task_tiers_view(bad);
            assert!(view.error.as_deref().unwrap_or("").starts_with("无法读取 Mac 任务清单"), "{}", bad);
            assert!(view.verified.is_empty() && view.experimental.is_empty() && view.excluded.is_empty()
                && view.notes.is_empty(), "{}", bad);
        }
        let view = task_tiers_view(r#"{"entries":["A"]}"#);
        assert_eq!(view.error, None);
        assert!(view.notes.is_empty());
    }

    /// The Windows contract: no tiers, no markings.
    #[cfg(not(target_os = "macos"))]
    #[test]
    fn other_platforms_get_no_tiers() {
        let view = super::macos_task_tiers();
        assert_eq!(view, super::TaskTiersView::default());
        assert_eq!(serde_json::to_value(&view).unwrap(),
                   serde_json::json!({ "verified": [], "experimental": [], "excluded": {}, "notes": {} }));
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn macos_reads_the_shipped_manifest() {
        if std::env::var_os("MAANIKKI_DEV_HOME").is_some() {
            return; // A dev home holds its own copy of the manifest.
        }
        let manifest = include_str!("../../../../agent/host/macos/preview_tasks.json");
        assert_eq!(super::macos_task_tiers(), super::task_tiers_view(manifest));
    }

    use super::{admit_batch_on, admit_tasks, submitted_controller_kind, target_refuses_task, ControllerKind, GameTarget};

    fn kind(t: &str) -> Option<ControllerKind> {
        Some(ControllerKind::Type(t.to_string()))
    }

    fn envs(controller: &str) -> std::collections::HashMap<String, String> {
        std::collections::HashMap::from([("PI_CONTROLLER".to_string(), controller.to_string())])
    }

    /// Every task entry the shipped interface defines (tasks/*.json).
    fn shipped_entries() -> Vec<String> {
        let dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tasks");
        let mut entries = Vec::new();
        for file in std::fs::read_dir(dir).unwrap() {
            let text = std::fs::read_to_string(file.unwrap().path()).unwrap();
            let value: serde_json::Value = serde_json::from_str(&text).unwrap();
            for task in value.get("task").and_then(|t| t.as_array()).into_iter().flatten() {
                if let Some(entry) = task.get("entry").and_then(|e| e.as_str()) {
                    entries.push(entry.to_string());
                }
            }
        }
        entries.sort();
        entries.dedup();
        assert!(entries.len() > 20);
        entries
    }

    const WINDOWS: &str = r#"{"name":"nikki_windows","type":"Win32","win32":{"class_regex":"^UnrealWindow$"}}"#;
    const MACOS: &str = r#"{"name":"nikki_macos","type":"MacOS"}"#;
    const ADB: &str = r#"{"name":"nikki_android","type":"Adb"}"#;

    /// Windows itself: the production admission (start_tasks, manual and scheduled) and the append path admit
    /// every shipped entry, plus the start-game pretask entry and MXU's built-in launch task, with the Win32
    /// controller connected or not, submitted or not.
    #[cfg(target_os = "windows")]
    #[test]
    fn windows_admits_every_shipped_task_as_before() {
        let mut entries = shipped_entries();
        entries.extend(["MaaNikki_StartGame".to_string(), "MXU_LAUNCH".to_string()]);
        let refs: Vec<&str> = entries.iter().map(String::as_str).collect();
        let windows = kind("Win32");
        let submitted = submitted_controller_kind(Some(&envs(WINDOWS)), None);
        assert_eq!(submitted, windows);
        for (connected, submitted) in [(&windows, &submitted), (&None, &windows), (&windows, &None), (&None, &None)] {
            let admitted = admit_tasks(connected.as_ref(), submitted.as_ref(), &refs).unwrap();
            assert!(admitted.iter().all(Option::is_none), "{:?} {:?}", connected, submitted);
        }
        for entry in &refs {
            assert_eq!(target_refuses_task(windows.as_ref(), entry), None);
            assert_eq!(target_refuses_task(None, entry), None);
        }
    }

    /// macOS itself: with the MacOS controller the production admission gives exactly the host rules.
    #[cfg(target_os = "macos")]
    #[test]
    fn macos_admission_with_its_controller_matches_the_host_rules() {
        let mut entries = shipped_entries();
        entries.extend(["MaaNikki_StartGame".to_string(), "MXU_LAUNCH".to_string()]);
        let refs: Vec<&str> = entries.iter().map(String::as_str).collect();
        let expected: Vec<Option<String>> = refs.iter().map(|e| super::platform_refuses_task(e)).collect();
        let macos = submitted_controller_kind(Some(&envs(MACOS)), None);
        assert_eq!(macos, kind("MacOS"));
        for (connected, submitted) in [(&macos, &macos), (&None, &macos), (&macos, &None), (&None, &None)] {
            assert_eq!(admit_tasks(connected.as_ref(), submitted.as_ref(), &refs).unwrap(), expected);
        }
        for (entry, reason) in refs.iter().zip(&expected) {
            assert_eq!(&target_refuses_task(macos.as_ref(), entry), reason);
        }
    }

    #[test]
    fn the_host_rules_apply_unchanged_for_the_hosts_own_controller() {
        let refusing = |e: &str| if e == "B" { Some("no B".to_string()) } else { None };
        for (host, name) in [(GameTarget::Win32, "Win32"), (GameTarget::MacOS, "MacOS")] {
            let own = kind(name);
            for (connected, submitted) in [(&own, &own), (&None, &own), (&own, &None), (&None, &None)] {
                assert_eq!(admit_batch_on(Some(host), connected.as_ref(), submitted.as_ref(), &["A", "B"], refusing)
                               .unwrap(), vec![None, Some("no B".to_string())]);
            }
        }
    }

    #[test]
    fn other_controller_types_keep_the_host_rules_but_must_match() {
        let admit_all = |_: &str| None;
        for name in ["Gamepad", "PlayCover", "WlRoots"] {
            let raw = format!(r#"{{"name":"x","type":"{}"}}"#, name);
            let submitted = submitted_controller_kind(Some(&envs(&raw)), None);
            assert_eq!(submitted, kind(name));
            // Same type connected: the host rules, as before.
            assert_eq!(admit_batch_on(Some(GameTarget::Win32), kind(name).as_ref(), submitted.as_ref(), &["A"],
                                      admit_all).unwrap(), vec![None]);
            // A Win32 request while a legacy controller is connected (or the reverse) is a mismatch.
            assert!(admit_batch_on(Some(GameTarget::Win32), kind(name).as_ref(), kind("Win32").as_ref(), &["A"],
                                   admit_all).is_err());
            assert!(admit_batch_on(Some(GameTarget::Win32), kind("Win32").as_ref(), submitted.as_ref(), &["A"],
                                   admit_all).is_err());
        }
    }

    #[test]
    fn adb_mismatched_and_malformed_controllers_are_refused_as_a_batch() {
        let admit_all = |_: &str| None;
        let adb = submitted_controller_kind(Some(&envs(ADB)), None);
        assert_eq!(adb, kind("Adb"));
        for (host, name) in [(GameTarget::Win32, "Win32"), (GameTarget::MacOS, "MacOS")] {
            // Refused even with no tasks: a batch must not start up an agent for a controller that cannot run.
            for entries in [&["MaaNikki_Mailbox"][..], &[][..]] {
                let reason = admit_batch_on(Some(host), adb.as_ref(), adb.as_ref(), entries, admit_all).unwrap_err();
                assert!(reason.contains("安卓"), "{}", reason);
                assert!(admit_batch_on(Some(host), adb.as_ref(), None, entries, admit_all).is_err());
                assert!(admit_batch_on(Some(host), None, adb.as_ref(), entries, admit_all).is_err());
            }
            // Connected to an emulator while the request says the desktop controller (stale or missing metadata).
            assert!(admit_batch_on(Some(host), adb.as_ref(), kind(name).as_ref(), &["A"], admit_all).is_err());
        }
        assert!(admit_batch_on(Some(GameTarget::MacOS), kind("Win32").as_ref(), None, &["A"], admit_all).is_err());
        assert!(admit_batch_on(Some(GameTarget::Win32), kind("MacOS").as_ref(), None, &["A"], admit_all).is_err());
        for bad in ["{", "[]", r#"{"type":3}"#, r#"{"name":"x"}"#, r#"{"type":""}"#] {
            let submitted = submitted_controller_kind(Some(&envs(bad)), Some("Win32"));
            assert_eq!(submitted, Some(ControllerKind::Malformed), "{}", bad);
            assert!(admit_batch_on(Some(GameTarget::Win32), None, submitted.as_ref(), &["A"], admit_all).is_err());
        }
        assert_eq!(submitted_controller_kind(None, Some("")), None);
        assert_eq!(submitted_controller_kind(None, None), None);
        assert!(target_refuses_task(kind("Adb").as_ref(), "X").unwrap().contains("已跳过 X"));
    }

    #[test]
    fn the_connection_is_rechecked_before_binding() {
        use super::super::types::ControllerConfig;
        use super::readmit_on;
        let win32 = ControllerConfig::Win32 { handle: 1, screencap_method: 1, screencap_fallbacks: vec![],
                                              mouse_method: 1, keyboard_method: 1, display_short_side: None };
        let adb = ControllerConfig::Adb { adb_path: "adb".into(), address: "127.0.0.1:5555".into(),
                                          screencap_methods: "8".into(), input_methods: "4".into(),
                                          config: "{}".into(), display_short_side: None };
        let admit_all = |_: &str| None;
        let host = Some(GameTarget::Win32);
        let ok = vec![None];
        // Unchanged connection.
        assert!(readmit_on(host, kind("Win32").as_ref(), Some(&win32), kind("Win32").as_ref(), &["A"], &ok, admit_all).is_ok());
        // Windows as before: the request named Win32 and the first connection completed after admission.
        assert!(readmit_on(host, None, Some(&win32), kind("Win32").as_ref(), &["A"], &ok, admit_all).is_ok());
        assert!(readmit_on(host, None, Some(&win32), None, &["A"], &ok, admit_all).is_ok());
        // Switched to (or first connected as) an emulator: refused.
        assert!(readmit_on(host, kind("Win32").as_ref(), Some(&adb), kind("Win32").as_ref(), &["A"], &ok, admit_all).is_err());
        assert!(readmit_on(host, None, Some(&adb), None, &["A"], &ok, admit_all).is_err());
        // Admission results that differ under the new connection also refuse.
        let refusing = |_: &str| Some("no".to_string());
        assert_eq!(readmit_on(host, None, Some(&win32), None, &["A"], &ok, refusing).unwrap_err(),
                   super::CONNECTION_CHANGED);
    }


    #[test]
    fn a_broken_manifest_is_an_error() {
        assert!(preview_task_allowed("{}", "MaaNikki_Mailbox").is_err());
        assert!(preview_task_allowed("not json", "MaaNikki_Mailbox").is_err());
    }
}

#[cfg(test)]
mod macos_bundle_tests {
    use super::macos_bundle_resources_dir;
    use std::path::{Path, PathBuf};

    #[test]
    fn an_app_bundle_reads_its_resources_from_contents_resources() {
        assert_eq!(
            macos_bundle_resources_dir(Path::new("/Applications/MaaNikki.app/Contents/MacOS")),
            Some(PathBuf::from("/Applications/MaaNikki.app/Contents/Resources"))
        );
        // Gatekeeper's App Translocation runs the bundle from a random read-only path.
        assert_eq!(
            macos_bundle_resources_dir(Path::new("/private/var/folders/x/T/AppTranslocation/1/d/MaaNikki.app/Contents/MacOS")),
            Some(PathBuf::from("/private/var/folders/x/T/AppTranslocation/1/d/MaaNikki.app/Contents/Resources"))
        );
    }

    #[test]
    fn anything_else_keeps_the_executable_directory() {
        for dir in [
            "/repo/client/src-tauri/target/release",
            "/x/Contents/MacOS",
            "/x/MaaNikki.app/MacOS",
            "/x/MaaNikki.app/Contents/Frameworks",
            "MacOS",
            "/",
            "C:\\MaaNikki",
        ] {
            assert_eq!(macos_bundle_resources_dir(Path::new(dir)), None, "{}", dir);
        }
    }
}
