export type TaskStatus =
  | 'PENDING'
  | 'SCRIPTING'
  | 'SCRIPT_READY'
  | 'SYNTHESIZING'
  | 'POSTPROCESSING'
  | 'PACKAGING'
  | 'DONE'
  | 'FAILED'
  | 'CANCELED'

export interface UserOut {
  id: number
  username: string
  created_at?: string | null
}

export interface TokenOut {
  access_token: string
  token_type: string
  expires_in: number
  user: UserOut
}

export interface TaskOut {
  id: string
  topic: string
  status: TaskStatus
  progress: number
  stage: string
  error_msg: string
  target_duration_sec: number
  target_word_count: number
  style: string
  voice_a: string
  voice_b: string
  speed: number
  tone: string
  content_flagged: boolean
  script_title: string
  script_summary: string
  line_count: number
  created_at?: string | null
  updated_at?: string | null
  finished_at?: string | null
  // --- D12 可观测性（后端 schemas.TaskOut）---
  /** 队列位置：0 = 正在合成，1 = 下一个，2 = 再下一个；不在队列（终态/等待确认）为 null。 */
  queue_position: number | null
  /** 合成阶段最近一次句级缓存命中数（续跑会整体重算，非累加）。 */
  cache_hit_count: number
  /** 已处理的句数（命中 + 未命中）。为 0 时命中率无意义，后端给 null。 */
  cache_seg_count: number
  /** 句级缓存命中率（0~1）；seg=0 时为 null。 */
  cache_hit_rate: number | null
}

export interface TaskPageOut {
  total: number
  page: number
  page_size: number
  pages: number
  items: TaskOut[]
}

export interface ScriptLineOut {
  seq: number
  speaker: string
  text: string
  read_text: string
  text_hash: string | null
  duration_ms: number
  seg_status: string
}

export interface ScriptOut {
  task_id: string
  title: string
  summary: string
  status: string
  line_count: number
  lines: ScriptLineOut[]
}

export interface FeedOut {
  title: string
  description: string
  cover_url: string
  category: string
  explicit: boolean
  user_token: string
  feed_url: string | null
  updated_at: string | null
}

export interface FeedUpdateIn {
  title?: string
  description?: string
  cover_url?: string
  category?: string
  explicit?: boolean
  reset_token?: boolean
}

export interface TaskCreateIn {
  topic: string
  target_duration_sec?: number
  duration_min?: number
  style?: string
  voice_a?: string
  voice_b?: string
  speed?: number
  tone?: string
}

export interface ScriptLineIn {
  speaker: 'A' | 'B'
  text: string
}

export interface ScriptSaveIn {
  title?: string
  summary?: string
  lines: ScriptLineIn[]
}
