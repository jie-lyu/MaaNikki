import { useAppStore } from '@/stores/appStore';
import {
  androidTaskTiersPendingFor,
  macTaskTiersPending,
  waitForAndroidTaskTiers,
  waitForMacTaskTiers,
} from '@/services/macTaskTiersService';
import { loggers } from '@/utils';
import type { Instance } from '@/types/interface';

const log = loggers.task;

const STORAGE_KEY_TRIGGERED = 'mxu_schedule_triggeredSlots';

const CHECK_INTERVAL_MS = 30_000; // 每 30 秒轮询一次（分钟精度下降低到点延迟）
const SLOT_TTL_MS = 48 * 60 * 60 * 1000; // 触发记录保留 48 小时
const DEBOUNCE_MS = 2_000; // 事件触发后 2 秒内去重

export type ScheduleTriggerCallback = (
  instance: Instance,
  policyName: string,
  slotLabel: string,
) => Promise<boolean>;

function formatSlotKey(date: Date): string {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, '0');
  const d = String(date.getDate()).padStart(2, '0');
  const h = String(date.getHours()).padStart(2, '0');
  const mi = String(date.getMinutes()).padStart(2, '0');
  return `${y}-${m}-${d}-${h}-${mi}`;
}

function minuteStart(date: Date): Date {
  return new Date(
    date.getFullYear(),
    date.getMonth(),
    date.getDate(),
    date.getHours(),
    date.getMinutes(),
  );
}

function buildTriggeredSlotKey(instanceId: string, slotStr: string): string {
  return `${instanceId}:${slotStr}`;
}

function normalizeTriggeredSlotKey(key: string): string | null {
  const lastColon = key.lastIndexOf(':');
  if (lastColon <= 0) {
    return null;
  }

  const slotStr = key.substring(lastColon + 1);
  if (!/^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}$/.test(slotStr)) {
    return null;
  }

  const prefix = key.substring(0, lastColon);
  const firstColon = prefix.indexOf(':');
  const instanceId = firstColon === -1 ? prefix : prefix.substring(0, firstColon);

  return instanceId ? buildTriggeredSlotKey(instanceId, slotStr) : null;
}

class ScheduleService {
  private intervalId: ReturnType<typeof setInterval> | null = null;
  private checking = false;
  private triggerFn: ScheduleTriggerCallback | null = null;
  private debounceTimer: ReturnType<typeof setTimeout> | null = null;
  /**
   * Android emulator (macOS only): slot keys of due Adb instances waiting for the Android task tiers outside `checking`
   * (decideAfterAndroidTiers). A check meanwhile leaves them to that wait. Only Adb slot keys ever enter it.
   */
  private androidWaits = new Set<string>();
  /** Bumped whenever decideAfterAndroidTiers saves a slot, so a check running meanwhile merges instead of overwriting. */
  private androidSlotSaves = 0;
  /**
   * The slots of the check in progress, including those it triggered but has not saved yet; null between checks.
   * decideAfterAndroidTiers looks at it before triggering, so neither path triggers a slot the other already did.
   */
  private checkSlots: Set<string> | null = null;

  private getTriggeredSlots(): Set<string> {
    try {
      const val = localStorage.getItem(STORAGE_KEY_TRIGGERED);
      if (!val) {
        return new Set();
      }

      const rawSlots: unknown = JSON.parse(val);
      if (!Array.isArray(rawSlots)) {
        return new Set();
      }

      const normalized = new Set<string>();
      let changed = false;

      for (const item of rawSlots) {
        if (typeof item !== 'string') {
          changed = true;
          continue;
        }

        const normalizedKey = normalizeTriggeredSlotKey(item);
        if (!normalizedKey) {
          changed = true;
          continue;
        }

        normalized.add(normalizedKey);
        if (normalizedKey !== item) {
          changed = true;
        }
      }

      if (changed) {
        this.setTriggeredSlots(normalized);
      }

      return normalized;
    } catch {
      return new Set();
    }
  }

  private setTriggeredSlots(slots: Set<string>) {
    localStorage.setItem(STORAGE_KEY_TRIGGERED, JSON.stringify([...slots]));
  }

  private cleanupOldSlots() {
    const slots = this.getTriggeredSlots();
    if (slots.size === 0) return;

    const cutoff = Date.now() - SLOT_TTL_MS;
    const cleaned = new Set<string>();

    for (const key of slots) {
      // 当前格式: instanceId:YYYY-MM-DD-HH-mm
      const lastColon = key.lastIndexOf(':');
      const slotStr = key.substring(lastColon + 1);
      const [y, mo, d, h, mi] = slotStr.split('-').map(Number);
      const slotTs = new Date(y, mo - 1, d, h, mi).getTime();
      if (slotTs >= cutoff) {
        cleaned.add(key);
      }
    }

    if (cleaned.size !== slots.size) {
      this.setTriggeredSlots(cleaned);
    }
  }

  setTriggerCallback(fn: ScheduleTriggerCallback | null) {
    this.triggerFn = fn;
  }

  start() {
    if (this.intervalId) return;

    log.info('[调度器] 启动，轮询间隔', CHECK_INTERVAL_MS / 1000, '秒');

    this.check();
    this.intervalId = setInterval(() => this.check(), CHECK_INTERVAL_MS);

    document.addEventListener('visibilitychange', this.handleVisibilityChange);
    window.addEventListener('focus', this.handleFocus);
  }

  stop() {
    if (this.intervalId) {
      clearInterval(this.intervalId);
      this.intervalId = null;
    }
    if (this.debounceTimer) {
      clearTimeout(this.debounceTimer);
      this.debounceTimer = null;
    }

    document.removeEventListener('visibilitychange', this.handleVisibilityChange);
    window.removeEventListener('focus', this.handleFocus);

    log.info('[调度器] 已停止');
  }

  private handleVisibilityChange = () => {
    if (document.visibilityState === 'visible') {
      log.info('[调度器] 窗口可见，触发检查');
      this.debouncedCheck();
    }
  };

  private handleFocus = () => {
    log.info('[调度器] 窗口获得焦点，触发检查');
    this.debouncedCheck();
  };

  private debouncedCheck() {
    if (this.debounceTimer) {
      clearTimeout(this.debounceTimer);
    }
    this.debounceTimer = setTimeout(() => {
      this.debounceTimer = null;
      this.check();
    }, DEBOUNCE_MS);
  }

  async check() {
    if (this.checking || !this.triggerFn) return;
    this.checking = true;
    let deferredWait: Promise<void> | null = null;

    try {
      const now = new Date();
      const currentSlot = minuteStart(now);
      const weekday = currentSlot.getDay();
      const timeStr = `${String(currentSlot.getHours()).padStart(2, '0')}:${String(
        currentSlot.getMinutes(),
      ).padStart(2, '0')}`;
      const slotStr = formatSlotKey(currentSlot);

      // macOS only, and only while the task tiers are still loading: the slot is fixed above; wait for them
      // before deciding, so a start refused only because they were loading does not use up the slot. Other
      // backends never wait here (no yield, the same order as before). After the wait the service must be
      // running: stopped, this check records nothing and calls nothing; stopped and started again (whose own
      // immediate check was skipped while this one held `checking`), it decides the fixed slot for the running
      // service with the handler registered now, so the restart does not lose a due slot.
      if (macTaskTiersPending()) {
        await waitForMacTaskTiers();
        if (!this.intervalId || !this.triggerFn) return;
      }

      this.cleanupOldSlots();
      const triggeredSlots = this.getTriggeredSlots();
      let androidSavesAtRead = this.androidSlotSaves;
      let slotsModified = false;
      this.checkSlots = triggeredSlots;

      const { instances } = useAppStore.getState();
      // Android emulator (macOS only): due Adb instances whose Android task tiers are still loading, decided after
      // every other instance (below), so they never delay or block a due Win32 or native Mac instance whatever the
      // instance order. androidTaskTiersPendingFor looks at the controller type first and never reads the Android
      // tiers for any other instance; off macOS it is always false and this loop is the one from before.
      const deferred: string[] = [];

      for (const inst of instances) {
        const policies = inst.schedulePolicies || [];

        for (const policy of policies) {
          if (!policy.enabled) continue;
          if (!policy.weekdays.includes(weekday)) continue;
          if (!policy.times?.includes(timeStr)) continue;

          const slotKey = buildTriggeredSlotKey(inst.id, slotStr);
          // An Android tier wait saved a slot since the read above (while an earlier trigger was awaited, say): take
          // it in before deciding, so a slot is triggered at most once per due minute. Nothing below awaits before
          // the trigger. Never true unless an Adb instance waited, so native-only checks read nothing more.
          if (this.androidSlotSaves !== androidSavesAtRead) {
            androidSavesAtRead = this.androidSlotSaves;
            for (const key of this.getTriggeredSlots()) triggeredSlots.add(key);
          }
          if (triggeredSlots.has(slotKey)) break;

          const freshInst = useAppStore.getState().instances.find((i) => i.id === inst.id);
          if (!freshInst) continue;

          if (freshInst.isRunning) {
            log.info(
              `[调度器] 实例 "${inst.name}" 正在运行，跳过时间槽 ${slotStr} 策略 "${policy.name}"`,
            );
            triggeredSlots.add(slotKey);
            slotsModified = true;
            break;
          }

          if (this.androidWaits.has(slotKey) || androidTaskTiersPendingFor(freshInst.id)) {
            if (!this.androidWaits.has(slotKey)) deferred.push(inst.id);
            break;
          }

          log.info(
            `[调度器] 准时触发: 时间槽 ${slotStr}, 实例 "${inst.name}", 策略 "${policy.name}"`,
          );

          triggeredSlots.add(slotKey);
          slotsModified = true;

          try {
            await this.triggerFn(freshInst, policy.name, timeStr);
          } catch (err) {
            log.error(`[调度器] 触发失败:`, err);
          }

          // 每个实例每个时间槽只执行第一个匹配策略
          break;
        }
      }

      if (slotsModified) {
        if (this.androidSlotSaves !== androidSavesAtRead) {
          // An Android tier wait saved a slot during this check: keep it.
          for (const key of this.getTriggeredSlots()) triggeredSlots.add(key);
        }
        this.setTriggeredSlots(triggeredSlots);
      }
      this.checkSlots = null;

      // The deferred Adb instances wait for the Android tiers once, without holding `checking`: a check in the next
      // minute (a native Mac instance due then, say) runs meanwhile.
      if (deferred.length > 0) {
        const keys = deferred.map((id) => buildTriggeredSlotKey(id, slotStr));
        for (const key of keys) this.androidWaits.add(key);
        deferredWait = this.decideAfterAndroidTiers(deferred, weekday, timeStr, slotStr).finally(() => {
          for (const key of keys) this.androidWaits.delete(key);
        });
      }
    } finally {
      this.checkSlots = null;
      this.checking = false;
    }
    if (deferredWait) await deferredWait;
  }

  /**
   * Decides due Adb instances once the Android tiers settled. Stopped meanwhile: nothing is recorded or started.
   * Otherwise each is decided again from the store as it is now (its policies, enabled flags and running state may
   * have changed during the wait), and its slot is saved before it is started, so no other check starts it again.
   */
  private async decideAfterAndroidTiers(deferred: string[], weekday: number, timeStr: string, slotStr: string) {
    await waitForAndroidTaskTiers();
    if (!this.intervalId || !this.triggerFn) return;
    for (const id of deferred) {
      const inst = useAppStore.getState().instances.find((i) => i.id === id);
      const policy = inst?.schedulePolicies?.find(
        (p) => p.enabled && p.weekdays.includes(weekday) && p.times?.includes(timeStr),
      );
      if (!inst || !policy) continue;
      const slotKey = buildTriggeredSlotKey(inst.id, slotStr);
      const triggeredSlots = this.getTriggeredSlots();
      // Saved, or triggered by a check still in progress (saved when it ends): already used for this minute.
      if (triggeredSlots.has(slotKey) || this.checkSlots?.has(slotKey)) continue;
      triggeredSlots.add(slotKey);
      this.setTriggeredSlots(triggeredSlots);
      this.androidSlotSaves += 1;
      if (inst.isRunning) {
        log.info(`[调度器] 实例 "${inst.name}" 正在运行，跳过时间槽 ${slotStr} 策略 "${policy.name}"`);
        continue;
      }
      log.info(`[调度器] 准时触发: 时间槽 ${slotStr}, 实例 "${inst.name}", 策略 "${policy.name}"`);
      try {
        await this.triggerFn(inst, policy.name, timeStr);
      } catch (err) {
        log.error(`[调度器] 触发失败:`, err);
      }
    }
  }
}

export const scheduleService = new ScheduleService();
