//! macOS 连接在一次运行之后的归属规则。
//!
//! MaaNikki 的 Mac 控制模块记录自己按住的键和鼠标，旧连接必须先确认松开、再在后台丢弃，并且不能被
//! 下一次运行复用。Mac 控制器因此不进连接池（`pool_reuse_allowed`）：每次连接都是新的原生控制器，
//! 接上时由 `attach` 交回旧的交给后台退役。
//!
//! 同一实例上同时只有一轮 Mac 运行（`MacRun`）：启动请求在一个临界区里取得这一轮（`claim_start`），
//! 提交任务后转为运行中，结束回调把它转为收尾，agent 断开后由那次收尾释放；启动失败只撤销自己的
//! 那一轮。取得这一轮时上一轮 Tasker 的回调随即失效，新运行总是新建 Tasker。
//!
//! 这里只有纯粹的状态转换（控制器和 Tasker 用泛型表示），实例代码在持有锁时调用，测试用整数 ID
//! 驱动各种先后顺序。

/// 控制器能否进连接池、从池中复用。Mac 不能：池中复用会让刚结束或正在退役的连接被重新接上，
/// 每次连接都新建，旧连接经 `attach` 交给后台退役。
pub(crate) fn pool_reuse_allowed(config: &super::types::ControllerConfig) -> bool {
    !matches!(config, super::types::ControllerConfig::MacOS { .. })
}

/// 一个实例上与连接归属有关的字段。
pub(crate) struct ConnectionSlots<'a, C, T> {
    pub controller: &'a mut Option<C>,
    pub tasker: &'a mut Option<T>,
    pub stopped: &'a mut bool,
    pub generation: &'a mut u64,
}

/// 一次 Mac 运行结束时取走的东西：这次运行的 agent 和释放应针对的代数。
pub(crate) struct FinishedRun<A, B> {
    pub generation: u64,
    pub clients: Vec<A>,
    pub children: Vec<B>,
}

/// 实例上这一轮 Mac 运行的状态，`None` 表示空闲。启动和运行用启动请求的令牌标识，收尾用代数标识。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum MacRun {
    /// 启动请求已取得这一轮，正在重连、启动 agent、提交任务。
    Starting(u64),
    /// 任务已提交，由这一轮 Tasker 的结束回调收尾。
    Running(u64),
    /// 已取走这一轮的 agent，正在断开（反注册动作）。
    Cleanup(u64),
}

/// 启动请求在一个临界区里取得这一轮（判断与取得不能分开）。上一轮仍在启动、仍在运行
/// （`run_active`：任务状态仍是运行中）或仍在收尾时不能取得。用户停止后结束回调没有到达的那一轮
/// 可以被接替：取得时 `tasker_serial` 加一，它的 Tasker 的回调随即失效，新运行总是新建 Tasker。
pub(crate) fn claim_start(
    run: &mut Option<MacRun>,
    run_active: bool,
    token: u64,
    tasker_serial: &mut u64,
) -> bool {
    match *run {
        Some(MacRun::Starting(_)) | Some(MacRun::Cleanup(_)) => return false,
        Some(MacRun::Running(_)) if run_active => return false,
        _ => {}
    }
    *run = Some(MacRun::Starting(token));
    *tasker_serial = tasker_serial.wrapping_add(1);
    true
}

/// 任务已提交（与提交在同一个临界区里）：这一轮转为运行中，之后由它的结束回调收尾。
pub(crate) fn start_running(run: &mut Option<MacRun>, token: u64) {
    if *run == Some(MacRun::Starting(token)) {
        *run = Some(MacRun::Running(token));
    }
}

/// 启动失败、取消或没有提交任何任务：只撤销自己取得的那一轮。
pub(crate) fn abandon_start(run: &mut Option<MacRun>, token: u64) {
    if *run == Some(MacRun::Starting(token)) {
        *run = None;
    }
}

/// Mac 运行结束：在判定运行结束的同一个临界区里（持有实例锁）标记连接不可用、代数加一（与停止
/// agent 时相同），取走这次运行的 agent，让这次运行的 Tasker 的回调从此失效（`tasker_serial`
/// 加一），并把这一轮转为收尾（记下这一代，只有这次收尾能释放：`clear_cleanup`）。之后的清理只
/// 针对取走的这些和这一代。
pub(crate) fn finish_run<C, T, A, B>(
    slots: ConnectionSlots<'_, C, T>,
    clients: &mut Vec<A>,
    children: &mut Vec<B>,
    run: &mut Option<MacRun>,
    tasker_serial: &mut u64,
) -> FinishedRun<A, B> {
    *slots.stopped = true;
    *slots.generation = slots.generation.wrapping_add(1);
    *tasker_serial = tasker_serial.wrapping_add(1);
    *run = Some(MacRun::Cleanup(*slots.generation));
    FinishedRun {
        generation: *slots.generation,
        clients: std::mem::take(clients),
        children: std::mem::take(children),
    }
}

/// 收尾完成（agent 已断开）：只释放这次收尾自己的那一轮。
pub(crate) fn clear_cleanup(run: &mut Option<MacRun>, generation: u64) {
    if *run == Some(MacRun::Cleanup(generation)) {
        *run = None;
    }
}

/// Mac 回调能否改动实例：只有当前 Tasker 的回调可以。Tasker 被换掉、交给退役（`attach` 取走），
/// 它的运行已经结束（`finish_run`），或新的启动已取得这一轮（`claim_start`）后，迟到的回调一律忽略。
pub(crate) fn callback_is_current(tasker_serial: u64, has_tasker: bool, callback_serial: u64) -> bool {
    has_tasker && tasker_serial == callback_serial
}

/// 接上新的控制器：代数加一，交回旧的控制器与 Tasker，由调用方在锁外交给后台退役。
/// `same` 判断新旧是否为同一个原生控制器（池中命中时是同一个），同一个时不退役。
pub(crate) fn attach<C, T>(
    slots: ConnectionSlots<'_, C, T>,
    new: C,
    same: impl Fn(&C, &C) -> bool,
) -> (Option<C>, Option<T>) {
    *slots.generation = slots.generation.wrapping_add(1);
    *slots.stopped = false;
    let old_tasker = slots.tasker.take();
    let old = slots.controller.replace(new);
    let old = old.filter(|old| !same(old, slots.controller.as_ref().unwrap()));
    (old, old_tasker)
}

/// 释放线程取走句柄：只有代数仍是安排释放时的那一代才取走；否则说明已开始新的运行，旧句柄已由
/// `attach` 交回退役，这里什么也不做。
pub(crate) fn take_for_release<C, T>(
    slots: ConnectionSlots<'_, C, T>,
    expected_generation: u64,
) -> Option<(Option<C>, Option<T>)> {
    if *slots.generation != expected_generation {
        return None;
    }
    *slots.stopped = true;
    Some((slots.controller.take(), slots.tasker.take()))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 一个实例的字段，加上被退役的控制器 ID。
    struct Instance {
        controller: Option<u32>,
        tasker: Option<u32>,
        stopped: bool,
        generation: u64,
        retired: Vec<u32>,
    }

    impl Instance {
        fn new(controller: u32) -> Self {
            Self { controller: Some(controller), tasker: Some(100 + controller), stopped: false, generation: 1, retired: vec![] }
        }
        fn slots(&mut self) -> ConnectionSlots<'_, u32, u32> {
            ConnectionSlots {
                controller: &mut self.controller,
                tasker: &mut self.tasker,
                stopped: &mut self.stopped,
                generation: &mut self.generation,
            }
        }
        /// 下次运行开始：已停止的连接一律重新连接（这里新控制器用 `next` 表示）。
        fn start_run(&mut self, next: u32) -> u32 {
            if self.stopped {
                let (old, _) = attach(self.slots(), next, |a, b| a == b);
                self.retired.extend(old);
            }
            self.controller.unwrap()
        }
    }

    #[test]
    fn restart_while_release_waits_never_reuses_the_old_controller() {
        let mut inst = Instance::new(1);
        finish_run(inst.slots(), &mut Vec::<u8>::new(), &mut Vec::<u8>::new(), &mut None, &mut 0);
        let scheduled = inst.generation; // release worker waits for the tasker here
        assert_eq!(inst.start_run(2), 2); // a quick restart gets a fresh connection
        assert!(take_for_release(inst.slots(), scheduled).is_none()); // the worker stands down
        assert_eq!(inst.retired, vec![1]); // the old controller is retired exactly once
        assert_eq!(inst.controller, Some(2));
    }

    #[test]
    fn release_first_then_restart_retires_once() {
        let mut inst = Instance::new(1);
        finish_run(inst.slots(), &mut Vec::<u8>::new(), &mut Vec::<u8>::new(), &mut None, &mut 0);
        let scheduled = inst.generation;
        let (taken, tasker) = take_for_release(inst.slots(), scheduled).unwrap();
        inst.retired.extend(taken);
        assert_eq!(tasker, Some(101));
        assert_eq!(inst.start_run(2), 2);
        assert_eq!(inst.retired, vec![1]);
    }

    #[test]
    fn reattaching_the_same_controller_retires_nothing() {
        let mut inst = Instance::new(1);
        let (old, _) = attach(inst.slots(), 1, |a, b| a == b);
        assert_eq!(old, None);
        let (old, tasker) = attach(inst.slots(), 3, |a, b| a == b);
        assert_eq!((old, tasker), (Some(1), None));
    }

    #[test]
    fn cleanup_of_a_finished_run_never_touches_the_next_one() {
        let mut inst = Instance::new(1);
        let mut clients = vec![11];
        let mut children = vec![21];
        let mut pending = None;
        let finished = finish_run(inst.slots(), &mut clients, &mut children, &mut pending, &mut 0);
        assert_eq!(pending, Some(MacRun::Cleanup(finished.generation)));
        // A new run starts before the old one's cleanup runs: fresh controller, fresh agents.
        assert_eq!(inst.start_run(2), 2);
        clients.push(12);
        children.push(22);
        // The old cleanup only has what it took, and its release targets a generation that is gone.
        assert_eq!((finished.clients, finished.children), (vec![11], vec![21]));
        assert!(take_for_release(inst.slots(), finished.generation).is_none());
        assert_eq!((clients, children), (vec![12], vec![22]));
        assert_eq!(inst.controller, Some(2));
        assert_eq!(inst.retired, vec![1]);
    }

    #[test]
    fn without_a_new_run_the_release_takes_the_finished_connection() {
        let mut inst = Instance::new(1);
        let finished = finish_run(inst.slots(), &mut Vec::<u8>::new(), &mut Vec::<u8>::new(), &mut None, &mut 0);
        let (taken, _) = take_for_release(inst.slots(), finished.generation).unwrap();
        assert_eq!(taken, Some(1));
    }

    #[test]
    fn mac_controllers_never_use_the_pool() {
        use crate::commands::types::ControllerConfig;
        let mac: ControllerConfig = serde_json::from_value(serde_json::json!({
            "type": "MacOS", "handle": 1, "screencap_method": 2, "input_method": 3
        }))
        .unwrap();
        assert!(!pool_reuse_allowed(&mac));
    }

    #[test]
    fn a_run_that_never_stopped_keeps_its_connection() {
        let mut inst = Instance::new(1);
        assert_eq!(inst.start_run(2), 1);
        assert!(inst.retired.is_empty());
    }

    /// 一个实例上与一轮 Mac 运行有关的字段，按生产代码的步骤分开操作，便于把交错固定下来。
    struct Runs {
        inst: Instance,
        serial: u64,
        has_tasker: bool,
        status_running: bool,
        run: Option<MacRun>,
        clients: Vec<u32>,
    }

    impl Runs {
        /// 第一次连接后、还没有运行过：没有 Tasker。
        fn idle() -> Self {
            Self { inst: Instance::new(1), serial: 0, has_tasker: false, status_running: false, run: None, clients: vec![] }
        }
        /// start_tasks_impl 开头的临界区。
        fn claim(&mut self, token: u64) -> bool {
            let active = self.status_running;
            claim_start(&mut self.run, active, token, &mut self.serial)
        }
        /// 取得之后：已停止或有旧 Tasker 就重连（旧 Tasker 交给退役），再新建 Tasker；返回它的序号。
        fn connect_and_create_tasker(&mut self, next_controller: u32) -> u64 {
            if self.inst.stopped || self.has_tasker {
                self.inst.stopped = true;
                self.inst.start_run(next_controller);
                self.has_tasker = false;
            }
            self.serial += 1;
            self.has_tasker = true;
            self.serial
        }
        /// 提交任务（同一个临界区里转为运行中）。
        fn post(&mut self, token: u64) {
            self.status_running = true;
            start_running(&mut self.run, token);
        }
        /// 一次完整的启动；返回这一轮 Tasker 的序号。
        fn start(&mut self, token: u64, agent: u32, next_controller: u32) -> u64 {
            assert!(self.claim(token));
            let serial = self.connect_and_create_tasker(next_controller);
            self.clients.push(agent);
            self.post(token);
            serial
        }
        /// 结束回调（与 handle_task_callback 的判断相同）。
        fn complete(&mut self, callback_serial: u64) -> Option<FinishedRun<u32, u8>> {
            if !callback_is_current(self.serial, self.has_tasker, callback_serial) {
                return None;
            }
            self.status_running = false;
            Some(finish_run(self.inst.slots(), &mut self.clients, &mut Vec::new(), &mut self.run, &mut self.serial))
        }
    }

    #[test]
    fn two_starts_cannot_both_claim_the_run() {
        let mut runs = Runs::idle();
        assert!(runs.claim(1)); // A releases the lock to reconnect and start its agent
        assert!(!runs.claim(2)); // B, arriving meanwhile, is held off
        abandon_start(&mut runs.run, 2); // B giving up does not release A's claim
        assert_eq!(runs.run, Some(MacRun::Starting(1)));
        abandon_start(&mut runs.run, 1); // A's start fails
        assert!(runs.claim(2));
    }

    #[test]
    fn a_start_that_waited_for_the_last_run_reconnects_and_keeps_its_own_callbacks() {
        let mut runs = Runs::idle();
        let a = runs.start(1, 11, 2);
        assert!(!runs.claim(2)); // A is still running: B waits outside the lock
        let finished = runs.complete(a).unwrap();
        assert!(!runs.claim(2)); // A's agent is still being disconnected
        clear_cleanup(&mut runs.run, finished.generation);
        // B's reconnect is decided after its claim, so it sees A's stopped connection.
        assert!(runs.claim(2));
        let b = runs.connect_and_create_tasker(3);
        assert_eq!(runs.inst.controller, Some(3));
        runs.clients.push(12);
        runs.post(2);
        assert!(runs.complete(a).is_none()); // a duplicate of A's end
        let finished_b = runs.complete(b).unwrap(); // B's own end still finishes B
        assert_eq!((finished.clients, finished_b.clients), (vec![11], vec![12]));
    }

    #[test]
    fn only_the_finishing_cleanup_releases_the_run() {
        let mut runs = Runs::idle();
        let a = runs.start(1, 11, 2);
        let finished = runs.complete(a).unwrap();
        clear_cleanup(&mut runs.run, finished.generation.wrapping_add(7));
        assert!(!runs.claim(2));
        clear_cleanup(&mut runs.run, finished.generation);
        assert!(runs.claim(2));
    }

    #[test]
    fn a_stopped_run_whose_end_never_came_can_be_taken_over_and_stays_silent() {
        let mut runs = Runs::idle();
        let a = runs.start(1, 11, 2);
        runs.status_running = false; // the user stopped A; its end callback has not arrived
        runs.inst.stopped = true;
        assert!(runs.claim(2)); // A's callbacks stop counting at this very moment
        assert!(runs.complete(a).is_none()); // before B even reconnects
        let b = runs.connect_and_create_tasker(3);
        runs.post(2);
        assert!(runs.complete(a).is_none()); // and after
        assert!(runs.complete(b).is_some());
    }

    #[test]
    fn a_callback_while_the_tasker_is_handed_over_is_ignored() {
        assert!(!callback_is_current(1, false, 1));
        assert!(!callback_is_current(2, true, 1));
        assert!(callback_is_current(2, true, 2));
    }
}
