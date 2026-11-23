//! macOS 连接在一次运行之后的归属规则。
//!
//! MaaNikki 的 Mac 控制模块记录自己按住的键和鼠标，旧连接必须先确认松开、再在后台丢弃，并且不能被
//! 下一次运行复用。这里只有纯粹的状态转换（控制器和 Tasker 用泛型表示），实例代码在持有锁时调用，
//! 测试用整数 ID 驱动各种先后顺序。

/// 一个实例上与连接归属有关的字段。
pub(crate) struct ConnectionSlots<'a, C, T> {
    pub controller: &'a mut Option<C>,
    pub tasker: &'a mut Option<T>,
    pub stopped: &'a mut bool,
    pub generation: &'a mut u64,
}

/// 运行结束（完成、失败或停止）时：连接不再可用，下次运行必须重新连接。
pub(crate) fn mark_run_finished<C, T>(slots: ConnectionSlots<'_, C, T>) {
    *slots.stopped = true;
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
        mark_run_finished(inst.slots());
        let scheduled = inst.generation; // release worker waits for the tasker here
        assert_eq!(inst.start_run(2), 2); // a quick restart gets a fresh connection
        assert!(take_for_release(inst.slots(), scheduled).is_none()); // the worker stands down
        assert_eq!(inst.retired, vec![1]); // the old controller is retired exactly once
        assert_eq!(inst.controller, Some(2));
    }

    #[test]
    fn release_first_then_restart_retires_once() {
        let mut inst = Instance::new(1);
        mark_run_finished(inst.slots());
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
    fn a_run_that_never_stopped_keeps_its_connection() {
        let mut inst = Instance::new(1);
        assert_eq!(inst.start_run(2), 1);
        assert!(inst.retired.is_empty());
    }
}
