import { normalizeAgentConfigs } from '@/types/interface';
import { loggers } from '@/utils/logger';
import { isTauri } from '@/utils/paths';
import { useAppStore } from '@/stores/appStore';
import { getCurrentControllerAndResource } from '@/stores/helpers';

import { maaService } from './maaService';

const log = loggers.task;

const STOP_TIMEOUT_MS = 8000;
const STOP_REPOST_INTERVAL_MS = 800;
const STOP_POLL_INTERVAL_MS = 100;

const stopPromises = new Map<string, Promise<boolean>>();

/**
 * How many stops were requested per instance. An Android start compares it before submitting and after the backend
 * answered: a Stop pressed while the start was still being prepared then wins over that start (the backend's run
 * slot only exists once the start request arrives). Only instances whose selected controller is Adb read or write
 * it: Win32 and native Mac starts and stops leave it untouched.
 */
const stopGenerations = new Map<string, number>();

export function stopGeneration(instanceId: string): number {
  return stopGenerations.get(instanceId) ?? 0;
}

async function waitForTaskStop(instanceId: string) {
  const start = Date.now();
  let lastPost = start;

  while (Date.now() - start < STOP_TIMEOUT_MS) {
    const running = await maaService.isRunning(instanceId);
    if (!running) {
      return true;
    }

    if (Date.now() - lastPost >= STOP_REPOST_INTERVAL_MS) {
      try {
        await maaService.stopTask(instanceId);
        lastPost = Date.now();
      } catch (error) {
        log.warn(`[task-stop#${instanceId}] 重发停止请求失败:`, error);
      }
    }

    await new Promise((resolve) => setTimeout(resolve, STOP_POLL_INTERVAL_MS));
  }

  return false;
}

function cleanupTaskState(instanceId: string) {
  const state = useAppStore.getState();
  state.updateInstance(instanceId, { isRunning: false });
  state.setInstanceTaskStatus(instanceId, null);
  state.setInstanceCurrentTaskId(instanceId, null);
  state.clearScheduleExecution(instanceId);
}

/** The instance's selected controller is Adb (the same choice the start makes from the store). */
function selectsAdb(instanceId: string): boolean {
  const state = useAppStore.getState();
  const { controllerName } = getCurrentControllerAndResource(state, instanceId);
  return state.projectInterface?.controller.find((c) => c.name === controllerName)?.type === 'Adb';
}

export interface StopOptions {
  /**
   * Android emulator only (ignored for other controllers): send a new backend stop even while an earlier stop of this
   * instance is still pending. The toolbar's compensating stop after a start response uses it: the earlier stop may
   * have reached the backend while its run slot was still idle (so it stopped nothing) and its reply may still be on
   * the way; joining it would never reach the run the start has just admitted.
   */
  fresh?: boolean;
}

export async function stopInstanceTasks(instanceId: string, options: StopOptions = {}): Promise<boolean> {
  const adb = selectsAdb(instanceId);
  if (adb) {
    stopGenerations.set(instanceId, stopGeneration(instanceId) + 1);
  }
  const existing = stopPromises.get(instanceId);
  if (existing && adb && options.fresh) {
    return stopAgainWhilePending(instanceId, existing);
  }
  if (existing) {
    return existing;
  }
  return startStop(instanceId);
}

/**
 * The fresh stop of an Adb instance while an earlier stop is pending: the backend stop is sent now; once the earlier
 * stop has settled, the usual stop (wait until the run ended, stop the agent, clear the state) follows, joining one
 * that has begun meanwhile.
 */
async function stopAgainWhilePending(instanceId: string, earlier: Promise<boolean>): Promise<boolean> {
  log.info(`[task-stop#${instanceId}] 再次发送停止（之前的停止还没返回）`);
  try {
    await maaService.stopTask(instanceId);
  } catch (error) {
    log.warn(`[task-stop#${instanceId}] 再次发送停止失败:`, error);
  }
  await earlier.catch(() => false);
  return stopPromises.get(instanceId) ?? startStop(instanceId);
}

function startStop(instanceId: string): Promise<boolean> {
  const stopPromise = (async () => {
    log.info(`[task-stop#${instanceId}] 停止任务`);

    try {
      await maaService.stopTask(instanceId);
    } catch (error) {
      const stillRunning = await maaService.isRunning(instanceId).catch(() => false);
      if (stillRunning) {
        throw error;
      }
      log.info(`[task-stop#${instanceId}] Tasker 已停止，跳过首次 stop 错误`);
    }

    const stopped = await waitForTaskStop(instanceId);
    if (!stopped) {
      log.warn(`[task-stop#${instanceId}] 等待任务停止超时，保留运行状态`);
      return false;
    }

    const agentConfigs = normalizeAgentConfigs(useAppStore.getState().projectInterface?.agent);
    if (agentConfigs && agentConfigs.length > 0) {
      await maaService.stopAgent(instanceId);
    }

    cleanupTaskState(instanceId);
    return true;
  })().finally(() => {
    stopPromises.delete(instanceId);
  });

  stopPromises.set(instanceId, stopPromise);
  return stopPromise;
}

export async function stopInstanceTasksAndExitApp(instanceId: string): Promise<boolean> {
  const stopped = await stopInstanceTasks(instanceId);
  if (!stopped) {
    return false;
  }

  if (!isTauri()) {
    return true;
  }

  const { exit } = await import('@tauri-apps/plugin-process');
  await exit(0);
  return true;
}
