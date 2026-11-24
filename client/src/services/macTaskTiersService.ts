import { invoke } from '@tauri-apps/api/core';
import { apiGet } from '@/utils/backendApi';
import { isTauri } from '@/utils/paths';
import { loggers } from '@/utils/logger';
import { MAC_TIERS_UNREADABLE, normalizeMacTaskTiers, type MacTaskTiers } from '@/utils/macTaskTiers';

/** Reads the Mac task tiers from the backend (desktop command or WebUI route); never throws. */
export async function loadMacTaskTiers(): Promise<MacTaskTiers> {
  try {
    const raw = isTauri()
      ? await invoke<unknown>('get_macos_task_tiers')
      : await apiGet<unknown>('/macos/task-tiers');
    return normalizeMacTaskTiers(raw);
  } catch (error) {
    loggers.app.warn('读取 Mac 任务清单失败:', error);
    return {
      ...normalizeMacTaskTiers(null),
      error: `${MAC_TIERS_UNREADABLE}（${error instanceof Error ? error.message : String(error)}）`,
    };
  }
}
