//! 安卓模拟器的运行归属（docs/android-support-plan.md「客户端接线的设计」）。只在 macOS 上编译。
//!
//! 整个进程只有一个安卓运行槽：空闲 → 启动中(令牌) → 运行中(令牌) → 收尾(令牌) → 空闲。这一轮的 Tasker、
//! AgentClient 和 agent 子进程都由这个槽持有，不放进实例的 `tasker`/`agent_clients`/`agent_children`，所以
//! Windows 和原生 Mac 共用的开始、停止、回调和释放代码看不到它们。共用函数只在入口加一个分支：这个实例有
//! 安卓运行时转到这里或拒绝；Win32 和 MacOS 控制器走不到这里（槽只会被 Adb 实例的开始占用，占用期间这个
//! 实例的重连、换控制器、销毁一律拒绝）。
//!
//! 预览版靠拒绝缩小状态空间，而不是处理每一种时序：同一时间只有一个安卓实例在运行；只整批开始，不追加、
//! 不改选项（提交时管线覆盖一律是 "{}"）；运行中（包括收尾）不重连、不换控制器、不断开、不销毁实例或资源、
//! 不直接点击；停止之后，在 agent 子进程退出并放开 `owner.lock` 之前，下一次开始被拒绝。
//!
//! 锁的顺序：槽 → 实例（`MaaState::instances`）→ 遥测。持有槽或实例锁时从不丢弃 Tasker、AgentClient 或
//! 子进程，也从不等待 Tasker：它的回调线程会来拿这两把锁。

use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::process::Child;
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::{Duration, Instant};

use log::{info, warn};
use maa_framework::agent_client::AgentClient;
use maa_framework::common::{AdbInputMethod, AdbScreencapMethod};
use maa_framework::tasker::Tasker;
use tauri::Emitter;

use super::types::{AgentConfig, ControllerConfig, ControllerInfo, InstanceRuntime, MaaState, TaskConfig};

/// 被停止取消的开始返回给前端的标记（与共用流程相同，前端据此不报错）。
const CANCELLED: &str = "MXU_PRE_ACTION_CANCELLED";

pub const BUSY_OTHER_INSTANCE: &str = "同一时间只能有一个安卓实例在运行，请等另一个实例的这一轮结束后再开始。";
pub const ALREADY_RUNNING: &str = "这个实例的安卓任务已经在运行，请等这一轮结束，或先停止。";
pub const STILL_FINISHING: &str = "上一轮安卓任务还在收尾（等待 agent 退出），请稍后再开始。";
pub const STILL_CONNECTING: &str = "这个实例正在连接控制器，请等连接完成后再开始。";
pub const OWNER_LOCK_BUSY: &str =
    "安卓模拟器的输入锁（owner.lock）还被别的进程占用（例如上一轮还没退出的 agent，或开发用的运行器），请等它结束后再开始。";
pub const NOT_FROM_TASK_LIST: &str = "安卓模拟器上只能从任务列表开始任务。";
pub const DUPLICATE_ENTRIES: &str = "任务列表里有重复的条目，无法在安卓模拟器上开始。";
pub const ONE_AGENT_ONLY: &str = "安卓模拟器上只支持一个 agent，无法开始任务。";
pub const NOT_CONNECTED: &str = "安卓模拟器没有连接，请重新连接后再开始。";
pub const NO_RESOURCE: &str = "资源还没有加载，请等资源加载完成后再开始。";
pub const DESKTOP_RUN_ACTIVE: &str = "这个实例还有别的任务在运行，请先停止。";
pub const DIRECT_CLICK_REFUSED: &str = "安卓模拟器上不提供直接点击：触摸只由 agent 在核对画面并记录之后发送。";

/// 运行中被拒绝的操作。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Op {
    Connect,
    Disconnect,
    DestroyInstance,
    LoadResource,
    DestroyResource,
    AppendTask,
    ChangeOptions,
    Click,
}

fn busy_refusal(op: Op, finishing: bool) -> String {
    match op {
        Op::AppendTask => return super::utils::ANDROID_APPEND_REFUSED.to_string(),
        Op::ChangeOptions => return super::utils::ANDROID_LIVE_OVERRIDE_REFUSED.to_string(),
        Op::Click => return DIRECT_CLICK_REFUSED.to_string(),
        _ => {}
    }
    let action = match op {
        Op::Connect => "重新连接或更换控制器",
        Op::Disconnect => "断开控制器",
        Op::DestroyInstance => "关闭这个实例",
        Op::LoadResource | Op::DestroyResource => "重新加载或更换资源",
        Op::AppendTask | Op::ChangeOptions | Op::Click => unreachable!(),
    };
    if finishing {
        format!("安卓模拟器预览版：上一轮安卓任务还在收尾（等待 agent 退出），这期间不能{}，请稍后再试。", action)
    } else {
        format!("安卓模拟器预览版：安卓任务运行中不能{}，请先停止并等这一轮收尾结束后再试。", action)
    }
}

/// 槽的阶段。令牌在每次开始时新取；回调、停止和收尾只作用于令牌相同的那一轮。
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub(crate) enum Phase {
    #[default]
    Idle,
    /// `cancelled`：开始期间用户按了停止；开始的那条路径在提交任务之前发现它并自己收尾。
    Starting { token: u64, instance_id: String, cancelled: bool },
    Running { token: u64, instance_id: String },
    /// `released`：收尾线程已经放掉了 Tasker、agent 和子进程，但当时 `owner.lock` 仍被占用；之后每次检查
    /// 槽时再试一次这把锁，能拿到就回到空闲。
    Finishing { token: u64, instance_id: String, released: bool },
}

impl Phase {
    fn instance_id(&self) -> Option<&str> {
        match self {
            Phase::Idle => None,
            Phase::Starting { instance_id, .. }
            | Phase::Running { instance_id, .. }
            | Phase::Finishing { instance_id, .. } => Some(instance_id),
        }
    }
}

/// 收尾时的期限（测试里缩短）。
#[derive(Clone, Copy, Debug)]
pub(crate) struct Timing {
    pub tasker_stop: Duration,
    pub child_exit: Duration,
    pub lock_wait: Duration,
    pub poll: Duration,
}

impl Default for Timing {
    fn default() -> Self {
        Timing {
            tasker_stop: Duration::from_secs(10),
            child_exit: Duration::from_secs(5),
            lock_wait: Duration::from_secs(10),
            poll: Duration::from_millis(100),
        }
    }
}

/// 这一轮向前端发出的通知（生产环境发给 Tauri 和 WebSocket，测试里记录下来）。
pub(crate) trait RunEvents: Send + Sync {
    fn state_changed(&self, instance_id: &str, kind: &str);
    fn message(&self, instance_id: &str, line: &str);
}

struct AppEvents(tauri::AppHandle);

impl RunEvents for AppEvents {
    fn state_changed(&self, instance_id: &str, kind: &str) {
        super::utils::emit_state_changed(&self.0, instance_id, kind);
    }
    fn message(&self, instance_id: &str, line: &str) {
        super::maa_agent::emit_agent_output(&self.0, instance_id, "stderr", line);
    }
}

/// 这一轮自己的句柄。
pub(crate) struct RunHandles {
    pub tasker: Option<Tasker>,
    pub client: Option<AgentClient>,
    pub child: Option<Child>,
    pub events: Arc<dyn RunEvents>,
}

#[derive(Default)]
struct SlotInner {
    phase: Phase,
    run: Option<RunHandles>,
    /// 每个实例正在进行的连接数（connect_controller_impl 的入口登记，返回时注销）。
    connecting: HashMap<String, usize>,
    next_token: u64,
}

/// 整个进程唯一的安卓运行槽（放在 `MaaState` 里）。
#[derive(Default)]
pub struct AndroidRunSlot {
    inner: Arc<Mutex<SlotInner>>,
    /// `owner.lock` 所在目录；None 时用数据目录下的 config/android-input（测试里指向临时目录）。
    lock_dir: Option<PathBuf>,
    timing: Timing,
}

fn lock(inner: &Mutex<SlotInner>) -> MutexGuard<'_, SlotInner> {
    // 槽里的状态在每个临界区结束时都是一致的；某个线程在持锁时 panic 也不应让安卓运行永远拒绝。
    inner.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

impl AndroidRunSlot {
    #[cfg(test)]
    pub(crate) fn for_tests(lock_dir: PathBuf, timing: Timing) -> Self {
        AndroidRunSlot { inner: Arc::default(), lock_dir: Some(lock_dir), timing }
    }

    fn lock_dir(&self) -> Result<PathBuf, String> {
        match &self.lock_dir {
            Some(dir) => Ok(dir.clone()),
            None => super::utils::get_app_data_dir().map(|dir| super::android_input::input_dir(&dir)),
        }
    }

    #[cfg(test)]
    pub(crate) fn phase(&self) -> Phase {
        lock(&self.inner).phase.clone()
    }

    #[cfg(test)]
    fn connecting(&self, instance_id: &str) -> usize {
        lock(&self.inner).connecting.get(instance_id).copied().unwrap_or(0)
    }

    /// 槽对这个实例忙时返回它的阶段。收尾已放掉句柄、只在等 `owner.lock` 时，先再试一次这把锁：
    /// 能拿到就回到空闲（返回 None）。
    fn busy_for(&self, inner: &mut SlotInner, instance_id: &str) -> Option<Phase> {
        self.heal(inner);
        (inner.phase.instance_id() == Some(instance_id)).then(|| inner.phase.clone())
    }

    fn heal(&self, inner: &mut SlotInner) {
        if matches!(inner.phase, Phase::Finishing { released: true, .. })
            && self.lock_dir().is_ok_and(|dir| super::android_input::owner_lock_free(&dir) == Ok(true))
        {
            info!("[android_run] owner.lock is free again; the slot is idle");
            inner.phase = Phase::Idle;
        }
    }
}

// ============================================================================
// 共用函数的入口分支
// ============================================================================

/// 这个实例有安卓运行（启动中、运行中或收尾）时拒绝该操作。
pub fn refuse_while_busy(state: &MaaState, instance_id: &str, op: Op) -> Result<(), String> {
    let slot = &state.android_run;
    let mut inner = lock(&slot.inner);
    match slot.busy_for(&mut inner, instance_id) {
        None => Ok(()),
        Some(phase) => Err(busy_refusal(op, matches!(phase, Phase::Finishing { .. }))),
    }
}

/// 直接点击：安卓运行期间拒绝，而且 Adb 实例一律拒绝（直接点击绕过了 agent 核对画面和记录的触摸路径）。
pub fn refuse_click(state: &MaaState, instance_id: &str) -> Result<(), String> {
    refuse_while_busy(state, instance_id, Op::Click)?;
    let instances = state.instances.lock().map_err(|e| e.to_string())?;
    if matches!(
        instances.get(instance_id).and_then(|inst| inst.controller_config.as_ref()),
        Some(ControllerConfig::Adb { .. })
    ) {
        return Err(DIRECT_CLICK_REFUSED.to_string());
    }
    Ok(())
}

/// 停止：这个实例有安卓运行时由这里处理并返回 Some，否则返回 None（调用方照原来的流程）。
/// 启动中 → 标记取消（开始的那条路径收尾）；运行中 → 未完成的任务记为失败、整轮失败、开始收尾；
/// 收尾中 → 什么都不做。重复调用没有副作用。
pub fn stop_if_busy(state: &MaaState, instance_id: &str) -> Option<Result<(), String>> {
    let slot = &state.android_run;
    let job = {
        let mut inner = lock(&slot.inner);
        slot.busy_for(&mut inner, instance_id)?;
        match &mut inner.phase {
            Phase::Starting { cancelled, .. } => {
                info!("[android_run] stop while starting on {}: the start is cancelled", instance_id);
                *cancelled = true;
                None
            }
            Phase::Running { token, .. } => {
                let token = *token;
                info!("[android_run] stop on {}: finishing the run", instance_id);
                match state.instances.lock() {
                    Ok(mut instances) => {
                        if let Some(instance) = instances.get_mut(instance_id) {
                            fail_unfinished(instance);
                        }
                    }
                    Err(e) => warn!("[android_run] instances lock poisoned while stopping: {}", e),
                }
                super::telemetry::on_run_cancelled(instance_id);
                Some(begin_finishing(slot, &mut inner, token, "task-stopped"))
            }
            Phase::Finishing { .. } | Phase::Idle => None,
        }
    };
    if let Some(job) = job {
        job.spawn();
    }
    Some(Ok(()))
}

/// 实例状态里的「正在运行」：这个实例的安卓运行处于启动中或运行中（收尾时已不算运行，开始会被拒绝并说明原因）。
pub fn reports_running(state: &MaaState, instance_id: &str) -> bool {
    running_instance(state).as_deref() == Some(instance_id)
}

/// 处于启动中或运行中的安卓实例（给状态快照用：在拿实例锁之前读）。
pub fn running_instance(state: &MaaState) -> Option<String> {
    match &lock(&state.android_run.inner).phase {
        Phase::Starting { instance_id, .. } | Phase::Running { instance_id, .. } => Some(instance_id.clone()),
        Phase::Idle | Phase::Finishing { .. } => None,
    }
}

/// connect_controller_impl 的入口：这个实例有安卓运行时拒绝（重连、换控制器）；否则登记一次进行中的连接
/// （进行中时安卓的开始被拒绝），并把 Adb 控制器的输入和截图方式固定为控制器定义里的那两种
/// （Maatouch、RawWithGzip，决定 5）。其他控制器的配置原样返回。
pub fn enter_connect(
    state: &MaaState,
    instance_id: &str,
    config: ControllerConfig,
) -> Result<(ControllerConfig, ConnectGuard), String> {
    let slot = &state.android_run;
    let mut inner = lock(&slot.inner);
    if let Some(phase) = slot.busy_for(&mut inner, instance_id) {
        return Err(busy_refusal(Op::Connect, matches!(phase, Phase::Finishing { .. })));
    }
    *inner.connecting.entry(instance_id.to_string()).or_insert(0) += 1;
    Ok((
        pin_adb_methods(config),
        ConnectGuard { inner: Arc::clone(&slot.inner), instance_id: instance_id.to_string() },
    ))
}

/// 一次进行中的连接；连接返回（或被丢弃）时注销。
pub struct ConnectGuard {
    inner: Arc<Mutex<SlotInner>>,
    instance_id: String,
}

impl Drop for ConnectGuard {
    fn drop(&mut self) {
        let mut inner = lock(&self.inner);
        if let Some(count) = inner.connecting.get_mut(&self.instance_id) {
            *count -= 1;
            if *count == 0 {
                inner.connecting.remove(&self.instance_id);
            }
        }
    }
}

/// 安卓控制器只用 Maatouch 输入和 RawWithGzip 截图（build/android/interface-overlay.json 的 nikki_android）。
pub const PINNED_INPUT: AdbInputMethod = AdbInputMethod::MAATOUCH;
pub const PINNED_SCREENCAP: AdbScreencapMethod = AdbScreencapMethod::RAW_WITH_GZIP;

pub fn pin_adb_methods(config: ControllerConfig) -> ControllerConfig {
    match config {
        ControllerConfig::Adb { adb_path, address, config, display_short_side, .. } => ControllerConfig::Adb {
            adb_path,
            address,
            screencap_methods: PINNED_SCREENCAP.bits().to_string(),
            input_methods: PINNED_INPUT.bits().to_string(),
            config,
            display_short_side,
        },
        other => other,
    }
}

// ============================================================================
// 开始
// ============================================================================

/// start_tasks_impl 在准入和 android_start_refusal 都通过、目标是 Adb 之后转到这里。整个开始在单独的任务里
/// 运行：调用方的 future 被丢弃（例如 HTTP 连接断开）时，开始照样走完，槽不会停在半路。
#[allow(clippy::too_many_arguments)]
pub async fn start(
    app: tauri::AppHandle,
    maa_state: &Arc<MaaState>,
    instance_id: String,
    tasks: Vec<TaskConfig>,
    agent_configs: Option<Vec<AgentConfig>>,
    cwd: String,
    tcp_compat_mode: bool,
    pi_envs: Option<HashMap<String, String>>,
    reset_state: bool,
    controller_info: Option<ControllerInfo>,
    refusals: Vec<Option<String>>,
) -> Result<Vec<i64>, String> {
    let state = Arc::clone(maa_state);
    let app_for_output = app.clone();
    let id_for_output = instance_id.clone();
    let result = tauri::async_runtime::spawn(async move {
        start_owned(
            app, state, instance_id, tasks, agent_configs, cwd, tcp_compat_mode, pi_envs, reset_state,
            controller_info, refusals,
        )
        .await
    })
    .await
    .map_err(|e| e.to_string())?;
    if let Err(reason) = &result {
        if reason != CANCELLED {
            super::maa_agent::emit_agent_output(&app_for_output, &id_for_output, "stderr", reason);
        }
    }
    result
}

/// 不需要任何状态就能判断的开始条件：只整批开始；每个任务都来自任务列表，条目不重复（这一轮按条目判定结束）；
/// 只有一个 agent。
pub(crate) fn batch_refusal(
    tasks: &[TaskConfig],
    agent_configs: Option<&[AgentConfig]>,
    reset_state: bool,
) -> Option<&'static str> {
    if !reset_state {
        return Some(super::utils::ANDROID_APPEND_REFUSED);
    }
    let mut seen = HashSet::new();
    for task in tasks {
        match task.selected_task_id.as_deref() {
            None | Some("") => return Some(NOT_FROM_TASK_LIST),
            Some(id) if !seen.insert(id) => return Some(DUPLICATE_ENTRIES),
            Some(_) => {}
        }
    }
    if agent_configs.map_or(0, <[AgentConfig]>::len) != 1 {
        return Some(ONE_AGENT_ONLY);
    }
    None
}

#[allow(clippy::too_many_arguments)]
async fn start_owned(
    app: tauri::AppHandle,
    state: Arc<MaaState>,
    instance_id: String,
    tasks: Vec<TaskConfig>,
    agent_configs: Option<Vec<AgentConfig>>,
    cwd: String,
    tcp_compat_mode: bool,
    pi_envs: Option<HashMap<String, String>>,
    reset_state: bool,
    controller_info: Option<ControllerInfo>,
    refusals: Vec<Option<String>>,
) -> Result<Vec<i64>, String> {
    if let Some(reason) = batch_refusal(&tasks, agent_configs.as_deref(), reset_state) {
        return Err(reason.to_string());
    }
    if refusals.iter().all(Option::is_some) {
        return Err(refusals.into_iter().flatten().next().unwrap_or_default());
    }
    let agent = agent_configs.and_then(|configs| configs.into_iter().next()).ok_or(ONE_AGENT_ONLY)?;
    // 交给 agent 的运行计划只列出会执行的任务（与共用流程相同）。
    let mut pi_envs = pi_envs.unwrap_or_default();
    if refusals.iter().any(Option::is_some) {
        let accepted: Vec<&str> = tasks
            .iter()
            .zip(&refusals)
            .filter(|(_, refused)| refused.is_none())
            .map(|(task, _)| task.entry.as_str())
            .collect();
        if let Some(plan) = pi_envs.get_mut("PI_MAANIKKI_RUN_PLAN") {
            *plan = super::utils::run_plan_with_entries(plan, &accepted);
        }
    }

    let claim = claim(&state, &instance_id)?;
    let token = claim.token;
    info!("[android_run] start on {} (token {})", instance_id, token);

    let (resource, controller, controller_raw) = {
        let instances = state.instances.lock().map_err(|e| e.to_string())?;
        let instance = instances.get(&instance_id).ok_or("Instance not found")?;
        if !matches!(instance.controller_config, Some(ControllerConfig::Adb { .. })) || instance.controller_stopped {
            return Err(NOT_CONNECTED.to_string());
        }
        let controller = instance.controller.clone().filter(|c| c.connected()).ok_or(NOT_CONNECTED)?;
        let resource = instance.resource.clone().filter(|r| r.loaded()).ok_or(NO_RESOURCE)?;
        if instance.tasker.as_ref().is_some_and(|t| t.running()) {
            return Err(DESKTOP_RUN_ACTIVE.to_string());
        }
        let raw = controller.raw() as usize;
        (resource, controller, raw)
    };

    let events: Arc<dyn RunEvents> = Arc::new(AppEvents(app.clone()));
    let tasker = Tasker::new().map_err(|e| e.to_string())?;
    {
        let sink_state = Arc::clone(&state);
        let sink_app = app.clone();
        let sink_id = instance_id.clone();
        tasker
            .add_sink(move |msg, detail| {
                on_tasker_event(&sink_state, &sink_app, &sink_id, token, msg, detail);
                super::utils::emit_callback_event(&sink_app, msg, detail);
            })
            .map_err(|e| e.to_string())?;
        let ctx_app = app.clone();
        let ctx_id = instance_id.clone();
        tasker
            .add_context_sink(move |msg, detail| {
                super::telemetry::on_node_event(&ctx_id, msg, detail);
                super::utils::emit_callback_event(&ctx_app, msg, detail);
            })
            .map_err(|e| e.to_string())?;
    }
    tasker.bind(&resource, &controller).map_err(|e| e.to_string())?;
    if !tasker.inited() {
        return Err("Tasker not properly initialized".to_string());
    }

    // 停止在启动 agent 之前就到了：不再启动它。
    if claim.cancelled() {
        return Err(CANCELLED.to_string());
    }
    let (client, child) = super::maa_agent::start_single_agent(
        app.clone(),
        agent,
        0,
        instance_id.clone(),
        cwd,
        tcp_compat_mode,
        resource,
        controller,
        tasker.clone(),
        Arc::new(pi_envs),
    )
    .await
    .map_err(|e| format!("Agent start failed: {}", e))?;

    let handles = RunHandles { tasker: Some(tasker.clone()), client: Some(client), child: Some(child), events };
    let task_names: Vec<String> =
        tasks.iter().map(|task| task.task_name.clone().unwrap_or_else(|| task.entry.clone())).collect();
    // 遥测：必须在 post_task 之前开始这一轮（与共用流程相同）。
    super::telemetry::on_run_start(&instance_id, &task_names, controller_info.as_ref());
    let notices: Vec<String> = tasks
        .iter()
        .zip(&refusals)
        .filter(|(_, refused)| refused.is_none())
        .filter_map(|(task, _)| super::utils::android_override_notice(&task.entry, &task.pipeline_override))
        .collect();
    for notice in &notices {
        super::maa_agent::emit_agent_output(&app, &instance_id, "stderr", notice);
    }
    for reason in refusals.iter().flatten() {
        super::maa_agent::emit_agent_output(&app, &instance_id, "stderr", reason);
    }

    let (result, job) = claim.commit(handles, |instance| {
        // 开始到这里，实例的连接不能变（连接在进行中或运行中都会被拒绝；这里再核对一次）。
        if instance.controller.as_ref().map(|c| c.raw() as usize) != Some(controller_raw)
            || !matches!(instance.controller_config, Some(ControllerConfig::Adb { .. }))
        {
            return Err(super::utils::CONNECTION_CHANGED.to_string());
        }
        Ok(post_tasks(&instance_id, instance, &tasker, &tasks, &refusals))
    });
    if let Some(job) = job {
        job.spawn();
    }
    match &result {
        Ok(ids) if ids.is_empty() => super::telemetry::on_run_cancelled(&instance_id),
        Ok(_) => super::utils::emit_state_changed(&app, &instance_id, "task-started"),
        Err(_) => super::telemetry::on_run_cancelled(&instance_id),
    }
    result
}

/// 在槽和实例两把锁里提交任务，并像共用流程的首批那样初始化实例的任务运行状态。返回提交成功的任务 ID。
fn post_tasks(
    instance_id: &str,
    instance: &mut InstanceRuntime,
    tasker: &Tasker,
    tasks: &[TaskConfig],
    refusals: &[Option<String>],
) -> Vec<i64> {
    let mut telemetry_posting = super::telemetry::begin_posting(instance_id);
    instance.task_ids.clear();
    let run_state = &mut instance.task_run_state;
    run_state.statuses.clear();
    run_state.mappings.clear();
    run_state.pending_task_ids.clear();
    run_state.current_task_index = 0;
    let mut ids = Vec::new();
    for (task, refused) in tasks.iter().zip(refusals) {
        let Some(selected_id) = task.selected_task_id.clone() else { continue };
        if refused.is_some() {
            run_state.statuses.insert(selected_id, "failed".to_string());
            continue;
        }
        // 安卓上不应用任务选项（风险 16）：提交的覆盖一律是 "{}"。
        match tasker.post_task(&task.entry, super::utils::ANDROID_PIPELINE_OVERRIDE) {
            Ok(job) => {
                info!("[android_run] posted {} as task {}", task.entry, job.id);
                ids.push(job.id);
                run_state.pending_task_ids.push(job.id);
                run_state.mappings.insert(job.id, selected_id.clone());
                run_state.statuses.insert(selected_id, "pending".to_string());
                let name = task.task_name.clone().unwrap_or_else(|| task.entry.clone());
                super::task_stats::register_posted_task(instance_id, job.id, &name);
                if let Some(posting) = telemetry_posting.as_mut() {
                    posting.register(
                        job.id,
                        super::telemetry::TaskMeta { name, options: task.options.clone().unwrap_or_default() },
                    );
                }
            }
            Err(e) => {
                warn!("[android_run] failed to post {}: {}", task.entry, e);
                run_state.statuses.insert(selected_id, "failed".to_string());
            }
        }
    }
    drop(telemetry_posting);
    if ids.is_empty() {
        run_state.overall_status = Some("Failed".to_string());
        instance.task_ids.clear();
    } else {
        run_state.overall_status = Some("Running".to_string());
        instance.task_ids = ids.clone();
    }
    ids
}

/// 取得槽：只有在空闲、这个实例没有进行中的连接、而且 `owner.lock` 此刻能拿到（拿到后立即放开）时。
fn claim(state: &Arc<MaaState>, instance_id: &str) -> Result<StartClaim, String> {
    let slot = &state.android_run;
    let mut inner = lock(&slot.inner);
    slot.heal(&mut inner);
    match &inner.phase {
        Phase::Idle => {}
        phase if phase.instance_id() != Some(instance_id) => return Err(BUSY_OTHER_INSTANCE.to_string()),
        Phase::Finishing { .. } => return Err(STILL_FINISHING.to_string()),
        _ => return Err(ALREADY_RUNNING.to_string()),
    }
    if inner.connecting.get(instance_id).is_some_and(|count| *count > 0) {
        return Err(STILL_CONNECTING.to_string());
    }
    let dir = slot.lock_dir()?;
    match super::android_input::owner_lock_free(&dir) {
        Ok(true) => {}
        Ok(false) => return Err(OWNER_LOCK_BUSY.to_string()),
        Err(e) => return Err(format!("无法检查安卓模拟器的输入锁：{}", e)),
    }
    inner.next_token += 1;
    let token = inner.next_token;
    inner.phase = Phase::Starting { token, instance_id: instance_id.to_string(), cancelled: false };
    Ok(StartClaim { state: Arc::clone(state), instance_id: instance_id.to_string(), token })
}

/// 一次开始取得的槽。丢弃时，如果槽仍是这次的「启动中」（开始在启动 agent 之前就失败或被取消，没有要收尾的
/// 子进程），回到空闲。不能在持有槽锁时丢弃。
struct StartClaim {
    state: Arc<MaaState>,
    instance_id: String,
    token: u64,
}

impl StartClaim {
    fn cancelled(&self) -> bool {
        matches!(lock(&self.state.android_run.inner).phase, Phase::Starting { cancelled: true, .. })
    }

    /// 提交前最后一步：在槽锁里确认这次开始仍属于这一轮、没有被停止，再在实例锁里调用 `post` 提交任务。
    /// 提交了任务 → 运行中，句柄交给槽；被取消、`post` 失败或没有任务提交成功 → 收尾（返回的收尾工作
    /// 由调用方在锁外启动），收尾完成、子进程退出且 `owner.lock` 能拿到之后才回到空闲。
    fn commit(
        self,
        handles: RunHandles,
        post: impl FnOnce(&mut InstanceRuntime) -> Result<Vec<i64>, String>,
    ) -> (Result<Vec<i64>, String>, Option<FinishJob>) {
        let slot = &self.state.android_run;
        let mut inner = lock(&slot.inner);
        let owned = matches!(&inner.phase,
            Phase::Starting { token, cancelled: false, .. } if *token == self.token);
        if !owned {
            info!("[android_run] start on {} was cancelled before posting", self.instance_id);
            return (Err(CANCELLED.to_string()), Some(self.finish_with(&mut inner, handles, "task-stopped")));
        }
        let posted = match self.state.instances.lock() {
            Ok(mut instances) => match instances.get_mut(&self.instance_id) {
                Some(instance) => post(instance),
                None => Err("Instance not found".to_string()),
            },
            Err(e) => Err(e.to_string()),
        };
        match posted {
            Ok(ids) if !ids.is_empty() => {
                inner.phase = Phase::Running { token: self.token, instance_id: self.instance_id.clone() };
                inner.run = Some(handles);
                (Ok(ids), None)
            }
            Ok(ids) => (Ok(ids), Some(self.finish_with(&mut inner, handles, "tasks-completed"))),
            Err(reason) => (Err(reason), Some(self.finish_with(&mut inner, handles, "task-stopped"))),
        }
    }

    fn finish_with(&self, inner: &mut SlotInner, handles: RunHandles, kind: &'static str) -> FinishJob {
        let slot = &self.state.android_run;
        if matches!(&inner.phase, Phase::Starting { token, .. } if *token == self.token) {
            inner.phase =
                Phase::Finishing { token: self.token, instance_id: self.instance_id.clone(), released: false };
        }
        FinishJob {
            slot: Arc::clone(&slot.inner),
            lock_dir: slot.lock_dir(),
            timing: slot.timing,
            token: self.token,
            instance_id: self.instance_id.clone(),
            handles,
            kind,
        }
    }
}

impl Drop for StartClaim {
    fn drop(&mut self) {
        let mut inner = lock(&self.state.android_run.inner);
        if matches!(&inner.phase, Phase::Starting { token, .. } if *token == self.token) {
            inner.phase = Phase::Idle;
        }
    }
}

// ============================================================================
// 回调
// ============================================================================

/// 一条 Tasker 回调对这一轮的作用。
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum Applied {
    /// 不是任务回调、令牌不是当前这一轮的、或这一轮已不在运行中。
    Ignored,
    Started(i64),
    Finished { task_id: i64, succeeded: bool, all_done: bool },
    /// 内部的 post_stop（MaaTaskerPostStop）结束：剩下的任务一并失败，这一轮收尾。
    Aborted,
}

fn on_tasker_event(
    state: &Arc<MaaState>,
    app: &tauri::AppHandle,
    instance_id: &str,
    token: u64,
    message: &str,
    details: &str,
) {
    let (applied, job) = apply_task_event(state, token, message, details);
    match applied {
        Applied::Ignored => {}
        Applied::Started(task_id) => {
            super::task_stats::on_task_start(instance_id, task_id);
            super::telemetry::on_task_start(instance_id, task_id);
        }
        Applied::Finished { task_id, succeeded, all_done } => {
            super::task_stats::on_task_finished(app, instance_id, task_id, succeeded);
            super::telemetry::on_task_finished(instance_id, task_id, succeeded);
            let _ = app.emit("adventure-report-changed", ());
            if all_done {
                super::telemetry::on_run_finished(instance_id);
            }
        }
        Applied::Aborted => super::telemetry::on_run_finished(instance_id),
    }
    if applied != Applied::Ignored {
        super::utils::emit_state_changed(app, instance_id, "task-progress");
    }
    // 收尾在单独的线程里做：不能在 Tasker 自己的回调线程上等它停下或丢弃它。
    if let Some(job) = job {
        job.spawn();
    }
}

/// 按回调更新实例的任务运行状态（与 handle_task_callback 对首批任务的做法相同），只认当前这一轮的令牌。
/// 全部任务结束或内部 post_stop 时转为收尾，返回收尾工作（调用方在锁外启动）。
pub(crate) fn apply_task_event(
    state: &MaaState,
    token: u64,
    message: &str,
    details: &str,
) -> (Applied, Option<FinishJob>) {
    let is_started = message == "Tasker.Task.Starting";
    let is_succeeded = message == "Tasker.Task.Succeeded";
    let is_failed = message == "Tasker.Task.Failed";
    if !is_started && !is_succeeded && !is_failed {
        return (Applied::Ignored, None);
    }
    let Ok(parsed) = serde_json::from_str::<serde_json::Value>(details) else {
        return (Applied::Ignored, None);
    };
    let Some(task_id) = parsed.get("task_id").and_then(|id| id.as_i64()) else {
        return (Applied::Ignored, None);
    };
    let post_stop = parsed.get("entry").and_then(|e| e.as_str()) == Some("MaaTaskerPostStop");
    if post_stop && is_started {
        return (Applied::Ignored, None);
    }

    let slot = &state.android_run;
    let mut inner = lock(&slot.inner);
    let instance_id = match &inner.phase {
        Phase::Running { token: current, instance_id } if *current == token => instance_id.clone(),
        _ => return (Applied::Ignored, None),
    };
    let Ok(mut instances) = state.instances.lock() else {
        return (Applied::Ignored, None);
    };
    let Some(instance) = instances.get_mut(&instance_id) else {
        return (Applied::Ignored, None);
    };

    if post_stop {
        fail_unfinished(instance);
        drop(instances);
        let job = begin_finishing(slot, &mut inner, token, "tasks-completed");
        return (Applied::Aborted, Some(job));
    }

    let run_state = &mut instance.task_run_state;
    if !run_state.pending_task_ids.contains(&task_id) {
        return (Applied::Ignored, None);
    }
    if is_started {
        if let Some(idx) = run_state.pending_task_ids.iter().position(|&id| id == task_id) {
            run_state.current_task_index = idx;
        }
        if let Some(selected_id) = run_state.mappings.get(&task_id).cloned() {
            run_state.statuses.insert(selected_id, "running".to_string());
        }
        return (Applied::Started(task_id), None);
    }
    if let Some(selected_id) = run_state.mappings.get(&task_id).cloned() {
        let status = if is_succeeded { "succeeded" } else { "failed" };
        run_state.statuses.insert(selected_id, status.to_string());
    }
    let all_done = run_state.pending_task_ids.iter().all(|id| {
        run_state
            .mappings
            .get(id)
            .and_then(|selected_id| run_state.statuses.get(selected_id))
            .is_some_and(|s| s == "succeeded" || s == "failed")
    });
    let mut job = None;
    if all_done {
        let failed = run_state.statuses.values().any(|s| s == "failed");
        run_state.overall_status = Some(if failed { "Failed" } else { "Succeeded" }.to_string());
        instance.task_ids.clear();
        drop(instances);
        job = Some(begin_finishing(slot, &mut inner, token, "tasks-completed"));
    }
    (Applied::Finished { task_id, succeeded: is_succeeded, all_done }, job)
}

/// 未完成的任务记为失败，整轮失败。
fn fail_unfinished(instance: &mut InstanceRuntime) {
    let run_state = &mut instance.task_run_state;
    for status in run_state.statuses.values_mut() {
        if status == "pending" || status == "running" {
            *status = "failed".to_string();
        }
    }
    run_state.overall_status = Some("Failed".to_string());
    instance.task_ids.clear();
}

// ============================================================================
// 收尾
// ============================================================================

/// 运行中的这一轮转为收尾，取出它的句柄。调用方持有槽锁，并在锁外启动返回的收尾工作。
fn begin_finishing(slot: &AndroidRunSlot, inner: &mut SlotInner, token: u64, kind: &'static str) -> FinishJob {
    let instance_id = inner.phase.instance_id().unwrap_or_default().to_string();
    inner.phase = Phase::Finishing { token, instance_id: instance_id.clone(), released: false };
    let handles = inner.run.take().unwrap_or_else(|| RunHandles {
        tasker: None,
        client: None,
        child: None,
        events: Arc::new(NoEvents),
    });
    FinishJob {
        slot: Arc::clone(&slot.inner),
        lock_dir: slot.lock_dir(),
        timing: slot.timing,
        token,
        instance_id,
        handles,
        kind,
    }
}

struct NoEvents;

impl RunEvents for NoEvents {
    fn state_changed(&self, _: &str, _: &str) {}
    fn message(&self, _: &str, _: &str) {}
}

/// 一轮的收尾：停下 Tasker（有期限）、断开 agent、等子进程退出（超时强杀）、等 `owner.lock` 能拿到（有期限），
/// 之后才让槽回到空闲。锁一直拿不到时槽留在收尾（之后的开始继续被拒绝，这是安全的结果）。
pub(crate) struct FinishJob {
    slot: Arc<Mutex<SlotInner>>,
    lock_dir: Result<PathBuf, String>,
    timing: Timing,
    token: u64,
    instance_id: String,
    handles: RunHandles,
    kind: &'static str,
}

impl FinishJob {
    fn spawn(self) {
        let name = format!("android-finish-{}", self.token);
        if let Err(e) = std::thread::Builder::new().name(name).spawn(move || self.run()) {
            warn!("[android_run] could not start the finishing thread: {}", e);
        }
    }

    pub(crate) fn run(self) {
        let FinishJob { slot, lock_dir, timing, token, instance_id, handles, kind } = self;
        let RunHandles { tasker, client, child, events } = handles;
        if let Some(tasker) = &tasker {
            if tasker.running() {
                let _ = tasker.post_stop();
                let deadline = Instant::now() + timing.tasker_stop;
                while tasker.running() && Instant::now() < deadline {
                    std::thread::sleep(timing.poll);
                }
                if tasker.running() {
                    warn!("[android_run] the tasker of run {} did not stop in time", token);
                }
            }
        }
        if let Some(client) = client {
            super::maa_agent::disconnect_agent(&client);
            drop(client);
        }
        if let Some(child) = child {
            reap_child(child, timing);
        }
        drop(tasker);

        let deadline = Instant::now() + timing.lock_wait;
        let lock_free = loop {
            let free = match &lock_dir {
                Ok(dir) => super::android_input::owner_lock_free(dir),
                Err(e) => Err(e.clone()),
            };
            match free {
                Ok(true) => break true,
                Ok(false) => {}
                Err(e) => warn!("[android_run] cannot check owner.lock: {}", e),
            }
            if Instant::now() >= deadline {
                break false;
            }
            std::thread::sleep(timing.poll);
        };
        {
            let mut inner = lock(&slot);
            if let Phase::Finishing { token: current, released, .. } = &mut inner.phase {
                if *current == token {
                    if lock_free {
                        inner.phase = Phase::Idle;
                    } else {
                        *released = true;
                    }
                }
            }
        }
        if lock_free {
            info!("[android_run] run {} on {} finished", token, instance_id);
        } else {
            warn!("[android_run] run {} on {}: owner.lock is still held after the agent exited", token, instance_id);
            events.message(&instance_id, OWNER_LOCK_BUSY);
        }
        events.state_changed(&instance_id, kind);
    }
}

/// 等子进程自己退出（有期限），超时强杀并回收。
fn reap_child(mut child: Child, timing: Timing) {
    let deadline = Instant::now() + timing.child_exit;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => return,
            Ok(None) if Instant::now() < deadline => std::thread::sleep(timing.poll),
            Ok(None) => break,
            Err(e) => {
                warn!("[android_run] error waiting for the agent: {}", e);
                break;
            }
        }
    }
    warn!("[android_run] the agent did not exit in time, killing it");
    let _ = child.kill();
    let _ = child.wait();
}

/// 窗口关闭时：结束这一轮的 agent 子进程（共用的 cleanup_all_agent_children 只看实例上的子进程）。
pub fn kill_on_exit(state: &MaaState) {
    let run = lock(&state.android_run.inner).run.take();
    if let Some(RunHandles { client, child, tasker, .. }) = run {
        if let Some(client) = &client {
            super::maa_agent::disconnect_agent(client);
        }
        if let Some(mut child) = child {
            let _ = child.kill();
            let _ = child.wait();
        }
        drop(client);
        drop(tasker);
    }
}

#[cfg(test)]
mod tests;
