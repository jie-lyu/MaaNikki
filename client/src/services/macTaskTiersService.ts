import { invoke } from '@tauri-apps/api/core';
import { apiGet } from '@/utils/backendApi';
import { isTauri } from '@/utils/paths';
import { loggers } from '@/utils/logger';
import { useAppStore } from '@/stores/appStore';
import { getCurrentControllerAndResource } from '@/stores/helpers';
import {
  MAC_TIERS_UNREADABLE,
  macTaskTiersSettled,
  macTaskTiersShapeError,
  normalizeMacTaskTiers,
  type MacTaskTiers,
} from '@/utils/macTaskTiers';
import { ANDROID_TIERS_MALFORMED, ANDROID_TIERS_UNREADABLE, usesAndroidTiers, whenSettled } from '@/utils/taskTiers';

/** Reads the Mac task tiers from the backend (desktop command or WebUI route); never throws. */
export async function loadMacTaskTiers(): Promise<MacTaskTiers> {
  try {
    const raw = isTauri()
      ? await invoke<unknown>('get_macos_task_tiers')
      : await apiGet<unknown>('/macos/task-tiers');
    const tiers = normalizeMacTaskTiers(raw);
    // Only called for a macOS backend: an empty or malformed answer must refuse starts, not mark nothing.
    const shapeError = tiers.error ? '' : macTaskTiersShapeError(raw);
    return shapeError ? { ...tiers, error: shapeError } : tiers;
  } catch (error) {
    loggers.app.warn('读取 Mac 任务清单失败:', error);
    return {
      ...normalizeMacTaskTiers(null),
      error: `${MAC_TIERS_UNREADABLE}（${error instanceof Error ? error.message : String(error)}）`,
    };
  }
}

/**
 * Reads the Android emulator task tiers (same shape as the Mac ones) from the backend; never throws. Only
 * called for a macOS backend, so an empty or malformed answer refuses the Adb instances' starts.
 */
export async function loadAndroidTaskTiers(): Promise<MacTaskTiers> {
  try {
    const raw = isTauri()
      ? await invoke<unknown>('get_android_task_tiers')
      : await apiGet<unknown>('/android/task-tiers');
    const tiers = normalizeMacTaskTiers(raw);
    const shapeError = tiers.error ? '' : macTaskTiersShapeError(raw) ? ANDROID_TIERS_MALFORMED : '';
    return shapeError ? { ...tiers, error: shapeError } : tiers;
  } catch (error) {
    loggers.app.warn('读取安卓任务清单失败:', error);
    return {
      ...normalizeMacTaskTiers(null),
      error: `${ANDROID_TIERS_UNREADABLE}（${error instanceof Error ? error.message : String(error)}）`,
    };
  }
}

type TierState = ReturnType<typeof useAppStore.getState>;

/** The type of the controller an instance has selected (same fallback order as the rest of the store). */
export function instanceControllerType(state: TierState, instanceId: string): string | undefined {
  const { controllerName } = getCurrentControllerAndResource(state, instanceId);
  return state.projectInterface?.controller.find((c) => c.name === controllerName)?.type;
}

/** Some instance has an Adb controller selected on a macOS backend, so its runs need the Android tiers. */
function androidTiersNeeded(state: TierState): boolean {
  return state.instances.some((inst) => usesAndroidTiers(state.backendOS, instanceControllerType(state, inst.id)));
}

/**
 * True only on a macOS backend whose Mac task tiers have not arrived yet, or whose Android tiers have not
 * arrived while an instance has an Adb controller selected. Without an Adb instance this is the Mac check
 * alone, as before; off macOS it is always false.
 */
export function macTaskTiersPending(): boolean {
  const state = useAppStore.getState();
  if (state.backendOS !== 'macos') return false;
  return state.macTaskTiers === null || (state.androidTaskTiers === null && androidTiersNeeded(state));
}

/**
 * What the scheduler waits on before deciding a slot: macTaskTiersSettled over the app store, and, while an
 * instance has an Adb controller selected on macOS, the Android tiers too (both are read in parallel at
 * startup, so this adds at most the difference between the two reads; the same timeout applies).
 */
export function waitForMacTaskTiers(timeoutMs = 30000): Promise<void> {
  if (!androidTiersNeeded(useAppStore.getState())) {
    return macTaskTiersSettled(useAppStore.getState, useAppStore.subscribe, timeoutMs);
  }
  return whenSettled(() => !macTaskTiersPending(), useAppStore.subscribe, timeoutMs);
}
