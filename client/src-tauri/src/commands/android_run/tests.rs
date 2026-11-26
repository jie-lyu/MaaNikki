//! The Android run slot: one test per refusal rule, the state machine (token filter, stop while starting,
//! Finishing → Idle only after the agent exited and owner.lock is free), and the entry branches in the shared
//! functions leaving Win32 and MacOS instances to the shared code with its observable results unchanged.

use super::*;
use std::os::unix::io::AsRawFd;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};

const A: &str = "android";
const B: &str = "other-android";
const W: &str = "windows";
const M: &str = "mac";

fn timing() -> Timing {
    Timing {
        touch_wait: Duration::from_millis(300),
        touch_poll: Duration::from_millis(10),
        tasker_stop: Duration::from_millis(200),
        child_exit: Duration::from_millis(1500),
        lock_wait: Duration::from_millis(400),
        poll: Duration::from_millis(10),
        exit_wait: Duration::from_millis(1500),
    }
}

fn temp_dir() -> PathBuf {
    static NEXT: AtomicU64 = AtomicU64::new(0);
    let dir = std::env::temp_dir().join(format!(
        "maanikki-android-run-{}-{}",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::Relaxed)
    ));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

fn adb() -> ControllerConfig {
    ControllerConfig::Adb {
        adb_path: "adb".into(),
        address: "127.0.0.1:5555".into(),
        screencap_methods: "18446744073709551559".into(),
        input_methods: "7".into(),
        config: "{\"extras\":{}}".into(),
        display_short_side: Some(720),
    }
}

fn win32() -> ControllerConfig {
    ControllerConfig::Win32 {
        handle: 1,
        screencap_method: 8,
        screencap_fallbacks: vec![2],
        mouse_method: 1,
        keyboard_method: 1,
        display_short_side: None,
    }
}

fn macos() -> ControllerConfig {
    ControllerConfig::MacOS { handle: 7, screencap_method: 1, input_method: 1, display_short_side: None }
}

/// A state whose owner.lock lives in a fresh temporary directory, with an Adb instance A and B, a Win32
/// instance W and a MacOS instance M (configurations only: no native handles in unit tests).
fn state() -> (Arc<MaaState>, PathBuf) {
    state_in(temp_dir())
}

fn state_in(dir: PathBuf) -> (Arc<MaaState>, PathBuf) {
    state_with(dir, |slot| slot)
}

fn state_with(dir: PathBuf, slot: impl FnOnce(AndroidRunSlot) -> AndroidRunSlot) -> (Arc<MaaState>, PathBuf) {
    let mut state = MaaState::default();
    state.android_run = slot(AndroidRunSlot::for_tests(dir.clone(), timing(), adb_configured));
    {
        let mut instances = state.instances.lock().unwrap();
        for (id, config) in [(A, adb()), (B, adb()), (W, win32()), (M, macos())] {
            let mut instance = InstanceRuntime::default();
            instance.controller_config = Some(config);
            instances.insert(id.to_string(), instance);
        }
    }
    (Arc::new(state), dir)
}

/// The claim's connection check without native handles: the instance's controller is an Adb one that is not
/// waiting for a reconnect (the production check also needs a connected handle; see the test for it).
fn adb_configured(state: &MaaState, instance_id: &str) -> Result<(), String> {
    let instances = state.instances.lock().unwrap();
    match instances.get(instance_id) {
        Some(instance)
            if matches!(instance.controller_config, Some(ControllerConfig::Adb { .. })) && !instance.controller_stopped =>
        {
            Ok(())
        }
        _ => Err(NOT_CONNECTED.to_string()),
    }
}

fn set_phase(state: &MaaState, phase: Phase) {
    lock(&state.android_run.inner).phase = phase;
}

fn hold_lock(dir: &Path) -> std::fs::File {
    let file = std::fs::OpenOptions::new().read(true).append(true).create(true).open(dir.join("owner.lock")).unwrap();
    assert_eq!(unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) }, 0);
    file
}

use std::path::Path;

#[derive(Default)]
struct Recorded(Mutex<Vec<(String, String)>>);

impl RunEvents for Recorded {
    fn state_changed(&self, instance_id: &str, kind: &str) {
        self.0.lock().unwrap().push((instance_id.to_string(), kind.to_string()));
    }
    fn message(&self, instance_id: &str, line: &str) {
        self.0.lock().unwrap().push((instance_id.to_string(), format!("message: {}", line)));
    }
    fn retire(&self, instance_id: &str, task_id: i64, started: bool) {
        self.0.lock().unwrap().push((instance_id.to_string(), format!("retire {} started={}", task_id, started)));
    }
}

impl Recorded {
    fn kinds(&self) -> Vec<String> {
        self.0.lock().unwrap().iter().map(|(_, kind)| kind.clone()).collect()
    }
}

/// Every test here takes or checks owner.lock or spawns processes, so they run one at a time: a process spawned on another thread while a
/// test holds the lock keeps the lock's open file description until it execs, so the lock can look held a
/// little after the test released it (see the report on this step).
fn serial() -> std::sync::MutexGuard<'static, ()> {
    crate::commands::android_input::serial_fd_tests()
}

fn sleeper(seconds: &str) -> Child {
    std::process::Command::new("sleep").arg(seconds).spawn().unwrap()
}

fn handles(child: Option<Child>, events: &Arc<Recorded>) -> RunHandles {
    RunHandles { tasker: None, client: None, child, events: Arc::clone(events) as Arc<dyn RunEvents> }
}

fn task(selected: Option<&str>, entry: &str) -> TaskConfig {
    TaskConfig {
        entry: entry.to_string(),
        pipeline_override: "{}".to_string(),
        selected_task_id: selected.map(str::to_string),
        task_name: None,
        options: None,
    }
}

fn agent() -> AgentConfig {
    AgentConfig { child_exec: "./python/python.exe".into(), child_args: None, identifier: None, timeout: None }
}

/// Puts the slot in Running(token) for instance A with two posted tasks (ids 11 and 12 for entries "x" and
/// "y"), the way post_tasks leaves the instance.
fn running(state: &Arc<MaaState>, token: u64, child: Option<Child>, events: &Arc<Recorded>) {
    {
        let mut instances = state.instances.lock().unwrap();
        let instance = instances.get_mut(A).unwrap();
        let run = &mut instance.task_run_state;
        run.pending_task_ids = vec![11, 12];
        run.mappings.insert(11, "x".into());
        run.mappings.insert(12, "y".into());
        run.statuses.insert("x".into(), "pending".into());
        run.statuses.insert("y".into(), "pending".into());
        run.overall_status = Some("Running".into());
        instance.task_ids = vec![11, 12];
    }
    let mut inner = lock(&state.android_run.inner);
    inner.phase = Phase::Running { token, instance_id: A.into() };
    inner.run = Some(handles(child, events));
    inner.progress = RunProgress { posted: vec![11, 12], ..RunProgress::default() };
}

fn status(state: &MaaState, selected: &str) -> String {
    state.instances.lock().unwrap()[A].task_run_state.statuses[selected].clone()
}

fn overall(state: &MaaState) -> Option<String> {
    state.instances.lock().unwrap()[A].task_run_state.overall_status.clone()
}

fn wait_for_idle(state: &MaaState) {
    let deadline = Instant::now() + Duration::from_secs(5);
    while state.android_run.phase() != Phase::Idle {
        assert!(Instant::now() < deadline, "still {:?}", state.android_run.phase());
        std::thread::sleep(Duration::from_millis(10));
    }
}

// ----------------------------------------------------------------------------
// Refusals at the start
// ----------------------------------------------------------------------------

#[test]
fn a_start_on_another_instance_is_refused_while_the_slot_is_busy() {
    let _serial = serial();
    let (state, _dir) = state();
    for phase in [
        Phase::Starting { token: 1, instance_id: B.into(), cancelled: false },
        Phase::Running { token: 1, instance_id: B.into() },
        Phase::Finishing { token: 1, instance_id: B.into(), released: false },
    ] {
        set_phase(&state, phase.clone());
        assert_eq!(claim(&state, A).err().as_deref(), Some(BUSY_OTHER_INSTANCE));
        assert_eq!(state.android_run.phase(), phase);
    }
}

#[test]
fn a_second_start_on_the_same_instance_is_refused_not_queued() {
    let _serial = serial();
    let (state, _dir) = state();
    for phase in [
        Phase::Starting { token: 1, instance_id: A.into(), cancelled: false },
        Phase::Running { token: 1, instance_id: A.into() },
    ] {
        set_phase(&state, phase.clone());
        assert_eq!(claim(&state, A).err().as_deref(), Some(ALREADY_RUNNING));
        assert_eq!(state.android_run.phase(), phase);
    }
}

#[test]
fn a_start_is_refused_while_the_last_run_is_finishing() {
    let _serial = serial();
    let (state, _dir) = state();
    let phase = Phase::Finishing { token: 1, instance_id: A.into(), released: false };
    set_phase(&state, phase.clone());
    assert_eq!(claim(&state, A).err().as_deref(), Some(STILL_FINISHING));
    assert_eq!(state.android_run.phase(), phase);
}

#[test]
fn a_start_is_refused_while_another_process_holds_owner_lock() {
    let _serial = serial();
    let (state, dir) = state();
    let holder = hold_lock(&dir);
    assert_eq!(claim(&state, A).err().as_deref(), Some(OWNER_LOCK_BUSY));
    assert_eq!(state.android_run.phase(), Phase::Idle);
    drop(holder);
    let claimed = claim(&state, A).unwrap();
    assert!(matches!(state.android_run.phase(), Phase::Starting { cancelled: false, .. }));
    drop(claimed);
}

#[test]
fn a_start_is_refused_while_a_connection_is_in_progress_on_the_instance() {
    let _serial = serial();
    let (state, _dir) = state();
    let (_, guard) = enter_connect(&state, A, adb()).unwrap();
    assert_eq!(claim(&state, A).err().as_deref(), Some(STILL_CONNECTING));
    // Another instance's connection does not matter.
    let (_, other) = enter_connect(&state, B, adb()).unwrap();
    drop(guard);
    let claimed = claim(&state, A).unwrap();
    drop(claimed);
    drop(other);
    assert_eq!(state.android_run.connecting(A), 0);
    assert_eq!(state.android_run.connecting(B), 0);
}

#[test]
fn a_batch_must_be_whole_from_the_task_list_unique_and_with_one_agent() {
    let _serial = serial();
    let one = [agent()];
    let ok = [task(Some("x"), "MaaNikki_AndroidCheck"), task(Some("y"), "MaaNikki_Mailbox")];
    assert_eq!(batch_refusal(&ok, Some(&one), true), None);
    assert_eq!(batch_refusal(&ok, Some(&one), false), Some(crate::commands::utils::ANDROID_APPEND_REFUSED));
    assert_eq!(batch_refusal(&[task(None, "MaaNikki_AndroidCheck")], Some(&one), true), Some(NOT_FROM_TASK_LIST));
    assert_eq!(batch_refusal(&[task(Some(""), "MaaNikki_AndroidCheck")], Some(&one), true), Some(NOT_FROM_TASK_LIST));
    let duplicate = [task(Some("x"), "MaaNikki_AndroidCheck"), task(Some("x"), "MaaNikki_Mailbox")];
    assert_eq!(batch_refusal(&duplicate, Some(&one), true), Some(DUPLICATE_ENTRIES));
    assert_eq!(batch_refusal(&ok, None, true), Some(ONE_AGENT_ONLY));
    assert_eq!(batch_refusal(&ok, Some(&[]), true), Some(ONE_AGENT_ONLY));
    assert_eq!(batch_refusal(&ok, Some(&[agent(), agent()]), true), Some(ONE_AGENT_ONLY));
}

#[test]
fn a_start_that_fails_before_the_agent_gives_the_slot_back() {
    let _serial = serial();
    let (state, _dir) = state();
    let claimed = claim(&state, A).unwrap();
    let token = claimed.token;
    assert_eq!(state.android_run.phase(), Phase::Starting { token, instance_id: A.into(), cancelled: false });
    drop(claimed);
    assert_eq!(state.android_run.phase(), Phase::Idle);
    // Each start takes a new token.
    let again = claim(&state, A).unwrap();
    assert!(again.token > token);
}

// ----------------------------------------------------------------------------
// Refusals while busy, in the shared functions
// ----------------------------------------------------------------------------

#[test]
fn every_operation_on_a_busy_instance_is_refused_with_a_reason() {
    let _serial = serial();
    let (state, _dir) = state();
    let ops = [
        Op::Connect, Op::Disconnect, Op::DestroyInstance, Op::LoadResource, Op::DestroyResource,
        Op::AppendTask, Op::ChangeOptions, Op::Click,
    ];
    for (phase, finishing) in [
        (Phase::Starting { token: 1, instance_id: A.into(), cancelled: false }, false),
        (Phase::Running { token: 1, instance_id: A.into() }, false),
        (Phase::Finishing { token: 1, instance_id: A.into(), released: false }, true),
    ] {
        set_phase(&state, phase);
        for op in ops {
            let reason = refuse_while_busy(&state, A, op).unwrap_err();
            assert!(!reason.is_empty());
            if finishing && !matches!(op, Op::AppendTask | Op::ChangeOptions | Op::Click) {
                assert!(reason.contains("收尾"), "{}", reason);
            }
            // Other instances are not affected.
            assert_eq!(refuse_while_busy(&state, B, op), Ok(()));
            assert_eq!(refuse_while_busy(&state, W, op), Ok(()));
        }
        assert!(enter_connect(&state, A, adb()).is_err());
        assert!(enter_connect(&state, A, macos()).is_err());  // a controller switch too
        assert_eq!(state.android_run.connecting(A), 0);
    }
    set_phase(&state, Phase::Idle);
    for op in ops {
        assert_eq!(refuse_while_busy(&state, A, op), Ok(()));
    }
}

#[test]
fn direct_clicks_are_refused_for_every_adb_instance_under_the_clicks_own_lock() {
    let _serial = serial();
    use crate::commands::maa_core::post_click_impl;
    let (state, _dir) = state();
    // The type is checked where the controller is picked (no native handle here: a desktop instance gets the
    // shared "not connected" answer, an Adb one the refusal before any controller is looked at).
    assert_eq!(post_click_impl(&state, A, 1, 1).err().as_deref(), Some(DIRECT_CLICK_REFUSED));
    assert_eq!(post_click_impl(&state, W, 1, 1).err().as_deref(), Some("Controller not connected"));
    assert_eq!(post_click_impl(&state, M, 1, 1).err().as_deref(), Some("Controller not connected"));
    assert_eq!(post_click_impl(&state, "missing", 1, 1).err().as_deref(), Some("Instance not found"));
    let instances = state.instances.lock().unwrap();
    assert_eq!(refuse_click_on(&instances[A]).err().as_deref(), Some(DIRECT_CLICK_REFUSED));
    assert_eq!(refuse_click_on(&instances[W]), Ok(()));
    assert_eq!(refuse_click_on(&instances[M]), Ok(()));
}

#[test]
fn no_screencap_is_posted_to_an_instance_while_its_android_run_holds_the_slot() {
    let _serial = serial();
    use crate::commands::maa_core::post_screencap_impl;
    let (state, _dir) = state();
    for phase in [
        Phase::Starting { token: 1, instance_id: A.into(), cancelled: false },
        Phase::Running { token: 1, instance_id: A.into() },
        Phase::Finishing { token: 1, instance_id: A.into(), released: false },
    ] {
        set_phase(&state, phase);
        assert!(screencap_held(&state, A));
        // Refused before the instance or its controller is looked at: the subscription and the one-shot command
        // serve the cached image instead.
        assert_eq!(post_screencap_impl(&state, A).err().as_deref(), Some(SCREENCAP_HELD));
        // Other instances (another emulator, Win32, MacOS) keep the shared path and its answers.
        for other in [B, W, M] {
            assert!(!screencap_held(&state, other));
            assert_eq!(post_screencap_impl(&state, other).err().as_deref(), Some("Controller not connected"));
        }
    }
    set_phase(&state, Phase::Idle);
    assert!(!screencap_held(&state, A));
    assert_eq!(post_screencap_impl(&state, A).err().as_deref(), Some("Controller not connected"));
    assert_eq!(post_screencap_impl(&state, "missing").err().as_deref(), Some("Instance not found"));
}

#[test]
fn a_finishing_slot_whose_lock_frees_later_becomes_idle_at_the_next_check() {
    let _serial = serial();
    let (state, dir) = state();
    let holder = hold_lock(&dir);
    set_phase(&state, Phase::Finishing { token: 3, instance_id: A.into(), released: true });
    assert!(refuse_while_busy(&state, A, Op::Connect).is_err());
    assert_eq!(claim(&state, A).err().as_deref(), Some(STILL_FINISHING));
    drop(holder);
    assert_eq!(refuse_while_busy(&state, A, Op::Connect), Ok(()));
    assert_eq!(state.android_run.phase(), Phase::Idle);
    // A run still releasing its handles (released: false) is never cut short.
    set_phase(&state, Phase::Finishing { token: 4, instance_id: A.into(), released: false });
    assert!(refuse_while_busy(&state, A, Op::Connect).is_err());
}

// ----------------------------------------------------------------------------
// The state machine
// ----------------------------------------------------------------------------

#[test]
fn a_stop_while_starting_cancels_and_the_start_finishes_its_own_agent() {
    let _serial = serial();
    let (state, _dir) = state();
    let events = Arc::new(Recorded::default());
    let claimed = claim(&state, A).unwrap();
    let token = claimed.token;
    assert_eq!(stop_if_busy(&state, A), Some(Ok(())));
    assert_eq!(state.android_run.phase(), Phase::Starting { token, instance_id: A.into(), cancelled: true });
    assert!(claimed.cancelled());
    assert_eq!(stop_if_busy(&state, A), Some(Ok(())));  // idempotent
    let mut posted = false;
    let (result, job) = claimed.commit(handles(Some(sleeper("0.2")), &events), |_| {
        posted = true;
        Ok(vec![1])
    });
    assert_eq!(result.err().as_deref(), Some(CANCELLED));
    assert!(!posted, "nothing is posted after a stop");
    assert_eq!(state.android_run.phase(), Phase::Finishing { token, instance_id: A.into(), released: false });
    job.unwrap().run();
    assert_eq!(state.android_run.phase(), Phase::Idle);
    assert_eq!(events.0.lock().unwrap().as_slice(), &[(A.to_string(), "task-stopped".to_string())]);
}

#[test]
fn a_committed_start_runs_with_its_own_handles_off_the_instance() {
    let _serial = serial();
    let (state, _dir) = state();
    let events = Arc::new(Recorded::default());
    let claimed = claim(&state, A).unwrap();
    let token = claimed.token;
    let (result, job) = claimed.commit(handles(Some(sleeper("5")), &events), |instance| {
        instance.task_run_state.overall_status = Some("Running".into());
        Ok(vec![11, 12])
    });
    assert_eq!(result, Ok(vec![11, 12]));
    assert!(job.is_none());
    assert_eq!(state.android_run.phase(), Phase::Running { token, instance_id: A.into() });
    assert!(reports_running(&state, A));
    assert!(!reports_running(&state, B));
    {
        let instances = state.instances.lock().unwrap();
        assert!(instances[A].tasker.is_none() && instances[A].agent_children.is_empty());
    }
    // Clean up the sleeper through a stop.
    assert_eq!(stop_if_busy(&state, A), Some(Ok(())));
    wait_for_idle(&state);
}

#[test]
fn a_start_with_nothing_posted_or_a_failed_post_finishes_instead_of_running() {
    let _serial = serial();
    let (state, _dir) = state();
    let events = Arc::new(Recorded::default());
    let (result, job) = claim(&state, A).unwrap().commit(handles(None, &events), |_| Ok(vec![]));
    assert_eq!(result.err().as_deref(), Some(NOTHING_POSTED));  // not reported as a successful start
    job.unwrap().run();
    assert_eq!(state.android_run.phase(), Phase::Idle);
    let (result, job) = claim(&state, A)
        .unwrap()
        .commit(handles(None, &events), |_| Err(crate::commands::utils::CONNECTION_CHANGED.to_string()));
    assert_eq!(result.err().as_deref(), Some(crate::commands::utils::CONNECTION_CHANGED));
    job.unwrap().run();
    assert_eq!(state.android_run.phase(), Phase::Idle);
    assert_eq!(
        events.0.lock().unwrap().as_slice(),
        &[(A.to_string(), "tasks-completed".to_string()), (A.to_string(), "task-stopped".to_string())]
    );
}

#[test]
fn callbacks_from_another_token_are_ignored() {
    let _serial = serial();
    let (state, _dir) = state();
    let events = Arc::new(Recorded::default());
    running(&state, 7, None, &events);
    for message in ["Tasker.Task.Starting", "Tasker.Task.Succeeded", "Tasker.Task.Failed"] {
        let (applied, job) = apply_task_event(&state, 6, message, r#"{"task_id":11,"entry":"x"}"#);
        assert_eq!(applied, Applied::Ignored);
        assert!(job.is_none());
    }
    let (applied, job) = apply_task_event(&state, 6, "Tasker.Task.Succeeded", r#"{"task_id":0,"entry":"MaaTaskerPostStop"}"#);
    assert_eq!(applied, Applied::Ignored);
    assert!(job.is_none());
    assert_eq!(status(&state, "x"), "pending");
    assert_eq!(overall(&state).as_deref(), Some("Running"));
    assert_eq!(state.android_run.phase(), Phase::Running { token: 7, instance_id: A.into() });
}

#[test]
fn callbacks_track_each_task_and_the_last_one_finishes_the_run() {
    let _serial = serial();
    let (state, _dir) = state();
    let events = Arc::new(Recorded::default());
    running(&state, 7, None, &events);
    assert_eq!(apply_task_event(&state, 7, "Tasker.Task.Starting", r#"{"task_id":11}"#).0, Applied::Started(11));
    assert_eq!(status(&state, "x"), "running");
    // Not a task of this run, and not a task message: nothing changes.
    assert_eq!(apply_task_event(&state, 7, "Tasker.Task.Succeeded", r#"{"task_id":99}"#).0, Applied::Ignored);
    assert_eq!(apply_task_event(&state, 7, "Node.Recognition.Succeeded", r#"{"task_id":11}"#).0, Applied::Ignored);
    let (applied, job) = apply_task_event(&state, 7, "Tasker.Task.Succeeded", r#"{"task_id":11}"#);
    assert_eq!(applied, Applied::Finished { task_id: 11, succeeded: true, all_done: false });
    assert!(job.is_none());
    assert_eq!(apply_task_event(&state, 7, "Tasker.Task.Starting", r#"{"task_id":12}"#).0, Applied::Started(12));
    assert_eq!(state.instances.lock().unwrap()[A].task_run_state.current_task_index, 1);
    let (applied, job) = apply_task_event(&state, 7, "Tasker.Task.Failed", r#"{"task_id":12}"#);
    assert_eq!(applied, Applied::Finished { task_id: 12, succeeded: false, all_done: true });
    assert_eq!((status(&state, "x"), status(&state, "y")), ("succeeded".to_string(), "failed".to_string()));
    assert_eq!(overall(&state).as_deref(), Some("Failed"));
    assert!(state.instances.lock().unwrap()[A].task_ids.is_empty());
    assert_eq!(state.android_run.phase(), Phase::Finishing { token: 7, instance_id: A.into(), released: false });
    assert!(!reports_running(&state, A));
    // A late callback of the finished run changes nothing.
    assert_eq!(apply_task_event(&state, 7, "Tasker.Task.Succeeded", r#"{"task_id":12}"#).0, Applied::Ignored);
    job.unwrap().run();
    assert_eq!(state.android_run.phase(), Phase::Idle);
    assert_eq!(events.0.lock().unwrap().as_slice(), &[(A.to_string(), "tasks-completed".to_string())]);
}

#[test]
fn all_tasks_succeeding_marks_the_run_succeeded() {
    let _serial = serial();
    let (state, _dir) = state();
    let events = Arc::new(Recorded::default());
    running(&state, 2, None, &events);
    apply_task_event(&state, 2, "Tasker.Task.Succeeded", r#"{"task_id":11}"#);
    let (_, job) = apply_task_event(&state, 2, "Tasker.Task.Succeeded", r#"{"task_id":12}"#);
    assert_eq!(overall(&state).as_deref(), Some("Succeeded"));
    job.unwrap().run();
    assert_eq!(state.android_run.phase(), Phase::Idle);
}

#[test]
fn an_internal_post_stop_fails_the_rest_and_finishes() {
    let _serial = serial();
    let (state, _dir) = state();
    let events = Arc::new(Recorded::default());
    running(&state, 5, None, &events);
    apply_task_event(&state, 5, "Tasker.Task.Starting", r#"{"task_id":11}"#);
    let (applied, _) = apply_task_event(&state, 5, "Tasker.Task.Starting", r#"{"task_id":0,"entry":"MaaTaskerPostStop"}"#);
    assert_eq!(applied, Applied::Ignored);
    let (applied, job) = apply_task_event(&state, 5, "Tasker.Task.Succeeded", r#"{"task_id":0,"entry":"MaaTaskerPostStop"}"#);
    assert_eq!(applied, Applied::Aborted);
    assert_eq!((status(&state, "x"), status(&state, "y")), ("failed".to_string(), "failed".to_string()));
    assert_eq!(overall(&state).as_deref(), Some("Failed"));
    job.unwrap().run();
    assert_eq!(state.android_run.phase(), Phase::Idle);
}

#[test]
fn a_user_stop_while_running_fails_the_unfinished_tasks_and_finishes() {
    let _serial = serial();
    let (state, _dir) = state();
    let events = Arc::new(Recorded::default());
    running(&state, 9, Some(sleeper("0.3")), &events);
    apply_task_event(&state, 9, "Tasker.Task.Succeeded", r#"{"task_id":11}"#);
    apply_task_event(&state, 9, "Tasker.Task.Starting", r#"{"task_id":12}"#);
    assert_eq!(stop_if_busy(&state, A), Some(Ok(())));
    assert_eq!((status(&state, "x"), status(&state, "y")), ("succeeded".to_string(), "failed".to_string()));
    assert_eq!(overall(&state).as_deref(), Some("Failed"));
    assert!(matches!(state.android_run.phase(), Phase::Finishing { token: 9, .. } | Phase::Idle));
    assert_eq!(stop_if_busy(&state, A), Some(Ok(())));  // idempotent while finishing
    assert_eq!(claim(&state, A).err().as_deref(), Some(STILL_FINISHING));
    wait_for_idle(&state);
    // Task 11 ended through its own callback; task 12 was running and is retired from the statistics once.
    assert_eq!(events.kinds(), ["retire 12 started=true", "task-stopped"]);
    // Nothing of the slot's for another instance, or once idle: the shared code handles it.
    assert_eq!(stop_if_busy(&state, A), None);
    assert_eq!(stop_if_busy(&state, W), None);
}

#[test]
fn finishing_ends_only_after_the_agent_exited_and_owner_lock_is_free() {
    let _serial = serial();
    let (state, dir) = state();
    let events = Arc::new(Recorded::default());
    let holder = hold_lock(&dir);
    running(&state, 4, Some(sleeper("0.3")), &events);
    let job = {
        let mut inner = lock(&state.android_run.inner);
        begin_finishing(&state.android_run, &mut inner, 4, "task-stopped")
    };
    let started = Instant::now();
    let worker = std::thread::spawn(move || job.run());
    // The agent (sleeper) is still alive for ~0.3 s and the lock stays held: Finishing throughout.
    std::thread::sleep(Duration::from_millis(150));
    assert_eq!(state.android_run.phase(), Phase::Finishing { token: 4, instance_id: A.into(), released: false });
    std::thread::sleep(Duration::from_millis(350));
    assert!(matches!(state.android_run.phase(), Phase::Finishing { token: 4, .. }));
    drop(holder);
    worker.join().unwrap();
    assert!(started.elapsed() >= Duration::from_millis(300));
    assert_eq!(state.android_run.phase(), Phase::Idle);
}

#[test]
fn a_lock_that_stays_held_leaves_the_slot_finishing_and_says_why() {
    let _serial = serial();
    let (state, dir) = state();
    let events = Arc::new(Recorded::default());
    let holder = hold_lock(&dir);
    running(&state, 4, None, &events);
    let job = {
        let mut inner = lock(&state.android_run.inner);
        begin_finishing(&state.android_run, &mut inner, 4, "task-stopped")
    };
    job.run();
    assert_eq!(state.android_run.phase(), Phase::Finishing { token: 4, instance_id: A.into(), released: true });
    assert_eq!(claim(&state, A).err().as_deref(), Some(STILL_FINISHING));
    assert_eq!(claim(&state, B).err().as_deref(), Some(BUSY_OTHER_INSTANCE));
    assert!(events.0.lock().unwrap().iter().any(|(_, line)| line.contains("owner.lock")));
    drop(holder);
    let claimed = claim(&state, A).unwrap();
    drop(claimed);
}

#[test]
fn an_agent_that_does_not_exit_is_killed_within_the_bound() {
    let _serial = serial();
    let (state, _dir) = state();
    let events = Arc::new(Recorded::default());
    running(&state, 4, Some(sleeper("30")), &events);
    let job = {
        let mut inner = lock(&state.android_run.inner);
        begin_finishing(&state.android_run, &mut inner, 4, "task-stopped")
    };
    let started = Instant::now();
    job.run();
    assert!(started.elapsed() < Duration::from_secs(5));
    assert_eq!(state.android_run.phase(), Phase::Idle);
}

#[test]
fn a_finishing_job_of_an_older_token_does_not_touch_a_newer_phase() {
    let _serial = serial();
    let (state, _dir) = state();
    let events = Arc::new(Recorded::default());
    running(&state, 4, None, &events);
    let job = {
        let mut inner = lock(&state.android_run.inner);
        begin_finishing(&state.android_run, &mut inner, 4, "task-stopped")
    };
    let newer = Phase::Running { token: 5, instance_id: A.into() };
    set_phase(&state, newer.clone());
    job.run();
    assert_eq!(state.android_run.phase(), newer);
}

#[test]
fn every_unfinished_task_is_retired_exactly_once_when_a_run_is_cut_short() {
    let _serial = serial();
    // A user stop with one task running and one never started.
    let (state, _dir) = state();
    let events = Arc::new(Recorded::default());
    running(&state, 3, None, &events);
    apply_task_event(&state, 3, "Tasker.Task.Starting", r#"{"task_id":11}"#);
    assert_eq!(stop_if_busy(&state, A), Some(Ok(())));
    wait_for_idle(&state);
    assert_eq!(stop_if_busy(&state, A), None);
    assert_eq!(apply_task_event(&state, 3, "Tasker.Task.Failed", r#"{"task_id":11}"#).0, Applied::Ignored);
    assert_eq!(events.kinds(), ["retire 11 started=true", "retire 12 started=false", "task-stopped"]);

    // An internal post_stop after the first task finished: only the second is retired.
    let (state, _dir) = self::state();
    let events = Arc::new(Recorded::default());
    running(&state, 4, None, &events);
    apply_task_event(&state, 4, "Tasker.Task.Succeeded", r#"{"task_id":11}"#);
    let (_, job) = apply_task_event(&state, 4, "Tasker.Task.Succeeded", r#"{"task_id":0,"entry":"MaaTaskerPostStop"}"#);
    job.unwrap().run();
    assert_eq!(events.kinds(), ["retire 12 started=false", "tasks-completed"]);

    // A run whose tasks all end through their callbacks retires nothing extra.
    let (state, _dir) = self::state();
    let events = Arc::new(Recorded::default());
    running(&state, 5, None, &events);
    apply_task_event(&state, 5, "Tasker.Task.Succeeded", r#"{"task_id":11}"#);
    let (_, job) = apply_task_event(&state, 5, "Tasker.Task.Failed", r#"{"task_id":12}"#);
    job.unwrap().run();
    assert_eq!(events.kinds(), ["tasks-completed"]);
}

/// A tasker for the finishing tests: running until post_stop (when `stops_on_post_stop`) or until the test calls
/// stop(); its running() waits while `gate` is closed, which holds the finishing worker at its first step. post_stop
/// records whether `watch` (the touch marker) existed at that moment.
#[derive(Clone)]
struct FakeTasker {
    running: Arc<AtomicBool>,
    post_stops: Arc<Mutex<Vec<bool>>>,
    stops_on_post_stop: bool,
    gate: Arc<AtomicBool>,
    watch: PathBuf,
}

impl FakeTasker {
    fn new(stops_on_post_stop: bool, watch: PathBuf) -> Self {
        FakeTasker {
            running: Arc::new(AtomicBool::new(true)),
            post_stops: Arc::default(),
            stops_on_post_stop,
            gate: Arc::new(AtomicBool::new(true)),
            watch,
        }
    }
    fn stop(&self) {
        self.running.store(false, Ordering::SeqCst);
    }
    fn post_stops(&self) -> Vec<bool> {
        self.post_stops.lock().unwrap().clone()
    }
}

impl RunTasker for FakeTasker {
    fn running(&self) -> bool {
        while !self.gate.load(Ordering::SeqCst) {
            std::thread::sleep(Duration::from_millis(1));
        }
        self.running.load(Ordering::SeqCst)
    }
    fn post_stop(&self) {
        self.post_stops.lock().unwrap().push(self.watch.exists());
        if self.stops_on_post_stop {
            self.stop();
        }
    }
}

fn running_with(state: &Arc<MaaState>, token: u64, child: Option<Child>, events: &Arc<Recorded>, tasker: &FakeTasker) {
    running(state, token, child, events);
    lock(&state.android_run.inner).run.as_mut().unwrap().tasker = Some(Box::new(tasker.clone()));
}

fn alive(pid: u32) -> bool {
    unsafe { libc::kill(pid as libc::pid_t, 0) == 0 }
}

fn has_message(events: &Recorded, text: &str) -> bool {
    events.kinds().iter().any(|kind| kind == &format!("message: {}", text))
}

fn finishing_job(state: &Arc<MaaState>, token: u64) -> FinishJob {
    let mut inner = lock(&state.android_run.inner);
    begin_finishing(&state.android_run, &mut inner, token, "task-stopped")
}

/// Records, at each statistics retirement, whether stop.request already existed.
struct RetireSeesRequest {
    request: PathBuf,
    seen: Mutex<Vec<bool>>,
}

impl RunEvents for RetireSeesRequest {
    fn state_changed(&self, _: &str, _: &str) {}
    fn message(&self, _: &str, _: &str) {}
    fn retire(&self, _: &str, _: i64, _: bool) {
        self.seen.lock().unwrap().push(self.request.exists());
    }
}

#[test]
fn a_stop_publishes_its_request_before_it_returns_and_before_any_statistics_io() {
    let _serial = serial();
    let (state, dir) = state();
    let events = Arc::new(Recorded::default());
    let tasker = FakeTasker::new(true, dir.join("touch.json"));
    tasker.gate.store(false, Ordering::SeqCst);  // the worker is held at its first step
    running_with(&state, 7, None, &events, &tasker);
    let seen = Arc::new(RetireSeesRequest { request: dir.join("stop.request"), seen: Mutex::default() });
    lock(&state.android_run.inner).run.as_mut().unwrap().events = Arc::clone(&seen) as Arc<dyn RunEvents>;
    assert_eq!(stop_if_busy(&state, A), Some(Ok(())));
    assert!(dir.join("stop.request").exists(), "written by Stop itself, not left to the worker");
    assert!(seen.seen.lock().unwrap().is_empty(), "no statistics I/O before Stop returned");
    tasker.gate.store(true, Ordering::SeqCst);
    wait_for_idle(&state);
    assert_eq!(*seen.seen.lock().unwrap(), [true, true]);  // both tasks retired, each after the request
    assert_eq!(tasker.post_stops(), [false]);  // no touch was reserved: the tasker is stopped normally
}

#[test]
fn a_touch_released_after_the_stop_request_is_followed_by_post_stop() {
    let _serial = serial();
    let (state, dir) = state();
    let events = Arc::new(Recorded::default());
    let marker = dir.join("touch.json");
    let request = dir.join("stop.request");
    let tasker = FakeTasker::new(true, marker.clone());
    running_with(&state, 4, Some(sleeper("0.2")), &events, &tasker);
    std::fs::write(&marker, "{}").unwrap();  // the agent has a touch reserved when the stop comes
    let order = Arc::new(Mutex::new(Vec::<&str>::new()));
    let agent = {
        let (order, marker, request) = (Arc::clone(&order), marker.clone(), request.clone());
        std::thread::spawn(move || {
            let deadline = Instant::now() + Duration::from_secs(2);
            while !request.exists() && Instant::now() < deadline {
                std::thread::sleep(Duration::from_millis(5));
            }
            order.lock().unwrap().push("request seen");
            std::thread::sleep(Duration::from_millis(100));
            order.lock().unwrap().push("touch released");
            std::fs::remove_file(&marker).unwrap();
        })
    };
    finishing_job(&state, 4).run();
    agent.join().unwrap();
    assert_eq!(order.lock().unwrap().as_slice(), ["request seen", "touch released"]);
    assert_eq!(tasker.post_stops(), [false], "post_stop once, after the marker was gone");
    assert!(!has_message(&events, AGENT_TERMINATED_AT_STOP));
    assert_eq!(state.android_run.phase(), Phase::Idle);
    assert!(request.exists(), "the request stays for the agent until the next start removes it");
    assert_eq!(std::fs::read_dir(&dir).unwrap().filter(|e| {
        e.as_ref().unwrap().file_name().to_string_lossy().ends_with(".tmp")
    }).count(), 0);
}

#[test]
fn a_touch_still_reserved_kills_the_agent_and_never_calls_post_stop() {
    let _serial = serial();
    let (state, dir) = state();
    let events = Arc::new(Recorded::default());
    let marker = dir.join("touch.json");
    let tasker = FakeTasker::new(true, marker.clone());
    let child = sleeper("30");
    let pid = child.id();
    running_with(&state, 4, Some(child), &events, &tasker);
    std::fs::write(&marker, "{}").unwrap();  // never released
    let job = finishing_job(&state, 4);
    let worker = std::thread::spawn(move || job.run());
    // touch_wait (0.3 s) runs out, the agent is killed, and the tasker keeps running past tasker_stop (0.2 s).
    std::thread::sleep(Duration::from_millis(900));
    assert!(!alive(pid), "the agent is killed (and reaped) instead of stopping the tasker");
    assert_eq!(state.android_run.phase(), Phase::Finishing { token: 4, instance_id: A.into(), released: false });
    assert_eq!(claim(&state, A).err().as_deref(), Some(STILL_FINISHING));
    assert!(tasker.post_stops().is_empty());
    assert!(has_message(&events, AGENT_TERMINATED_AT_STOP));
    assert!(has_message(&events, TASKER_STILL_RUNNING_AT_STOP));
    tasker.stop();  // the task ends by itself
    worker.join().unwrap();
    assert_eq!(state.android_run.phase(), Phase::Idle);
    assert!(tasker.post_stops().is_empty());
    assert!(marker.exists(), "the marker stays and blocks later touches");
}

#[test]
fn a_stop_request_that_cannot_be_written_kills_the_agent_and_never_calls_post_stop() {
    let _serial = serial();
    let parent = temp_dir();
    std::fs::write(parent.join("file"), "").unwrap();
    let (state, _) = state_in(parent.join("file").join("input"));  // not creatable: its parent is a file
    let events = Arc::new(Recorded::default());
    let tasker = FakeTasker::new(true, parent.join("touch.json"));
    let child = sleeper("30");
    let pid = child.id();
    running_with(&state, 4, Some(child), &events, &tasker);
    let job = finishing_job(&state, 4);
    let stopper = {
        let tasker = tasker.clone();
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(100));
            tasker.stop();
        })
    };
    job.run();
    stopper.join().unwrap();
    assert!(!alive(pid));
    assert!(tasker.post_stops().is_empty());
    assert!(has_message(&events, AGENT_TERMINATED_AT_STOP));
    // owner.lock cannot be checked there either: the slot stays finishing (safe).
    assert!(matches!(state.android_run.phase(), Phase::Finishing { token: 4, .. }));
}

#[test]
fn quitting_while_running_takes_the_safe_stop_and_waits_until_no_touch_can_be_cut() {
    let _serial = serial();
    // A touch stays reserved; the task ends by itself 0.6 s after the agent is gone.
    let (state, dir) = state();
    let events = Arc::new(Recorded::default());
    let tasker = FakeTasker::new(true, dir.join("touch.json"));
    let child = sleeper("30");
    let pid = child.id();
    running_with(&state, 4, Some(child), &events, &tasker);
    std::fs::write(dir.join("touch.json"), "{}").unwrap();
    let stopper = {
        let tasker = tasker.clone();
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(600));
            tasker.stop();
        })
    };
    let started = Instant::now();
    kill_on_exit(&state);
    let waited = started.elapsed();
    stopper.join().unwrap();
    assert!(waited >= Duration::from_millis(550), "returned after {:?}, before the tasker stopped", waited);
    assert!(waited < timing().exit_wait, "{:?}", waited);
    assert!(tasker.post_stops().is_empty());
    assert!(!alive(pid));
    assert!(dir.join("stop.request").exists());
    wait_for_idle(&state);

    // No touch reserved: the cooperative stop, and the exit then kills the agent rather than leaving it behind.
    let (state, dir) = self::state();
    let tasker = FakeTasker::new(false, dir.join("touch.json"));  // keeps "running" after post_stop
    let child = sleeper("30");
    let pid = child.id();
    running_with(&state, 5, Some(child), &events, &tasker);
    let started = Instant::now();
    kill_on_exit(&state);
    assert!(started.elapsed() < Duration::from_millis(500));
    assert!(!alive(pid));
    std::thread::sleep(Duration::from_millis(50));
    assert_eq!(tasker.post_stops(), [false]);
    tasker.stop();
    wait_for_idle(&state);

    // The tasker never stops: the exit proceeds after exit_wait, still without post_stop.
    let (state, dir) = self::state();
    let tasker = FakeTasker::new(true, dir.join("touch.json"));
    running_with(&state, 6, Some(sleeper("30")), &events, &tasker);
    std::fs::write(dir.join("touch.json"), "{}").unwrap();
    let started = Instant::now();
    kill_on_exit(&state);
    let waited = started.elapsed();
    assert!(waited >= timing().exit_wait && waited < timing().exit_wait * 2, "{:?}", waited);
    assert!(tasker.post_stops().is_empty());
    tasker.stop();
    wait_for_idle(&state);
}

#[test]
fn quitting_while_a_stop_is_finishing_waits_for_the_reserved_touch() {
    let _serial = serial();
    let (state, dir) = state();
    let events = Arc::new(Recorded::default());
    let marker = dir.join("touch.json");
    let tasker = FakeTasker::new(true, marker.clone());
    running_with(&state, 4, Some(sleeper("30")), &events, &tasker);
    std::fs::write(&marker, "{}").unwrap();
    assert_eq!(stop_if_busy(&state, A), Some(Ok(())));  // the worker now owns the handles
    let releaser = {
        let marker = marker.clone();
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(200));
            std::fs::remove_file(&marker).unwrap();
        })
    };
    let started = Instant::now();
    kill_on_exit(&state);
    releaser.join().unwrap();
    assert!(started.elapsed() >= Duration::from_millis(180), "{:?}", started.elapsed());
    wait_for_idle(&state);
    assert_eq!(tasker.post_stops(), [false]);

    // Nothing running or finishing: the exit does not wait.
    let started = Instant::now();
    kill_on_exit(&state);
    assert!(started.elapsed() < Duration::from_millis(50));
}

#[test]
fn a_start_removes_a_stale_stop_request_before_any_agent_exists() {
    let _serial = serial();
    let (state, dir) = state();
    std::fs::write(dir.join("stop.request"), "{}").unwrap();
    // Refused claims leave it alone (another run may still be stopping).
    set_phase(&state, Phase::Finishing { token: 1, instance_id: A.into(), released: false });
    assert!(claim(&state, A).is_err());
    assert!(dir.join("stop.request").exists());
    set_phase(&state, Phase::Idle);
    let holder = hold_lock(&dir);
    assert_eq!(claim(&state, A).err().as_deref(), Some(OWNER_LOCK_BUSY));
    assert!(dir.join("stop.request").exists());
    drop(holder);
    let claimed = claim(&state, A).unwrap();
    assert!(!dir.join("stop.request").exists());
    drop(claimed);
}

#[test]
fn the_slot_is_claimed_only_for_a_connected_adb_instance() {
    let _serial = serial();
    let (state, _dir) = state();
    for id in [W, M, "missing"] {
        assert!(claim(&state, id).is_err(), "{}", id);
        assert_eq!(state.android_run.phase(), Phase::Idle);
    }
    state.instances.lock().unwrap().get_mut(A).unwrap().controller_stopped = true;
    assert_eq!(claim(&state, A).err().as_deref(), Some(NOT_CONNECTED));
    state.instances.lock().unwrap().get_mut(A).unwrap().controller_stopped = false;
    drop(claim(&state, A).unwrap());
    // The production check also needs a connected native controller handle (none in unit tests).
    assert_eq!(instance_is_connected_adb(&state, A).err().as_deref(), Some(NOT_CONNECTED));
    assert_eq!(instance_is_connected_adb(&state, M).err().as_deref(), Some(NOT_CONNECTED));
}

// ----------------------------------------------------------------------------
// ADB masks
// ----------------------------------------------------------------------------

#[test]
fn adb_connections_use_the_controller_definitions_methods() {
    let _serial = serial();
    let overlay: serde_json::Value = serde_json::from_str(include_str!(
        "../../../../../build/android/interface-overlay.json"
    ))
    .unwrap();
    let controller = overlay["controller"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["name"] == "nikki_android")
        .unwrap();
    assert_eq!(controller["type"], "Adb");
    // MaaFramework's method names (MaaDef.h) to the binding's bits.
    let input = |name: &str| match name {
        "AdbShell" => AdbInputMethod::ADB_SHELL,
        "MinitouchAndAdbKey" => AdbInputMethod::MINITOUCH_AND_ADB_KEY,
        "Maatouch" => AdbInputMethod::MAATOUCH,
        "EmulatorExtras" => AdbInputMethod::EMULATOR_EXTRAS,
        other => panic!("unknown input method {}", other),
    };
    let screencap = |name: &str| match name {
        "EncodeToFileAndPull" => AdbScreencapMethod::ENCODE_TO_FILE_AND_PULL,
        "Encode" => AdbScreencapMethod::ENCODE,
        "RawWithGzip" => AdbScreencapMethod::RAW_WITH_GZIP,
        "RawByNetcat" => AdbScreencapMethod::RAW_BY_NETCAT,
        "MinicapDirect" => AdbScreencapMethod::MINICAP_DIRECT,
        "MinicapStream" => AdbScreencapMethod::MINICAP_STREAM,
        "EmulatorExtras" => AdbScreencapMethod::EMULATOR_EXTRAS,
        other => panic!("unknown screencap method {}", other),
    };
    let names = |value: &serde_json::Value| -> Vec<String> {
        match value {
            serde_json::Value::String(name) => vec![name.clone()],
            serde_json::Value::Array(items) => items.iter().map(|i| i.as_str().unwrap().to_string()).collect(),
            other => panic!("unexpected {}", other),
        }
    };
    let defined_input = names(&controller["adb"]["input"])
        .iter()
        .fold(AdbInputMethod::empty(), |all, name| all | input(name));
    let defined_screencap = names(&controller["adb"]["screencap"])
        .iter()
        .fold(AdbScreencapMethod::empty(), |all, name| all | screencap(name));
    assert_eq!(defined_input, PINNED_INPUT);
    assert_eq!(defined_screencap, PINNED_SCREENCAP);

    // Whatever the device discovery offered (here: everything, minitouch included), the connection uses these.
    let ControllerConfig::Adb { screencap_methods, input_methods, adb_path, address, config, display_short_side } =
        pin_adb_methods(adb())
    else {
        panic!("not Adb");
    };
    assert_eq!(input_methods, PINNED_INPUT.bits().to_string());
    assert_eq!(screencap_methods, PINNED_SCREENCAP.bits().to_string());
    assert_eq!((adb_path.as_str(), address.as_str()), ("adb", "127.0.0.1:5555"));
    assert_eq!((config.as_str(), display_short_side), ("{\"extras\":{}}", Some(720)));
}

#[test]
fn desktop_configs_pass_the_connect_entry_untouched() {
    let _serial = serial();
    let (state, _dir) = state();
    // With an Android run on another instance, too.
    set_phase(&state, Phase::Running { token: 1, instance_id: A.into() });
    for (id, config) in [(W, win32()), (M, macos())] {
        let (passed, guard) = enter_connect(&state, id, config.clone()).unwrap();
        assert_eq!(passed, config);
        assert_eq!(state.android_run.connecting(id), 1);
        drop(guard);
        assert_eq!(state.android_run.connecting(id), 0);
    }
    assert_eq!(state.android_run.phase(), Phase::Running { token: 1, instance_id: A.into() });
}

// ----------------------------------------------------------------------------
// The shared functions with Win32 and MacOS instances: observable results as before
// ----------------------------------------------------------------------------

/// Runs `check` once with the slot idle and once while an Android run holds the slot for instance A: the
/// Win32 and MacOS instances must see the same results either way, and the slot is never touched.
fn with_and_without_an_android_run(check: impl Fn(&Arc<MaaState>)) {
    for busy in [false, true] {
        let (state, _dir) = state();
        let phase = if busy { Phase::Running { token: 1, instance_id: A.into() } } else { Phase::Idle };
        set_phase(&state, phase.clone());
        check(&state);
        assert_eq!(state.android_run.phase(), phase);
        assert_eq!(state.android_run.connecting(W), 0);
        assert_eq!(state.android_run.connecting(M), 0);
    }
}

#[test]
fn stopping_desktop_instances_goes_through_the_shared_code() {
    let _serial = serial();
    use crate::commands::maa_core::stop_task_impl;
    with_and_without_an_android_run(|state| {
        // Win32 with no tasker yet: the stop is recorded and the start generation moves on.
        let before = state.instances.lock().unwrap()[W].start_generation;
        assert_eq!(stop_task_impl(state, W), Ok(()));
        {
            let instances = state.instances.lock().unwrap();
            assert!(instances[W].stop_in_progress);
            assert_eq!(instances[W].start_generation, before.wrapping_add(1));
        }
        // MacOS with no tasker: the Mac path also ends the run's statuses.
        {
            let mut instances = state.instances.lock().unwrap();
            let run = &mut instances.get_mut(M).unwrap().task_run_state;
            run.statuses.insert("m".into(), "running".into());
            run.overall_status = Some("Running".into());
        }
        assert_eq!(stop_task_impl(state, M), Ok(()));
        let instances = state.instances.lock().unwrap();
        assert_eq!(instances[M].task_run_state.statuses["m"], "failed");
        assert_eq!(instances[M].task_run_state.overall_status.as_deref(), Some("Failed"));
        assert!(instances[M].stop_in_progress);
    });
}

#[test]
fn stopping_desktop_agents_goes_through_the_shared_code() {
    let _serial = serial();
    use crate::commands::maa_agent::stop_agent_impl;
    with_and_without_an_android_run(|state| {
        for id in [W, M] {
            let before = state.instances.lock().unwrap()[id].start_generation;
            assert_eq!(stop_agent_impl(state, id), Ok(true));
            assert_eq!(state.instances.lock().unwrap()[id].start_generation, before.wrapping_add(1));
        }
        assert_eq!(stop_agent_impl(state, "missing").err().as_deref(), Some("Instance not found"));
    });
}

#[test]
fn other_shared_entry_points_answer_desktop_instances_as_before() {
    let _serial = serial();
    use crate::commands::maa_core::{connect_controller_impl, destroy_instance_impl, load_resource_impl,
                                    override_pipeline_impl, post_click_impl};
    with_and_without_an_android_run(|state| {
        let not_loaded = crate::commands::maa_library::ensure_loaded(state).unwrap_err();
        for (id, config) in [(W, win32()), (M, macos())] {
            assert_eq!(override_pipeline_impl(state, id, 1, "{}").err().as_deref(), Some("Tasker not created"));
            assert_eq!(post_click_impl(state, id, 1, 1).err().as_deref(), Some("Controller not connected"));
            let on_event: Arc<dyn Fn(&str, &str) + Send + Sync> = Arc::new(|_, _| {});
            assert_eq!(load_resource_impl(state, id, &[], on_event.clone(), None).err(), Some(not_loaded.clone()));
            let connected = tauri::async_runtime::block_on(connect_controller_impl(
                Arc::clone(state), id.to_string(), config, on_event,
            ));
            assert_eq!(connected.err(), Some(not_loaded.clone()));
        }
        for id in [W, M] {
            assert_eq!(destroy_instance_impl(state, id), Ok(()));
            assert!(!state.instances.lock().unwrap().contains_key(id));
        }
    });
}

#[test]
fn the_state_snapshot_reports_only_the_android_instance_as_running() {
    let _serial = serial();
    let (state, _dir) = state();
    let events = Arc::new(Recorded::default());
    let snapshot = crate::commands::state::get_all_states_impl(&state).unwrap();
    assert!(snapshot.instances.values().all(|s| !s.is_running));
    running(&state, 1, None, &events);
    let snapshot = crate::commands::state::get_all_states_impl(&state).unwrap();
    assert!(snapshot.instances[A].is_running);
    for id in [B, W, M] {
        assert!(!snapshot.instances[id].is_running, "{}", id);
    }
    set_phase(&state, Phase::Finishing { token: 1, instance_id: A.into(), released: false });
    let snapshot = crate::commands::state::get_all_states_impl(&state).unwrap();
    assert!(!snapshot.instances[A].is_running);
}

// ----------------------------------------------------------------------------
// Review round 3: quitting while starting, a stop request that cannot be written, the device lock
// ----------------------------------------------------------------------------

#[test]
fn quitting_while_starting_cancels_the_start_and_closes_android_start_admission() {
    let _serial = serial();
    let (state, _dir) = state();
    let events = Arc::new(Recorded::default());
    let claimed = claim(&state, A).unwrap();
    let token = claimed.token;
    let quitting = {
        let state = Arc::clone(&state);
        std::thread::spawn(move || {
            let started = Instant::now();
            kill_on_exit(&state);
            started.elapsed()
        })
    };
    let deadline = Instant::now() + Duration::from_secs(1);
    while state.android_run.phase() != (Phase::Starting { token, instance_id: A.into(), cancelled: true }) {
        assert!(Instant::now() < deadline, "still {:?}", state.android_run.phase());
        std::thread::sleep(Duration::from_millis(5));
    }
    assert!(claimed.cancelled());
    // The agent handshake returns only now: the pending start cannot post, it hands its agent to a finishing worker.
    std::thread::sleep(Duration::from_millis(100));
    let child = sleeper("30");
    let pid = child.id();
    let mut posted = false;
    let (result, job) = claimed.commit(handles(Some(child), &events), |_| {
        posted = true;
        Ok(vec![1])
    });
    assert_eq!(result.err().as_deref(), Some(CANCELLED));
    assert!(!posted, "no task is posted after Quit");
    job.unwrap().spawn();
    let waited = quitting.join().unwrap();
    assert!(waited >= Duration::from_millis(100), "the exit waited for the pending start: {:?}", waited);
    assert!(waited < timing().exit_wait, "{:?}", waited);
    assert!(!alive(pid), "the start's agent is killed by the exit");
    wait_for_idle(&state);
    // Admission stays closed: no new start, on any instance.
    assert_eq!(claim(&state, A).err().as_deref(), Some(QUITTING));
    assert_eq!(claim(&state, B).err().as_deref(), Some(QUITTING));

    // A claim admitted before Quit and committing after it, without the exit having seen it as Starting (the flag
    // alone): still refused.
    let (state, _dir) = self::state();
    let claimed = claim(&state, A).unwrap();
    lock(&state.android_run.inner).closed = true;
    let (result, job) = claimed.commit(handles(None, &events), |_| panic!("posted after Quit"));
    assert_eq!(result.err().as_deref(), Some(CANCELLED));
    job.unwrap().run();
    assert_eq!(state.android_run.phase(), Phase::Idle);

    // A start that fails before its agent while the exit waits: the slot goes back to Idle and the exit returns.
    let (state, _dir) = self::state();
    let claimed = claim(&state, A).unwrap();
    let dropper = std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(100));
        drop(claimed);
    });
    let started = Instant::now();
    kill_on_exit(&state);
    dropper.join().unwrap();
    assert!(started.elapsed() >= Duration::from_millis(90) && started.elapsed() < timing().exit_wait);
    assert_eq!(state.android_run.phase(), Phase::Idle);
}

#[test]
fn a_stop_whose_request_cannot_be_written_kills_the_agent_before_it_returns() {
    let _serial = serial();
    let (state, dir) = state();
    let events = Arc::new(Recorded::default());
    let tasker = FakeTasker::new(true, dir.join("touch.json"));
    tasker.gate.store(false, Ordering::SeqCst);  // the worker is held at its first step
    let child = sleeper("30");
    let pid = child.id();
    running_with(&state, 7, Some(child), &events, &tasker);
    let seen = Arc::new(RetireSeesRequest { request: dir.join("stop.request"), seen: Mutex::default() });
    lock(&state.android_run.inner).run.as_mut().unwrap().events = Arc::clone(&seen) as Arc<dyn RunEvents>;
    // The staging file cannot be written (a directory has its name) while the touch directory stays writable.
    std::fs::create_dir(dir.join(format!("stop.request.{}.tmp", std::process::id()))).unwrap();
    assert_eq!(stop_if_busy(&state, A), Some(Ok(())));
    assert!(!alive(pid), "the agent is killed (and reaped) inside Stop");
    assert!(!dir.join("stop.request").exists());
    assert!(seen.seen.lock().unwrap().is_empty(), "no statistics I/O before Stop returned");
    tasker.gate.store(true, Ordering::SeqCst);
    std::thread::sleep(Duration::from_millis(100));
    tasker.stop();  // the task ends by itself once the agent is gone
    wait_for_idle(&state);
    assert_eq!(*seen.seen.lock().unwrap(), [false, false]);
    assert!(tasker.post_stops().is_empty(), "never post_stop without the handshake");
}

#[test]
fn a_start_is_refused_while_another_process_drives_the_emulator() {
    let _serial = serial();
    let (state, dir) = state();
    let path = crate::commands::android_input::device_lock_path(&dir, "127.0.0.1:5555");
    // The runner (or another client) holds the device lock: refused, the slot stays Idle, nothing is cleared.
    let holder = std::fs::OpenOptions::new().read(true).append(true).create(true).open(&path).unwrap();
    assert_eq!(unsafe { libc::flock(holder.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) }, 0);
    std::fs::write(dir.join("stop.request"), "{}").unwrap();
    assert_eq!(claim(&state, A).err().as_deref(), Some(DEVICE_BUSY));
    assert_eq!(state.android_run.phase(), Phase::Idle);
    assert!(dir.join("stop.request").exists());
    // Another emulator is not affected.
    if let Some(ControllerConfig::Adb { address, .. }) =
        state.instances.lock().unwrap().get_mut(B).unwrap().controller_config.as_mut()
    {
        *address = "127.0.0.1:5556".into();
    }
    drop(claim(&state, B).unwrap());
    drop(holder);
    drop(claim(&state, A).unwrap());
    assert!(path.exists(), "the lock file is never removed");
}

#[test]
fn the_device_lock_is_held_from_the_claim_until_the_slot_is_idle() {
    let _serial = serial();
    let (state, dir) = state();
    let events = Arc::new(Recorded::default());
    let free = || crate::commands::android_input::take_device_lock(&dir, "127.0.0.1:5555").unwrap().is_some();
    assert!(free());
    // Starting, then Running, then Finishing: held throughout (a runner started meanwhile would be refused).
    let claimed = claim(&state, A).unwrap();
    let token = claimed.token;
    assert!(!free());
    let (result, _) = claimed.commit(handles(Some(sleeper("0.3")), &events), |_| Ok(vec![11, 12]));
    assert_eq!(result, Ok(vec![11, 12]));
    assert!(!free());
    let job = finishing_job(&state, token);
    assert!(!free());
    job.run();
    assert_eq!(state.android_run.phase(), Phase::Idle);
    assert!(free());
    // A start that gives the slot back before its agent releases it too, and a refused claim never keeps it.
    drop(claim(&state, A).unwrap());
    assert!(free());
    let holder = hold_lock(&dir);
    assert_eq!(claim(&state, A).err().as_deref(), Some(OWNER_LOCK_BUSY));
    assert!(free());
    // A finishing slot still waiting for owner.lock keeps the device lock until it heals.
    let claimed = {
        drop(holder);
        claim(&state, A).unwrap()
    };
    let token = claimed.token;
    let (result, _) = claimed.commit(handles(None, &events), |_| Ok(vec![11]));
    assert!(result.is_ok());
    let holder = hold_lock(&dir);
    finishing_job(&state, token).run();
    assert!(matches!(state.android_run.phase(), Phase::Finishing { released: true, .. }));
    assert!(!free());
    drop(holder);
    assert_eq!(refuse_while_busy(&state, A, Op::Connect), Ok(()));  // heals
    assert_eq!(state.android_run.phase(), Phase::Idle);
    assert!(free());
}

// ----------------------------------------------------------------------------
// The process activity (App Nap off) of a run
// ----------------------------------------------------------------------------

struct CountedActivity(Arc<AtomicU64>);

impl Drop for CountedActivity {
    fn drop(&mut self) {
        self.0.fetch_add(1, Ordering::SeqCst);
    }
}

/// A state whose slot counts the activities it begins and ends, as (begun, ended).
fn counted_state() -> (Arc<MaaState>, PathBuf, impl Fn() -> (u64, u64)) {
    let begun = Arc::new(AtomicU64::new(0));
    let ended = Arc::new(AtomicU64::new(0));
    let (b, e) = (Arc::clone(&begun), Arc::clone(&ended));
    let (state, dir) = state_with(temp_dir(), move |slot| {
        slot.with_activity(Arc::new(move || {
            b.fetch_add(1, Ordering::SeqCst);
            Box::new(CountedActivity(Arc::clone(&e))) as activity::Activity
        }))
    });
    (state, dir, move || (begun.load(Ordering::SeqCst), ended.load(Ordering::SeqCst)))
}

#[test]
fn a_run_holds_the_process_activity_from_its_claim_to_its_retirement() {
    let _serial = serial();
    let (state, dir, counts) = counted_state();
    let events = Arc::new(Recorded::default());

    // A refused start begins none, and Win32 and MacOS instances never claim the slot.
    assert_eq!(claim(&state, W).err().as_deref(), Some(NOT_CONNECTED));
    assert_eq!(claim(&state, M).err().as_deref(), Some(NOT_CONNECTED));
    let holder = hold_lock(&dir);
    assert_eq!(claim(&state, A).err().as_deref(), Some(OWNER_LOCK_BUSY));
    drop(holder);
    assert_eq!(counts(), (0, 0));

    // A start that fails before the agent ends it when it gives the slot back.
    let claimed = claim(&state, A).unwrap();
    assert!(state.android_run.holds_activity());
    assert_eq!(counts(), (1, 0));
    drop(claimed);
    assert_eq!((counts(), state.android_run.holds_activity()), ((1, 1), false));

    // A committed run holds it while running and through the stop's finishing, until the slot is idle.
    let claimed = claim(&state, A).unwrap();
    let (result, job) = claimed.commit(handles(Some(sleeper("0.2")), &events), |_| Ok(vec![11]));
    assert!(result.is_ok() && job.is_none());
    assert_eq!(counts(), (2, 1));
    assert_eq!(stop_if_busy(&state, A), Some(Ok(())));
    wait_for_idle(&state);
    assert_eq!((counts(), state.android_run.holds_activity()), ((2, 2), false));

    // A failed post finishes the run, and the finishing ends it.
    let (result, job) = claim(&state, A).unwrap().commit(handles(None, &events), |_| Err("post failed".to_string()));
    assert!(result.is_err());
    assert_eq!(counts(), (3, 2));
    job.unwrap().run();
    assert_eq!((state.android_run.phase(), counts()), (Phase::Idle, (3, 3)));

    // owner.lock still held after the agent exited: the slot stays Finishing, but the run's handles are released and
    // the activity ends with them; the next start begins a new one.
    let holder = hold_lock(&dir);
    running(&state, 40, None, &events);
    lock(&state.android_run.inner).activity = Some((state.android_run.begin_activity)());
    let job = {
        let mut inner = lock(&state.android_run.inner);
        begin_finishing(&state.android_run, &mut inner, 40, "task-stopped")
    };
    job.run();
    assert_eq!(state.android_run.phase(), Phase::Finishing { token: 40, instance_id: A.into(), released: true });
    assert_eq!((counts(), state.android_run.holds_activity()), ((4, 4), false));
    drop(holder);
    drop(claim(&state, A).unwrap());
    assert_eq!(counts(), (5, 5));

    // A start that panics while it holds the claim ends it while unwinding.
    let panicking = Arc::clone(&state);
    assert!(std::thread::spawn(move || {
        let _claimed = claim(&panicking, A).unwrap();
        panic!("start failed");
    })
    .join()
    .is_err());
    assert_eq!((state.android_run.phase(), counts()), (Phase::Idle, (6, 6)));
}
