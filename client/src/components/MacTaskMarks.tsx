import { FlaskConical, Info } from 'lucide-react';
import { useAppStore } from '@/stores/appStore';
import { MAC_EXPERIMENTAL_BADGE, MAC_EXPERIMENTAL_HINT, macTaskMark, type MacTaskMark } from '@/utils/macTaskTiers';
import { Tooltip } from './ui/Tooltip';

/** The Mac mark for a pipeline entry; always null on Windows (no tiers loaded). */
export function useMacTaskMark(entry: string | undefined): MacTaskMark | null {
  const tiers = useAppStore((state) => state.macTaskTiers);
  return macTaskMark(tiers, entry);
}

/** Hover text for a marked task: the experimental hint and/or the manifest note. */
export function macTaskHint(mark: MacTaskMark | null): string {
  if (mark?.tier === 'experimental') return mark.note ? `${MAC_EXPERIMENTAL_HINT}\n${mark.note}` : MAC_EXPERIMENTAL_HINT;
  if (mark?.tier === 'verified') return mark.note ?? '';
  return '';
}

/** The experimental task's consequence line, visible without opening its options. */
export function macTaskNoteLine(mark: MacTaskMark | null): string {
  return mark?.tier === 'experimental' && mark.note ? mark.note : '';
}

/**
 * Badge for experimental tasks; an info icon for verified tasks with a note. Nothing otherwise.
 * `plain` leaves out the own tooltip, for places that already show macTaskHint in theirs.
 */
export function MacTaskBadge({ mark, plain = false }: { mark: MacTaskMark | null; plain?: boolean }) {
  const hint = macTaskHint(mark);
  let badge;
  if (mark?.tier === 'experimental') {
    badge = (
      <span className="shrink-0 inline-flex items-center gap-0.5 rounded-full border border-warning/30 bg-warning/10 px-1.5 py-0.5 text-[10px] font-medium text-warning">
        <FlaskConical className="h-3 w-3" />
        {MAC_EXPERIMENTAL_BADGE}
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
