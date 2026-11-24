import { invoke } from '@tauri-apps/api/core';
import { apiGet } from '@/utils/backendApi';
import { isTauri } from '@/utils/paths';
import { loggers } from '@/utils/logger';
import { useAppStore } from '@/stores/appStore';
import {
  MAC_TIERS_UNREADABLE,
  macTaskTiersSettled,
  macTaskTiersShapeError,
  normalizeMacTaskTiers,
  type MacTaskTiers,
} from '@/utils/macTaskTiers';

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

/** macTaskTiersSettled over the app store: what the scheduler waits on before deciding a slot. */
export function waitForMacTaskTiers(timeoutMs = 30000): Promise<void> {
  return macTaskTiersSettled(useAppStore.getState, useAppStore.subscribe, timeoutMs);
}
