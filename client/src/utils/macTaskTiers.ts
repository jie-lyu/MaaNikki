/**
 * Mac task tiers as the backend reports them (get_macos_task_tiers / GET /api/macos/task-tiers), read from
 * agent/host/macos/preview_tasks.json. Only a macOS backend returns tiers; Windows and Linux get empty lists,
 * which means no markings anywhere. The per-task consequence lines live in the manifest's notes, so this file
 * holds only the generic wording.
 */

export interface MacTaskTiers {
  verified: string[];
  experimental: string[];
  excluded: Record<string, string>;
  notes: Record<string, string>;
  /** Set when a macOS backend could not read the manifest; every task is then refused before submission. */
  error?: string;
}

export const MAC_EXPERIMENTAL_BADGE = 'Mac 未验证（试用）';
export const MAC_EXPERIMENTAL_HINT = '这个任务可以在 Mac 上运行，但还没有在 Mac 上实机验证过，运行时请留意游戏画面。';
export const MAC_TIERS_PENDING = 'Mac 任务清单还没读取完，请稍后再开始。';
export const MAC_TIERS_MALFORMED = '无法读取 Mac 任务清单（后端返回的档位为空或格式不对）';
export const MAC_UNSUPPORTED_REASON = '暂不支持 Mac 测试预览版。';
export const MAC_TIERS_UNREADABLE = '无法读取 Mac 任务清单';

// The start notice for experimental tasks is written by the agent into the running task's own log
// (agent/registration.py), so it reaches scheduled runs and background instances too.

/**
 * Why a run may not start yet on this backend: on macOS until the tiers have loaded (the markings and the
 * per-task consequence notes must be visible before anything starts) or when they could not be read.
 * Empty on Windows, Linux and the dev preview without a backend, which have no tiers.
 */
export function macStartBlockedReason(backendOS: string, tiers: MacTaskTiers | null | undefined): string {
  if (backendOS !== 'macos') return '';
  if (!tiers) return MAC_TIERS_PENDING;
  if (tiers.error) return tiers.error;
  return hasMacTaskTiers(tiers) ? '' : MAC_TIERS_MALFORMED;
}

/**
 * Resolves once the Mac task tiers are settled (loaded or failed), immediately on other backends. The
 * scheduler waits on it so a slot that falls while the tiers are still loading is not used up by a refused
 * start. Gives up after timeoutMs; the start gate then refuses and says why. The store is passed in.
 */
export function macTaskTiersSettled(
  getState: () => { backendOS: string; macTaskTiers: MacTaskTiers | null },
  subscribe: (listener: () => void) => () => void,
  timeoutMs: number,
): Promise<void> {
  const settled = () => {
    const state = getState();
    return state.backendOS !== 'macos' || state.macTaskTiers !== null;
  };
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

export type MacTaskMark =
  | { tier: 'verified'; note?: string }
  | { tier: 'experimental'; note?: string }
  | { tier: 'excluded'; reason: string };

const strings = (value: unknown): string[] =>
  Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string' && item !== '') : [];

const stringMap = (value: unknown): Record<string, string> =>
  value && typeof value === 'object' && !Array.isArray(value)
    ? Object.fromEntries(
        Object.entries(value).filter(
          (pair): pair is [string, string] => pair[0] !== '' && typeof pair[1] === 'string' && pair[1] !== '',
        ),
      )
    : {};

/** Accepts whatever the backend sent; anything that is not the expected shape counts as empty. */
export function normalizeMacTaskTiers(raw: unknown): MacTaskTiers {
  const value = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {};
  const tiers: MacTaskTiers = {
    verified: strings(value.verified),
    experimental: strings(value.experimental),
    excluded: stringMap(value.excluded),
    notes: stringMap(value.notes),
  };
  if (typeof value.error === 'string' && value.error) tiers.error = value.error;
  return tiers;
}

/**
 * A macOS backend must return all four fields with the right types and at least one tier entry; anything
 * else (an empty or partial object, wrong types) is a load error rather than "nothing to mark", which would
 * otherwise let tasks start with no markings and no consequence notes. Empty string when the shape is right.
 */
export function macTaskTiersShapeError(raw: unknown): string {
  const value = raw && typeof raw === 'object' && !Array.isArray(raw) ? (raw as Record<string, unknown>) : null;
  const isMap = (item: unknown) => !!item && typeof item === 'object' && !Array.isArray(item);
  if (
    !value ||
    !Array.isArray(value.verified) ||
    !Array.isArray(value.experimental) ||
    !isMap(value.excluded) ||
    !isMap(value.notes)
  ) {
    return MAC_TIERS_MALFORMED;
  }
  return hasMacTaskTiers(normalizeMacTaskTiers(raw)) ? '' : MAC_TIERS_MALFORMED;
}

/** Tiers are present only on a macOS backend with a readable manifest. */
export function hasMacTaskTiers(tiers: MacTaskTiers | null | undefined): tiers is MacTaskTiers {
  return !!tiers && (tiers.verified.length > 0 || tiers.experimental.length > 0 || Object.keys(tiers.excluded).length > 0);
}

/** How a pipeline entry is marked on macOS; null when there are no tiers (Windows) or no entry. */
export function macTaskMark(tiers: MacTaskTiers | null | undefined, entry: string | undefined): MacTaskMark | null {
  if (!hasMacTaskTiers(tiers) || !entry) return null;
  const note = tiers.notes[entry];
  if (tiers.verified.includes(entry)) return { tier: 'verified', note };
  if (tiers.experimental.includes(entry)) return { tier: 'experimental', note };
  // Entries in no list are refused too (client and agent), so they are shown as unavailable.
  return { tier: 'excluded', reason: tiers.excluded[entry] ?? MAC_UNSUPPORTED_REASON };
}

/** The text shown where a task cannot run on macOS. */
export function macExcludedText(mark: MacTaskMark | null): string {
  return mark?.tier === 'excluded' ? `Mac 不可用：${mark.reason}` : '';
}
