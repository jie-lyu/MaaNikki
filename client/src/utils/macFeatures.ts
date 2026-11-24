import { useAppStore } from '@/stores/appStore';

/**
 * Built-in features that connect to the game window through selectGameController (utils/gameTarget.ts) only
 * find a Win32 controller, so on a macOS backend they cannot start. Their entry controls are disabled with this
 * reason instead of failing after a click. The custom-route task (MaaNikki_RouteRun) is a normal task and is
 * not affected.
 */
export const MAC_GAME_TARGET_UNSUPPORTED = 'Mac 版暂不支持（需要先选到 Mac 的游戏窗口）';

/** The reason game-window features are unavailable on this backend OS, or '' (Windows: available). */
export function gameTargetUnsupportedReason(backendOS: string): string {
  return backendOS === 'macos' ? MAC_GAME_TARGET_UNSUPPORTED : '';
}

/** Current reason from the store, without subscribing (for services). */
export function currentGameTargetUnsupportedReason(): string {
  return gameTargetUnsupportedReason(useAppStore.getState().backendOS);
}

/** Current reason, re-rendering when the backend OS becomes known (for components). */
export function useGameTargetUnsupportedReason(): string {
  return gameTargetUnsupportedReason(useAppStore((state) => state.backendOS));
}
