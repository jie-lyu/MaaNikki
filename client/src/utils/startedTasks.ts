/**
 * 把后端返回的任务 ID 与提交时的任务对应起来，用于登记任务显示名称。
 *
 * Windows 不会跳过任务，返回的 ID 与提交顺序一一对应。macOS 会跳过测试预览版未开放的任务，此时按
 * 后端记录的「任务 ID → 选中任务 ID」对应；拿不到对应关系时宁可不登记名称，也不按位置猜，
 * 否则被跳过的任务会占用后面任务的名称。
 */
export function pairStartedTasks<T extends { selectedTask: { id: string } }>(
  taskIds: number[],
  submitted: T[],
  options: { byBackendMapping: boolean; mappings?: Record<string, string> },
): Array<[number, T]> {
  if (!options.byBackendMapping) {
    return taskIds.flatMap((id, index) => (submitted[index] ? [[id, submitted[index]] as [number, T]] : []));
  }
  const mappings = options.mappings;
  if (!mappings) return [];
  return taskIds.flatMap((id) => {
    const selectedId = mappings[String(id)];
    const item = submitted.find((candidate) => candidate.selectedTask.id === selectedId);
    return item ? [[id, item] as [number, T]] : [];
  });
}
