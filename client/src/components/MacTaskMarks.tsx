import { FlaskConical, Info } from 'lucide-react';
import { useAppStore } from '@/stores/appStore';
import { instanceControllerType } from '@/services/macTaskTiersService';
import type { MacTaskTiers } from '@/utils/macTaskTiers';
import {
  experimentalBadgeText,
  experimentalHintText,
  taskMarkFor,
  tierPlatformFor,
  tiersForController,
  type TaskMark,
  type TierPlatform,
} from '@/utils/taskTiers';
import { Tooltip } from './ui/Tooltip';

/**
 * The tier list and wording platform for an instance's selected controller: Android for Adb on macOS,
 * otherwise the Mac tiers (null on Windows and Linux, so nothing is marked there).
 */
export function useInstanceTaskTiers(instanceId: string | undefined): { platform: TierPlatform; tiers: MacTaskTiers | null } {
  const backendOS = useAppStore((state) => state.backendOS);
  const controllerType = useAppStore((state) => (instanceId ? instanceControllerType(state, instanceId) : undefined));
  const macTiers = useAppStore((state) => state.macTaskTiers);
  const androidTiers = useAppStore((state) => state.androidTaskTiers);
  return {
    platform: tierPlatformFor(backendOS, controllerType),
    tiers: tiersForController(backendOS, controllerType, macTiers, androidTiers),
  };
}

/** The mark for a pipeline entry on an instance; always null on Windows (no tiers loaded). */
export function useInstanceTaskMark(instanceId: string | undefined, entry: string | undefined): TaskMark | null {
  const { platform, tiers } = useInstanceTaskTiers(instanceId);
  return taskMarkFor(platform, tiers, entry);
}

/** Hover text for a marked task: the experimental hint and/or the manifest note. */
export function macTaskHint(mark: TaskMark | null): string {
  if (mark?.tier === 'experimental') {
    const hint = experimentalHintText(mark);
    return mark.note ? `${hint}\n${mark.note}` : hint;
  }
  if (mark?.tier === 'verified') return mark.note ?? '';
  return '';
}

/** The experimental task's consequence line, visible without opening its options. */
export function macTaskNoteLine(mark: TaskMark | null): string {
  return mark?.tier === 'experimental' && mark.note ? mark.note : '';
}

/**
 * Badge for experimental tasks; an info icon for verified tasks with a note. Nothing otherwise.
 * `plain` leaves out the own tooltip, for places that already show macTaskHint in theirs.
 */
export function MacTaskBadge({ mark, plain = false }: { mark: TaskMark | null; plain?: boolean }) {
  const hint = macTaskHint(mark);
  let badge;
  if (mark?.tier === 'experimental') {
    badge = (
      <span className="shrink-0 inline-flex items-center gap-0.5 rounded-full border border-warning/30 bg-warning/10 px-1.5 py-0.5 text-[10px] font-medium text-warning">
        <FlaskConical className="h-3 w-3" />
        {experimentalBadgeText(mark)}
      </span>
    );
  } else if (mark?.tier === 'verified' && hint) {
    badge = (
      <span className="shrink-0 inline-flex text-text-muted" aria-label={hint}>
        <Info className="h-3.5 w-3.5" />
      </span>
    );
  } else {
    return null;
  }
  if (plain) return badge;
  return (
    <Tooltip content={<span className="whitespace-pre-line">{hint}</span>} maxWidth="max-w-xs">
      {badge}
    </Tooltip>
  );
}
