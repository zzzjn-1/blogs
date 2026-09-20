import type { TaskOut, TaskStatus } from '../api/types'

/**
 * `TaskOut` 测试构造器。
 *
 * 目的：让每个用例只声明**它真正关心的字段**，其余取一份自洽的默认值。
 * 直接手写字面量的话，后端每加一个字段就要改几十处测试——那会让人不愿意加测试。
 */
export function makeTask(patch: Partial<TaskOut> = {}): TaskOut {
  const base: TaskOut = {
    id: 't1',
    topic: '测试主题',
    status: 'PENDING' as TaskStatus,
    progress: 0,
    stage: '',
    error_msg: '',
    target_duration_sec: 600,
    target_word_count: 1200,
    style: '对话',
    voice_a: 'voice_a',
    voice_b: 'voice_b',
    speed: 1,
    tone: '',
    content_flagged: false,
    script_title: '',
    script_summary: '',
    line_count: 12,
    queue_position: null,
    cache_hit_count: 0,
    cache_seg_count: 0,
    cache_hit_rate: null,
  }
  return { ...base, ...patch }
}
