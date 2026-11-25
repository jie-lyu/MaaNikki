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
pub const NOTHING_POSTED: &str = "安卓任务一个也没能提交（模拟器可能已经断开），这一轮已结束；请检查连接后重新开始。";
pub const QUITTING: &str = "MaaNikki 正在退出，不再开始安卓任务。";
pub const DEVICE_BUSY: &str =
    "另一个 MaaNikki 进程正在驱动这台模拟器（例如开发用的运行器，或另一个 MaaNikki 客户端），请等它结束后再开始。";

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
    /// 停止的握手里等已经预留的触摸做完（`touch.json` 消失）的期限和间隔。
    pub touch_wait: Duration,
    pub touch_poll: Duration,
    pub tasker_stop: Duration,
    pub child_exit: Duration,
    pub lock_wait: Duration,
    pub poll: Duration,
    /// How long quitting the client waits for a finishing run to reach the point where no touch can be cut.
    pub exit_wait: Duration,
}

impl Default for Timing {
    fn default() -> Self {
        Timing {
            touch_wait: Duration::from_secs(5),
            touch_poll: Duration::from_millis(50),
            tasker_stop: Duration::from_secs(10),
            child_exit: Duration::from_secs(5),
            lock_wait: Duration::from_secs(10),
            poll: Duration::from_millis(100),
            exit_wait: Duration::from_secs(10),
        }
    }
}

/// What finishing needs of a run's Tasker (a fake in the unit tests).
pub(crate) trait RunTasker: Send {
    fn running(&self) -> bool;
    fn post_stop(&self);
}

impl RunTasker for Tasker {
    fn running(&self) -> bool {
        Tasker::running(self)
    }
    fn post_stop(&self) {
        let _ = Tasker::post_stop(self);
    }
}

/// The agent child of a finishing run, shared by its finishing worker and an exit (kill_on_exit) that may have to
/// kill it first. None once it has been reaped.
type SharedChild = Arc<Mutex<Option<Child>>>;

fn lock_child(agent: &Mutex<Option<Child>>) -> MutexGuard<'_, Option<Child>> {
    agent.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

/// Kills the agent child now (if it has not been reaped yet) and reaps it.
fn kill_agent(agent: &Mutex<Option<Child>>) {
    if let Some(mut child) = lock_child(agent).take() {
        let _ = child.kill();
        let _ = child.wait();
    }
}

/// A finishing worker's progress, read by an exit during Finishing.
struct WorkerProgress {
    token: u64,
    /// No touch can be cut any more: the run's tasker was not running, or the reserved touch was gone after the stop
    /// request, or the agent was killed and the tasker then stopped by itself.
    settled: bool,
    agent: SharedChild,
}

/// 这一轮向前端发出的通知（生产环境发给 Tauri 和 WebSocket，测试里记录下来）。
pub(crate) trait RunEvents: Send + Sync {
    fn state_changed(&self, instance_id: &str, kind: &str);
    fn message(&self, instance_id: &str, line: &str);
    /// 一个提交了、但这一轮结束时还没有结束回调的任务：从任务统计里注销（开始过的记一次失败的运行，没开始的
    /// 直接丢掉）。每个任务至多一次（结束之后迟到的回调一律忽略）。遥测按整轮结束（on_run_cancelled/finished）。
    fn retire(&self, instance_id: &str, task_id: i64, started: bool);
}

struct AppEvents(tauri::AppHandle);

impl RunEvents for AppEvents {
    fn state_changed(&self, instance_id: &str, kind: &str) {
        super::utils::emit_state_changed(&self.0, instance_id, kind);
    }
    fn message(&self, instance_id: &str, line: &str) {
        super::maa_agent::emit_agent_output(&self.0, instance_id, "stderr", line);
    }
    fn retire(&self, instance_id: &str, task_id: i64, started: bool) {
        if started {
            super::task_stats::on_task_finished(&self.0, instance_id, task_id, false);
        } else {
            super::task_stats::discard_posted_task(instance_id, task_id);
        }
    }
}

/// 这一轮自己的句柄。
pub(crate) struct RunHandles {
    pub tasker: Option<Box<dyn RunTasker>>,
    pub client: Option<AgentClient>,
    pub child: Option<Child>,
    pub events: Arc<dyn RunEvents>,
}

/// 运行中这一轮的任务：提交了哪些、哪些开始过、哪些有了结束回调（收尾时据此注销其余的任务统计）。
#[derive(Default)]
struct RunProgress {
    posted: Vec<i64>,
    started: HashSet<i64>,
    finished: HashSet<i64>,
}

impl RunProgress {
    /// 还没有结束回调的任务（任务 ID，是否开始过），按提交顺序；取出后清空，所以每个任务只注销一次。
    fn take_unfinished(&mut self) -> Vec<(i64, bool)> {
        let progress = std::mem::take(self);
        progress
            .posted
            .into_iter()
            .filter(|id| !progress.finished.contains(id))
            .map(|id| (id, progress.started.contains(&id)))
            .collect()
    }
}

#[derive(Default)]
struct SlotInner {
    phase: Phase,
    run: Option<RunHandles>,
    progress: RunProgress,
    /// 每个实例正在进行的连接数（connect_controller_impl 的入口登记，返回时注销）。
    connecting: HashMap<String, usize>,
    next_token: u64,
    /// The finishing worker of the run being finished (None once it is done).
    worker: Option<WorkerProgress>,
    /// Set when the client starts quitting (kill_on_exit): no Android start is admitted or committed any more.
    closed: bool,
    /// The host-wide device lock (android_input::take_device_lock) of the emulator this run drives, taken at the claim
    /// and held until the slot is Idle again: the developer runner (scripts/android/runner.py) and other clients take
    /// the same lock, so only one MaaNikki process drives an emulator at a time.
    device_lock: Option<std::fs::File>,
}

impl SlotInner {
    /// Back to Idle; the device lock is released with it.
    fn to_idle(&mut self) {
        self.phase = Phase::Idle;
        self.device_lock = None;
    }
}

/// 整个进程唯一的安卓运行槽（放在 `MaaState` 里）。
pub struct AndroidRunSlot {
    inner: Arc<Mutex<SlotInner>>,
    /// `owner.lock` 所在目录；None 时用数据目录下的 config/android-input（测试里指向临时目录）。
    lock_dir: Option<PathBuf>,
    /// Where the device lock lives; None: the fixed android_input::DEVICE_LOCK_DIR (a test directory in the unit tests).
    device_lock_dir: Option<PathBuf>,
    timing: Timing,
    /// 取得槽时对实例连接的检查（单元测试没有原生的控制器句柄，换成只看配置的检查）。
    connection_check: fn(&MaaState, &str) -> Result<(), String>,
}

impl Default for AndroidRunSlot {
    fn default() -> Self {
        AndroidRunSlot {
            inner: Arc::default(),
            lock_dir: None,
            device_lock_dir: None,
            timing: Timing::default(),
            connection_check: instance_is_connected_adb,
        }
    }
}

fn lock(inner: &Mutex<SlotInner>) -> MutexGuard<'_, SlotInner> {
    // 槽里的状态在每个临界区结束时都是一致的；某个线程在持锁时 panic 也不应让安卓运行永远拒绝。
    inner.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

impl AndroidRunSlot {
    #[cfg(test)]
    pub(crate) fn for_tests(
        lock_dir: PathBuf,
        timing: Timing,
        connection_check: fn(&MaaState, &str) -> Result<(), String>,
    ) -> Self {
        AndroidRunSlot {
            inner: Arc::default(),
            device_lock_dir: Some(lock_dir.clone()),
            lock_dir: Some(lock_dir),
            timing,
            connection_check,
        }
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
            inner.to_idle();
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
/// 直接点击发往 Adb 控制器时拒绝。post_click_impl 在选出要发送的控制器的同一把实例锁里调用，所以检查和发送之间
/// 连接不会换成 Adb（配置和句柄在同一把锁里一起发布）。
pub fn refuse_click_on(instance: &InstanceRuntime) -> Result<(), String> {
    if matches!(instance.controller_config, Some(ControllerConfig::Adb { .. })) {
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
                // First, before Stop returns and before any statistics I/O: begin_finishing publishes stop.request,
                // after which the agent sends no new touch.
                let job = begin_finishing(slot, &mut inner, token, "task-stopped");
                match state.instances.lock() {
                    Ok(mut instances) => {
                        if let Some(instance) = instances.get_mut(instance_id) {
                            fail_unfinished(instance);
                        }
                    }
                    Err(e) => warn!("[android_run] instances lock poisoned while stopping: {}", e),
                }
                Some(job)
            }
            Phase::Finishing { .. } | Phase::Idle => None,
        }
    };
    if let Some(job) = job {
        // A stop request that could not be written does not keep the agent from reserving another touch: the agent
        // child is killed here, before Stop returns and before any statistics I/O (outside the slot lock).
        job.kill_agent_if_unrequested();
        super::telemetry::on_run_cancelled(instance_id);
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
    let app_for_output = app.clone();
    let id_for_output = instance_id.clone();
    // 槽在任何等待或派生任务之前同步取得：从这一刻起，对这个实例的停止都会落到这一轮（取消它），不会走共用的
    // 停止路径而被这次开始错过。
    let claimed = batch_refusal(&tasks, agent_configs.as_deref(), reset_state)
        .map(str::to_string)
        .or_else(|| refusals.iter().all(Option::is_some).then(|| refusals.iter().flatten().next().cloned().unwrap_or_default()))
        .map_or_else(|| claim(maa_state, &instance_id), Err);
    let result = match claimed {
        Err(reason) => Err(reason),
        Ok(claim) => tauri::async_runtime::spawn(async move {
            start_owned(app, claim, tasks, agent_configs, cwd, tcp_compat_mode, pi_envs, controller_info, refusals)
                .await
        })
        .await
        .map_err(|e| e.to_string())
        .and_then(|result| result),
    };
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
    claim: StartClaim,
    tasks: Vec<TaskConfig>,
    agent_configs: Option<Vec<AgentConfig>>,
    cwd: String,
    tcp_compat_mode: bool,
    pi_envs: Option<HashMap<String, String>>,
    controller_info: Option<ControllerInfo>,
    refusals: Vec<Option<String>>,
) -> Result<Vec<i64>, String> {
    let state = Arc::clone(&claim.state);
    let instance_id = claim.instance_id.clone();
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

    let handles =
        RunHandles { tasker: Some(Box::new(tasker.clone())), client: Some(client), child: Some(child), events };
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
            // An invalid id (0: the framework refused the post, e.g. the emulator went away) counts as not posted.
            Ok(job) if job.id <= 0 => {
                warn!("[android_run] posting {} returned the invalid task id {}", task.entry, job.id);
                run_state.statuses.insert(selected_id, "failed".to_string());
            }
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

/// 取得槽：只有在空闲、客户端没有在退出、这个实例没有进行中的连接、它当前的控制器是已连接的 Adb、`owner.lock` 此刻
/// 能拿到（拿到后立即放开），而且这台模拟器的设备锁能拿到（一直拿着，直到槽回到空闲）时。取得之后删掉上一轮留下的停止请求（此刻没有这个数据目录的 agent 在运行：槽空闲、锁空着）。
/// 同步执行，开始的路径在任何等待之前调用它。
fn claim(state: &Arc<MaaState>, instance_id: &str) -> Result<StartClaim, String> {
    let slot = &state.android_run;
    let mut inner = lock(&slot.inner);
    if inner.closed {
        return Err(QUITTING.to_string());
    }
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
    // 槽锁 → 实例锁：连接在发布控制器之前一直登记着（见 enter_connect），所以这里看到的就是这一轮要用的连接。
    (slot.connection_check)(state, instance_id)?;
    let dir = slot.lock_dir()?;
    match super::android_input::owner_lock_free(&dir) {
        Ok(true) => {}
        Ok(false) => return Err(OWNER_LOCK_BUSY.to_string()),
        Err(e) => return Err(format!("无法检查安卓模拟器的输入锁：{}", e)),
    }
    // The host-wide device lock: refused while another MaaNikki process (the developer runner, another client) drives
    // this emulator. Held until the slot is Idle again.
    let serial = adb_serial(state, instance_id).ok_or(NOT_CONNECTED)?;
    let device_dir =
        slot.device_lock_dir.clone().unwrap_or_else(|| PathBuf::from(super::android_input::DEVICE_LOCK_DIR));
    let device_lock = match super::android_input::take_device_lock(&device_dir, &serial) {
        Ok(Some(file)) => file,
        Ok(None) => return Err(DEVICE_BUSY.to_string()),
        Err(e) => return Err(format!("无法独占这台安卓模拟器：{}", e)),
    };
    super::android_input::clear_stop_request(&dir)?;
    inner.device_lock = Some(device_lock);
    inner.next_token += 1;
    let token = inner.next_token;
    inner.phase = Phase::Starting { token, instance_id: instance_id.to_string(), cancelled: false };
    Ok(StartClaim { state: Arc::clone(state), instance_id: instance_id.to_string(), token })
}

/// The ADB serial (address) of the instance's Adb controller configuration.
fn adb_serial(state: &MaaState, instance_id: &str) -> Option<String> {
    let instances = state.instances.lock().ok()?;
    match &instances.get(instance_id)?.controller_config {
        Some(ControllerConfig::Adb { address, .. }) => Some(address.clone()),
        _ => None,
    }
}

/// 实例当前的控制器是已连接、没有因停止而待重连的 Adb 控制器（配置和句柄在同一把锁里一起发布）。
pub(crate) fn instance_is_connected_adb(state: &MaaState, instance_id: &str) -> Result<(), String> {
    let instances = state.instances.lock().map_err(|e| e.to_string())?;
    let instance = instances.get(instance_id).ok_or("Instance not found")?;
    let adb = matches!(instance.controller_config, Some(ControllerConfig::Adb { .. }));
    if !adb || instance.controller_stopped || !instance.controller.as_ref().is_some_and(|c| c.connected()) {
        return Err(NOT_CONNECTED.to_string());
    }
    Ok(())
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
        let inner = lock(&self.state.android_run.inner);
        inner.closed || matches!(inner.phase, Phase::Starting { cancelled: true, .. })
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
        // A client that started quitting meanwhile (kill_on_exit) admits no commit either.
        let owned = !inner.closed
            && matches!(&inner.phase, Phase::Starting { token, cancelled: false, .. } if *token == self.token);
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
                inner.progress = RunProgress { posted: ids.clone(), ..RunProgress::default() };
                (Ok(ids), None)
            }
            // Every post was refused (e.g. the emulator went away): an error, so the start is not reported as a success.
            Ok(_) => (Err(NOTHING_POSTED.to_string()), Some(self.finish_with(&mut inner, handles, "tasks-completed"))),
            Err(reason) => (Err(reason), Some(self.finish_with(&mut inner, handles, "task-stopped"))),
        }
    }

    fn finish_with(&self, inner: &mut SlotInner, handles: RunHandles, kind: &'static str) -> FinishJob {
        let slot = &self.state.android_run;
        if matches!(&inner.phase, Phase::Starting { token, .. } if *token == self.token) {
            inner.phase =
                Phase::Finishing { token: self.token, instance_id: self.instance_id.clone(), released: false };
        }
        // Nothing was posted, so nothing is unfinished; the worker publishes the stop request itself if the tasker
        // is running after all.
        FinishJob::new(slot, inner, self.token, self.instance_id.clone(), handles, kind, Vec::new(), None)
    }
}

impl Drop for StartClaim {
    fn drop(&mut self) {
        let mut inner = lock(&self.state.android_run.inner);
        if matches!(&inner.phase, Phase::Starting { token, .. } if *token == self.token) {
            inner.to_idle();
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
        inner.progress.started.insert(task_id);
        return (Applied::Started(task_id), None);
    }
    inner.progress.finished.insert(task_id);
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

/// Turns the running run into Finishing, takes its handles and publishes stop.request right away: before Stop returns
/// and before any statistics I/O; from then on the agent starts no new touch. The caller holds the slot lock and spawns
/// the returned job outside it.
fn begin_finishing(slot: &AndroidRunSlot, inner: &mut SlotInner, token: u64, kind: &'static str) -> FinishJob {
    let instance_id = inner.phase.instance_id().unwrap_or_default().to_string();
    inner.phase = Phase::Finishing { token, instance_id: instance_id.clone(), released: false };
    let handles = inner.run.take().unwrap_or_else(|| RunHandles {
        tasker: None,
        client: None,
        child: None,
        events: Arc::new(NoEvents),
    });
    let request = publish_stop_request(&slot.lock_dir());
    let unfinished = inner.progress.take_unfinished();
    FinishJob::new(slot, inner, token, instance_id, handles, kind, unfinished, Some(request))
}

fn publish_stop_request(lock_dir: &Result<PathBuf, String>) -> Result<(), String> {
    let result = match lock_dir {
        Ok(dir) => super::android_input::write_stop_request(dir),
        Err(e) => Err(format!("cannot request the agent to stop: {}", e)),
    };
    if let Err(e) = &result {
        warn!("[android_run] {}", e);
    }
    result
}

pub const AGENT_TERMINATED_AT_STOP: &str = "停止时 agent 还有一次触摸没有确认完成（或者停止请求写不进去）：为了不切断正在进行的点击，\
没有调用框架的停止，改为结束 agent 子进程、等任务自己停下。留下的触摸标记会挡住之后的触摸，请检查模拟器。";
pub const TASKER_STILL_RUNNING_AT_STOP: &str = "结束 agent 之后任务没有在 10 秒内自己停下：仍然没有调用框架的停止，这一轮停在收尾，\
任务停下之前不能再开始（一直停不下来就重启 MaaNikki）。请检查模拟器上是否留下按住的触点。";

/// Waits (bounded) for a reserved touch to finish (`touch.json` gone). True when the marker is gone.
fn wait_for_touch_release(dir: &std::path::Path, timing: Timing) -> bool {
    let deadline = Instant::now() + timing.touch_wait;
    loop {
        if !super::android_input::touch_reserved(dir) {
            return true;
        }
        if Instant::now() >= deadline {
            return false;
        }
        std::thread::sleep(timing.touch_poll);
    }
}

struct NoEvents;

impl RunEvents for NoEvents {
    fn state_changed(&self, _: &str, _: &str) {}
    fn message(&self, _: &str, _: &str) {}
    fn retire(&self, _: &str, _: i64, _: bool) {}
}

/// Finishing a run: stop the Tasker without cutting a touch, disconnect the agent, wait for its process to exit (killed
/// after a bound), wait (bounded) until `owner.lock` is free, and only then return the slot to Idle. A lock that stays
/// held leaves the slot Finishing (later starts keep being refused, the safe outcome).
///
/// The two ways the Tasker is stopped (the handshake of android_input::write_stop_request):
/// - The stop request was written and a reserved touch finished within the bound (`touch.json` gone): the agent sends
///   nothing once it sees the request after reserving, so `post_stop` cannot cut a touch.
/// - The marker outlived the bound, or the request was not written: `post_stop` is never called (the framework's stop
///   sends UP first and the in-flight click then sends its DOWN, leaving a contact held). The agent child is killed
///   instead: touches run in this process's controller (the agent calls run_action_direct through the AgentClient),
///   so a click already handed over finishes whole here and none can start once the agent is gone; disconnecting the
///   client lets the pending custom action return and the task (an entry node without next nodes) ends by itself. If
///   it is still running after the bound the slot stays Finishing and keeps waiting, still without `post_stop`.
pub(crate) struct FinishJob {
    slot: Arc<Mutex<SlotInner>>,
    lock_dir: Result<PathBuf, String>,
    timing: Timing,
    token: u64,
    instance_id: String,
    tasker: Option<Box<dyn RunTasker>>,
    client: Option<AgentClient>,
    agent: SharedChild,
    events: Arc<dyn RunEvents>,
    kind: &'static str,
    /// Posted tasks without an end callback yet (task id, whether it started), retired from the task statistics.
    unfinished: Vec<(i64, bool)>,
    /// Whether stop.request was written (None: not yet; the worker writes it first if the Tasker is running).
    stop_request: Option<Result<(), String>>,
}

impl FinishJob {
    /// The caller holds the slot lock; registers this job's progress in the slot (an exit waits on it).
    #[allow(clippy::too_many_arguments)]
    fn new(
        slot: &AndroidRunSlot,
        inner: &mut SlotInner,
        token: u64,
        instance_id: String,
        handles: RunHandles,
        kind: &'static str,
        unfinished: Vec<(i64, bool)>,
        stop_request: Option<Result<(), String>>,
    ) -> FinishJob {
        let RunHandles { tasker, client, child, events } = handles;
        let agent: SharedChild = Arc::new(Mutex::new(child));
        inner.worker = Some(WorkerProgress { token, settled: false, agent: Arc::clone(&agent) });
        FinishJob {
            slot: Arc::clone(&slot.inner),
            lock_dir: slot.lock_dir(),
            timing: slot.timing,
            token,
            instance_id,
            tasker,
            client,
            agent,
            events,
            kind,
            unfinished,
            stop_request,
        }
    }

    /// When stop.request could not be written, kills the agent child now (the worker would otherwise do it only after
    /// retiring the statistics). A no-op when the request was written or not attempted yet.
    fn kill_agent_if_unrequested(&self) {
        if matches!(self.stop_request, Some(Err(_))) {
            warn!("[android_run] run {}: the stop request was not written; killing the agent now", self.token);
            kill_agent(&self.agent);
        }
    }

    fn spawn(self) {
        let name = format!("android-finish-{}", self.token);
        if let Err(e) = std::thread::Builder::new().name(name).spawn(move || self.run()) {
            warn!("[android_run] could not start the finishing thread: {}", e);
        }
    }

    fn mark_settled(slot: &Mutex<SlotInner>, token: u64) {
        if let Some(worker) = lock(slot).worker.as_mut().filter(|w| w.token == token) {
            worker.settled = true;
        }
    }

    pub(crate) fn run(self) {
        let FinishJob {
            slot,
            lock_dir,
            timing,
            token,
            instance_id,
            tasker,
            client,
            agent,
            events,
            kind,
            unfinished,
            stop_request,
        } = self;
        // The stop request goes out before any statistics I/O (begin_finishing has already written it for a run that
        // was running).
        let request = match tasker.as_deref() {
            Some(t) if t.running() => Some(stop_request.unwrap_or_else(|| publish_stop_request(&lock_dir))),
            _ => None,
        };
        for (task_id, started) in unfinished {
            events.retire(&instance_id, task_id, started);
        }
        let mut agent_terminated = false;
        match (tasker.as_deref(), request) {
            (Some(tasker), Some(request)) => {
                let released = match (&request, &lock_dir) {
                    (Ok(()), Ok(dir)) => wait_for_touch_release(dir, timing),
                    _ => false,
                };
                if released {
                    info!("[android_run] run {}: stop path post_stop (no touch reserved after the stop request)", token);
                    Self::mark_settled(&slot, token);
                    tasker.post_stop();
                    let deadline = Instant::now() + timing.tasker_stop;
                    while tasker.running() && Instant::now() < deadline {
                        std::thread::sleep(timing.poll);
                    }
                    if tasker.running() {
                        warn!("[android_run] the tasker of run {} did not stop in time", token);
                    }
                } else {
                    warn!(
                        "[android_run] run {}: stop path agent-terminated, no post_stop ({})",
                        token,
                        if request.is_ok() { "a touch was still reserved" } else { "the stop request was not written" }
                    );
                    events.message(&instance_id, AGENT_TERMINATED_AT_STOP);
                    agent_terminated = true;
                    kill_agent(&agent);
                    if let Some(client) = &client {
                        // Bounds the custom action call the tasker may be waiting in, so the task can end.
                        super::maa_agent::disconnect_agent(client);
                    }
                    let deadline = Instant::now() + timing.tasker_stop;
                    while tasker.running() && Instant::now() < deadline {
                        std::thread::sleep(timing.poll);
                    }
                    if tasker.running() {
                        warn!("[android_run] run {}: the tasker is still running after the agent was killed; \
                               staying in Finishing without post_stop", token);
                        events.message(&instance_id, TASKER_STILL_RUNNING_AT_STOP);
                        while tasker.running() {
                            std::thread::sleep(timing.poll);
                        }
                    }
                    info!("[android_run] run {}: the tasker stopped on its own after the agent was killed", token);
                    Self::mark_settled(&slot, token);
                }
            }
            _ => Self::mark_settled(&slot, token),
        }
        if let Some(client) = client {
            if !agent_terminated {
                super::maa_agent::disconnect_agent(&client);
            }
            drop(client);
        }
        reap_child(&agent, timing);
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
            if inner.worker.as_ref().is_some_and(|w| w.token == token) {
                inner.worker = None;
            }
            if let Phase::Finishing { token: current, released, .. } = &mut inner.phase {
                if *current == token {
                    if lock_free {
                        inner.to_idle();
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

/// Waits (bounded) for the agent to exit by itself, then kills and reaps it.
fn reap_child(agent: &Mutex<Option<Child>>, timing: Timing) {
    let deadline = Instant::now() + timing.child_exit;
    loop {
        {
            let mut guard = lock_child(agent);
            let Some(child) = guard.as_mut() else { return };
            match child.try_wait() {
                Ok(Some(_)) => {
                    *guard = None;
                    return;
                }
                Ok(None) if Instant::now() < deadline => {}
                Ok(None) => break,
                Err(e) => {
                    warn!("[android_run] error waiting for the agent: {}", e);
                    break;
                }
            }
        }
        std::thread::sleep(timing.poll);
    }
    warn!("[android_run] the agent did not exit in time, killing it");
    kill_agent(agent);
}

/// Quitting the client (window destroyed, tray Quit): never exit in the middle of a touch. A running run takes the same
/// finishing as a Stop (stop request first, then one of the two ways above); until its worker reaches the point where
/// no touch can be cut (`touch.json` gone after the stop request, or the agent gone and the Tasker stopped), this waits,
/// bounded by `exit_wait`, then kills the agent child and lets the exit proceed. It never calls `post_stop` and never
/// drops the Tasker or the AgentClient (the worker owns them).
///
/// Quitting also closes Android start admission (no claim, no commit from now on) and cancels a run still starting, so
/// a start whose agent handshake is still pending can never post its tasks after Quit; the exit waits (within the same
/// bound) for such a start to give its slot back or hand its agent to a finishing worker. Past the bound an agent of a
/// start still in its handshake is not reached here (risk 24).
pub fn kill_on_exit(state: &MaaState) {
    let slot = &state.android_run;
    let job = {
        let mut inner = lock(&slot.inner);
        inner.closed = true;
        match &mut inner.phase {
            Phase::Starting { token, instance_id, cancelled } => {
                info!("[android_run] exiting while run {} on {} is starting: the start is cancelled", token, instance_id);
                *cancelled = true;
                None
            }
            Phase::Running { token, instance_id } => {
                let token = *token;
                info!("[android_run] exiting while run {} on {} is running: finishing it first", token, instance_id);
                Some(begin_finishing(slot, &mut inner, token, "task-stopped"))
            }
            Phase::Finishing { .. } | Phase::Idle => None,
        }
    };
    if let Some(job) = job {
        job.kill_agent_if_unrequested();
        job.spawn();
    }
    let deadline = Instant::now() + slot.timing.exit_wait;
    loop {
        let (settled, agent) = {
            let inner = lock(&slot.inner);
            match inner.worker.as_ref() {
                Some(worker) => (worker.settled, Some(Arc::clone(&worker.agent))),
                None => (!matches!(inner.phase, Phase::Starting { .. }), None),
            }
        };
        if settled || Instant::now() >= deadline {
            if !settled {
                warn!("[android_run] exiting before the finishing run reached a point where no touch can be cut");
            }
            if let Some(agent) = agent {
                kill_agent(&agent);
            }
            return;
        }
        std::thread::sleep(slot.timing.poll);
    }
}

#[cfg(test)]
mod tests;
