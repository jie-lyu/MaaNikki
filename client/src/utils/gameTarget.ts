import type { ControllerItem, ProjectInterface } from '@/types/interface';
import type { ControllerConfig, Win32Window } from '@/types/maa';
import { buildDesktopWindowControllerConfig, getDesktopWindowFilters } from '@/utils/controller';

/**
 * 内置功能（探索、钓鱼、按键读取、路线编辑、衣柜）连接游戏窗口时使用的统一目标选择器。
 *
 * 控制器选择、窗口查找和控制器配置都集中在这里，平台差异只需在本模块内按控制器类型分支。
 * 当前只支持 Win32，行为与原先各服务内联的写法一致。
 */

/** 找到的游戏窗口；服务使用其中的 handle、window_name、class_name。 */
export type GameWindow = Win32Window;

/** 窗口查找所需的后端能力（通常传入 maaService）。 */
export interface GameWindowFinder {
  findWin32Windows(classRegex?: string, windowRegex?: string, quiet?: boolean): Promise<Win32Window[]>;
}

/** 从已加载的项目接口中选出游戏控制器：第一个 Win32 控制器。 */
export function selectGameController(
  projectInterface: ProjectInterface | null | undefined,
): ControllerItem | undefined {
  return projectInterface?.controller.find((item) => item.type === 'Win32');
}

/** 按控制器的窗口筛选条件查找游戏窗口，quiet 原样传给后端查找。 */
export function findGameWindows(
  finder: GameWindowFinder,
  controller: ControllerItem | undefined,
  quiet?: boolean,
): Promise<GameWindow[]> {
  const { classRegex, titleRegex } = getDesktopWindowFilters(controller);
  return finder.findWin32Windows(classRegex, titleRegex, quiet);
}

/** 为找到的游戏窗口构建运行时控制器配置；控制器无效时返回 null。 */
export function buildGameControllerConfig(
  controller: ControllerItem | undefined,
  window: GameWindow,
): ControllerConfig | null {
  return buildDesktopWindowControllerConfig(controller, window.handle);
}
