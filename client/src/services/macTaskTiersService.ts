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

/** True only on a macOS backend whose task tiers have not arrived yet. */
export function macTaskTiersPending(): boolean {
  const state = useAppStore.getState();
  return state.backendOS === 'macos' && state.macTaskTiers === null;
}

/** macTaskTiersSettled over the app store: what the scheduler waits on before deciding a slot. */
export function waitForMacTaskTiers(timeoutMs = 30000): Promise<void> {
  return macTaskTiersSettled(useAppStore.getState, useAppStore.subscribe, timeoutMs);
}

/**
 * True when this instance's selected controller is Adb on a macOS backend and the Android tiers have not arrived
 * yet. The controller type is looked at first: for any other instance the Android tiers are never read, so native
 * Mac (and Windows) scheduling is exactly the Mac-only check above.
 */
export function androidTaskTiersPendingFor(instanceId: string): boolean {
  const state = useAppStore.getState();
  if (!usesAndroidTiers(state.backendOS, instanceControllerType(state, instanceId))) return false;
  return state.androidTaskTiers === null;
}

/** What the scheduler waits on before starting a due Adb instance: the Android tiers settled, or the timeout. */
export function waitForAndroidTaskTiers(timeoutMs = 30000): Promise<void> {
  return whenSettled(() => useAppStore.getState().androidTaskTiers !== null, useAppStore.subscribe, timeoutMs);
}
