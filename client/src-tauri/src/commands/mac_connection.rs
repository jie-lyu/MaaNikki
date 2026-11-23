//! macOS 连接在一次运行之后的归属规则。
//!
//! MaaNikki 的 Mac 控制模块记录自己按住的键和鼠标，旧连接必须先确认松开、再在后台丢弃，并且不能被
//! 下一次运行复用。Mac 控制器因此不进连接池（`pool_reuse_allowed`）：每次连接都是新的原生控制器，
//! 接上时由 `attach` 交回旧的交给后台退役。这里只有纯粹的状态转换（控制器和 Tasker 用泛型表示），
//! 实例代码在持有锁时调用，测试用整数 ID 驱动各种先后顺序。

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

/// Mac 运行结束：在判定运行结束的同一个临界区里（持有实例锁）标记连接不可用、代数加一（与停止
/// agent 时相同），取走这次运行的 agent，并让这次运行的 Tasker 的回调从此失效（`tasker_serial`
/// 加一）。收尾标记记下这一代，只有这次收尾能清除它（`clear_cleanup`）。之后的清理只针对取走的
/// 这些和这一代，紧接着开始的新运行不受影响。
pub(crate) fn finish_run<C, T, A, B>(
    slots: ConnectionSlots<'_, C, T>,
    clients: &mut Vec<A>,
    children: &mut Vec<B>,
    cleanup: &mut Option<u64>,
    tasker_serial: &mut u64,
) -> FinishedRun<A, B> {
    *slots.stopped = true;
    *slots.generation = slots.generation.wrapping_add(1);
    *tasker_serial = tasker_serial.wrapping_add(1);
    *cleanup = Some(*slots.generation);
    FinishedRun {
        generation: *slots.generation,
        clients: std::mem::take(clients),
        children: std::mem::take(children),
    }
}

/// 收尾完成（agent 已断开）：只清除这次收尾自己的标记。
pub(crate) fn clear_cleanup(cleanup: &mut Option<u64>, generation: u64) {
    if *cleanup == Some(generation) {
        *cleanup = None;
    }
}

/// Mac 回调能否改动实例：只有当前 Tasker 的回调可以。Tasker 被换掉、交给退役（`attach` 取走），
/// 或它的运行已经结束（`finish_run`）后，迟到的回调一律忽略。
pub(crate) fn callback_is_current(tasker_serial: u64, has_tasker: bool, callback_serial: u64) -> bool {
    has_tasker && tasker_serial == callback_serial
}

/// 新的 Mac 运行（重置状态）能否在这个临界区里取得启动权：上一轮仍在运行或仍在收尾时不能。
/// 调用方必须在同一个临界区里判断并取得启动权，不能先判断、放锁、再取得。
pub(crate) fn start_blocked(cleanup: Option<u64>, running: bool) -> bool {
    cleanup.is_some() || running
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
        assert_eq!(pending, Some(finished.generation));
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

    /// 实例上与一轮运行的开始和结束有关的字段：Tasker 序号、是否有 Tasker、运行中、收尾标记、agent。
    struct Runs {
        inst: Instance,
        serial: u64,
        has_tasker: bool,
        running: bool,
        cleanup: Option<u64>,
        clients: Vec<u32>,
    }

    impl Runs {
        /// 一轮运行正在进行：Tasker 序号 1，agent 11。
        fn running() -> Self {
            Self { inst: Instance::new(1), serial: 1, has_tasker: true, running: true, cleanup: None, clients: vec![11] }
        }
        /// 完成回调：只有当前 Tasker 的回调能结束这一轮（与 handle_task_callback 的判断相同）。
        fn complete(&mut self, callback_serial: u64) -> Option<FinishedRun<u32, u8>> {
            if !callback_is_current(self.serial, self.has_tasker, callback_serial) || !self.running {
                return None;
            }
            self.running = false;
            Some(finish_run(self.inst.slots(), &mut self.clients, &mut Vec::new(), &mut self.cleanup, &mut self.serial))
        }
        /// 新运行在一个临界区里判断并取得启动权：重新连接（交回旧 Tasker）、新建 Tasker、启动 agent。
        fn claim(&mut self, agent: u32) -> bool {
            if start_blocked(self.cleanup, self.running) {
                return false;
            }
            if self.inst.stopped {
                self.has_tasker = false; // attach hands the old tasker over for retirement
                self.inst.start_run(2);
            }
            self.serial += 1;
            self.has_tasker = true;
            self.clients.push(agent);
            self.running = true;
            true
        }
    }

    #[test]
    fn a_new_run_waits_for_the_finished_run_to_unregister_its_agent() {
        let mut runs = Runs::running();
        let finished = runs.complete(1).unwrap();
        assert!(!runs.claim(12)); // A's agent is still being disconnected
        clear_cleanup(&mut runs.cleanup, finished.generation.wrapping_add(7)); // someone else's cleanup
        assert!(!runs.claim(12));
        clear_cleanup(&mut runs.cleanup, finished.generation); // A's own cleanup, after the disconnect
        assert!(runs.claim(12));
        assert_eq!((finished.clients, runs.clients), (vec![11], vec![12]));
    }

    #[test]
    fn a_late_callback_of_a_finished_run_never_ends_the_next_one() {
        let mut runs = Runs::running();
        let finished = runs.complete(1).unwrap();
        assert!(runs.complete(1).is_none()); // a duplicate terminal callback of the same run
        clear_cleanup(&mut runs.cleanup, finished.generation);
        assert!(runs.claim(12));
        // A's tasker reports once more (a late task end or its internal post_stop): B is untouched.
        assert!(runs.complete(1).is_none());
        assert!(runs.running);
        assert_eq!(runs.clients, vec![12]);
        assert_eq!(runs.cleanup, None);
    }

    #[test]
    fn a_run_cannot_be_claimed_over_one_still_running() {
        let mut runs = Runs::running();
        assert!(!runs.claim(12)); // B would otherwise share A's tasker and lose its agent to A's end
        assert!(runs.complete(1).is_some());
        assert_eq!(runs.clients, Vec::<u32>::new());
    }

    #[test]
    fn a_callback_while_the_tasker_is_handed_over_is_ignored() {
        // A user stop leaves A's tasker current until its own end callback; a quick restart
        // reconnects (the tasker goes to retirement) before that callback arrives.
        assert!(!callback_is_current(1, false, 1));
        assert!(!callback_is_current(2, true, 1));
        assert!(callback_is_current(2, true, 2));
    }
}
