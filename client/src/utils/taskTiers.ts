/**
 * Which task tier list applies to an instance: the one for the controller the instance has selected.
 * On a macOS backend an Adb controller (Android emulator) uses the Android tiers (get_android_task_tiers /
 * GET /api/android/task-tiers, read from agent/host/android/tasks.json); every other controller uses the Mac
 * tiers exactly as before (macTaskTiers.ts). Other backends load neither, so nothing is marked or refused.
 * Android marks carry `platform: 'android'` so the wording follows the mark; Mac marks are the unchanged
 * MacTaskMark objects from macTaskMark.
 */
import {
  MAC_EXPERIMENTAL_BADGE,
  MAC_EXPERIMENTAL_HINT,
  hasMacTaskTiers,
  macExcludedText,
  macStartBlockedReason,
  macTaskMark,
  type MacTaskMark,
  type MacTaskTiers,
} from '@/utils/macTaskTiers';

export const ANDROID_EXPERIMENTAL_BADGE = '安卓模拟器未验证（试用）';
export const ANDROID_EXPERIMENTAL_HINT =
  '这个任务可以在安卓模拟器上运行，但还没有在安卓模拟器上实机验证过，运行时请留意游戏画面。';
export const ANDROID_TIERS_PENDING = '安卓任务清单还没读取完，请稍后再开始。';
export const ANDROID_TIERS_MALFORMED = '无法读取安卓任务清单（后端返回的档位为空或格式不对）';
export const ANDROID_UNSUPPORTED_REASON = '暂不支持安卓模拟器。';
export const ANDROID_TIERS_UNREADABLE = '无法读取安卓任务清单';
export const ANDROID_EXCLUDED_PREFIX = '安卓不可用：';

export type TierPlatform = 'mac' | 'android';

/** A tier mark; `platform` is set only on Android marks, so Mac marks stay exactly what macTaskMark returns. */
export type TaskMark = MacTaskMark & { platform?: 'android' };

/** True only for an Adb controller on a macOS backend, the one case that uses the Android tiers. */
export function usesAndroidTiers(backendOS: string, controllerType: string | undefined): boolean {
  return backendOS === 'macos' && controllerType === 'Adb';
}

/** The platform whose wording the instance's marks use. */
export function tierPlatformFor(backendOS: string, controllerType: string | undefined): TierPlatform {
  return usesAndroidTiers(backendOS, controllerType) ? 'android' : 'mac';
}

/**
 * The tier list for an instance's controller: the Android tiers for Adb on macOS, otherwise the Mac tiers
 * (which are null off macOS, as before, so nothing is marked there).
 */
export function tiersForController(
  backendOS: string,
  controllerType: string | undefined,
  macTiers: MacTaskTiers | null | undefined,
  androidTiers: MacTaskTiers | null | undefined,
): MacTaskTiers | null {
  return (usesAndroidTiers(backendOS, controllerType) ? androidTiers : macTiers) ?? null;
}

/** How a pipeline entry is marked for this platform; the Mac case is macTaskMark unchanged. */
export function taskMarkFor(
  platform: TierPlatform,
  tiers: MacTaskTiers | null | undefined,
  entry: string | undefined,
): TaskMark | null {
  const mark = macTaskMark(tiers, entry);
  if (platform !== 'android' || !mark) return mark;
  if (mark.tier === 'excluded') {
    // Entries in no list are refused on Android too; their reason is the Android wording.
    return { tier: 'excluded', reason: tiers!.excluded[entry!] ?? ANDROID_UNSUPPORTED_REASON, platform: 'android' };
  }
  return { ...mark, platform: 'android' };
}

/** The text shown where a task cannot run; Mac marks give macExcludedText unchanged. */
export function taskExcludedText(mark: TaskMark | null): string {
  if (mark?.platform !== 'android') return macExcludedText(mark);
  return mark.tier === 'excluded' ? `${ANDROID_EXCLUDED_PREFIX}${mark.reason}` : '';
}

/** The experimental badge text for a mark's platform. */
export function experimentalBadgeText(mark: TaskMark | null): string {
  return mark?.platform === 'android' ? ANDROID_EXPERIMENTAL_BADGE : MAC_EXPERIMENTAL_BADGE;
}

/** The generic experimental hint for a mark's platform (the manifest note is added by the caller). */
export function experimentalHintText(mark: TaskMark | null): string {
  return mark?.platform === 'android' ? ANDROID_EXPERIMENTAL_HINT : MAC_EXPERIMENTAL_HINT;
}

/**
 * Why a run of an instance with this controller may not start yet. Adb on macOS checks the Android tiers;
 * every other case is macStartBlockedReason unchanged (empty off macOS).
 */
export function startBlockedReasonFor(
  backendOS: string,
  controllerType: string | undefined,
  macTiers: MacTaskTiers | null | undefined,
  androidTiers: MacTaskTiers | null | undefined,
): string {
  if (!usesAndroidTiers(backendOS, controllerType)) return macStartBlockedReason(backendOS, macTiers);
  if (!androidTiers) return ANDROID_TIERS_PENDING;
  if (androidTiers.error) return androidTiers.error;
  return hasMacTaskTiers(androidTiers) ? '' : ANDROID_TIERS_MALFORMED;
}

/**
 * Resolves once `settled()` holds (checked on every store change), immediately if it already does, or after
 * timeoutMs. The scheduler's wait over the Mac and, when needed, the Android tiers.
 */
export function whenSettled(
  settled: () => boolean,
  subscribe: (listener: () => void) => () => void,
  timeoutMs: number,
): Promise<void> {
  if (settled()) return Promise.resolve();
  return new Promise((resolve) => {
    let unsubscribe: () => void = () => {};
    const timer = setTimeout(() => {
      unsubscribe();
      resolve();
    }, timeoutMs);
    unsubscribe = subscribe(() => {
      if (!settled()) return;
      clearTimeout(timer);
      unsubscribe();
      resolve();
    });
  });
}
