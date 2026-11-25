//! While an Android run is active the client process holds a macOS `NSProcessInfo` activity, so App Nap and its
//! timer coalescing do not throttle the process that owns the MaaFramework controller.
//!
//! Why: MaaFramework 5.16.1 performs a swipe in `ControllerAgent::handle_swipe` in this process: duration / 10 steps,
//! each a `std::this_thread::sleep_for` of the rest of 10 ms followed by one MaaTouch move; a click is a touch down,
//! a 50 ms `sleep_for` and a touch up. In the windowless dev client (screen locked) a swipe step took about 190 ms
//! (600/800/1000 ms swipes took 11.3-11.5/14.9-15.3/19.0-19.2 s, about 19 times the duration in every swipe) and a
//! click about 0.22 s, while the headless runner, a CLI process that App Nap does not apply to, took about 1.3 times
//! the duration and 0.055 s. The finger stayed down for the whole stretched swipe. (Not yet verified on the device:
//! see docs/android-support-plan.md「实现和设备验收」.)
//!
//! The options: `NSActivityUserInitiated` turns App Nap off for the process (and with it the nap's timer coalescing)
//! and keeps the system from idle sleep, so a user who starts the dailies and walks away does not come back to a run
//! paused by sleep (coordinator's decision, 2026-11-26); the display may still sleep, and closing a laptop lid still
//! sleeps the Mac and pauses the run. `NSActivityLatencyCritical` asks for precise timers, since the swipe is a train
//! of 10 ms sleeps. Held only from the claim of a run to its retirement, so the cost is bounded by the run.

use std::sync::Arc;

use log::info;

/// A held activity; dropping it ends the activity.
pub(crate) type Activity = Box<dyn Send>;

/// Begins the activity of one Android run (injected so the unit tests count begins and ends).
pub(crate) type BeginActivity = Arc<dyn Fn() -> Activity + Send + Sync>;

/// The production activity: `NSProcessInfo.beginActivityWithOptions:reason:`.
pub(crate) fn process_activity() -> BeginActivity {
    Arc::new(|| Box::new(ProcessActivity::begin()) as Activity)
}

/// No App Nap, no idle system sleep, precise timers; display sleep stays allowed.
fn activity_options() -> objc2_foundation::NSActivityOptions {
    use objc2_foundation::NSActivityOptions;
    NSActivityOptions::UserInitiated | NSActivityOptions::LatencyCritical
}

struct ProcessActivity {
    token: objc2::rc::Retained<objc2::runtime::ProtocolObject<dyn objc2::runtime::NSObjectProtocol>>,
}

// SAFETY: the token is an opaque object only handed back to `endActivity:`. NSProcessInfo is thread-safe (Apple's
// Thread Safety Summary), and an activity may be ended on another thread than the one that began it.
unsafe impl Send for ProcessActivity {}

impl ProcessActivity {
    fn begin() -> Self {
        use objc2_foundation::{NSProcessInfo, NSString};
        let options = activity_options();
        let token = objc2::rc::autoreleasepool(|_| {
            let reason = NSString::from_str("MaaNikki is running tasks on an Android emulator");
            NSProcessInfo::processInfo().beginActivityWithOptions_reason(options, &reason)
        });
        info!("[android_run] process activity begun (App Nap off, idle system sleep off, latency critical) for the Android run");
        ProcessActivity { token }
    }
}

impl Drop for ProcessActivity {
    fn drop(&mut self) {
        objc2::rc::autoreleasepool(|_| {
            // SAFETY: the token was returned by beginActivityWithOptions:reason: and is ended exactly once.
            unsafe { objc2_foundation::NSProcessInfo::processInfo().endActivity(&self.token) };
        });
        info!("[android_run] process activity ended");
    }
}

#[cfg(test)]
mod tests {
    /// The real activity begins and ends without the framework (no device, no window).
    #[test]
    fn the_process_activity_begins_and_ends() {
        let activity = (super::process_activity())();
        drop(activity);
    }

    /// The run keeps the system from idle sleep but lets the display sleep.
    #[test]
    fn the_activity_blocks_idle_system_sleep_but_not_display_sleep() {
        use objc2_foundation::NSActivityOptions;
        let options = super::activity_options();
        assert!(options.contains(NSActivityOptions::IdleSystemSleepDisabled));
        assert!(options.contains(NSActivityOptions::UserInitiatedAllowingIdleSystemSleep));
        assert!(options.contains(NSActivityOptions::LatencyCritical));
        assert!(!options.contains(NSActivityOptions::IdleDisplaySleepDisabled));
    }
}
