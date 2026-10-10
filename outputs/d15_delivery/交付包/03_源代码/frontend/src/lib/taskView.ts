// D12 可观测性字段的**纯展示逻辑**。
//
// 单独抽出来（不依赖 React）的原因：这三条语义都很容易写错，而写错之后
// 界面上只会「显示得有点怪」，不会有任何报错——
//   1. `queue_position === 0` 表示**正在合成**，不是「前面还有 0 个」；
//   2. `queue_position === null` 表示**不在队列**（等待确认 / 已终态），
//      此时显示「排队第 0 位」是错的，必须不显示；
//   3. `cache_hit_rate === null` 表示 `cache_seg_count === 0`（无可展示读数），
//      不是「命中率 0%」，两者在界面上含义完全不同。
// 抽成纯函数后可以用表驱动断言直接锁住，不必起浏览器。

type QueueView = {
  status: string
  queue_position?: number | null
}

type CacheView = {
  cache_hit_rate?: number | null
  cache_hit_count?: number
  cache_seg_count?: number
}

/** 排队状态值得展示的时机（终态与等待确认时，队列位置恒为 null）。 */
const QUEUE_VISIBLE = new Set(['PENDING', 'SCRIPTING', 'SYNTHESIZING', 'POSTPROCESSING', 'PACKAGING'])

/** 缓存读数值得展示的时机：合成及其之后的阶段。 */
const CACHE_VISIBLE = new Set(['SYNTHESIZING', 'POSTPROCESSING', 'PACKAGING', 'DONE'])

/**
 * 队列位置的展示文案。
 *
 * - `0` → 「正在合成」（后端口径：0 = 正在跑）
 * - `>0` → 「前面还有 N 个任务」
 * - `null` → 不在队列；仅当任务还是 `PENDING`（刚提交、尚未被调度器认领）时
 *   退化为「已提交，排队中…」，其余状态返回 `null`（不显示）。
 */
export function queueLabel(t: QueueView): string | null {
  const pos = t.queue_position
  if (pos === null || pos === undefined) {
    // 后端约定 null = 不在队列。PENDING 是唯一的例外：它必然「在等」，
    // 只是「还没进队列」与「队列已丢」在响应里无法区分，此时说「排队中」不会错。
    return t.status === 'PENDING' ? '已提交，排队中…' : null
  }
  if (!Number.isFinite(pos)) return null
  if (pos <= 0) return '正在合成'
  return `前面还有 ${pos} 个任务`
}

/**
 * 句级缓存命中率的展示文案，形如 `句级缓存命中 14/27（51.9%）`。
 *
 * `cache_hit_rate === null`（即 `cache_seg_count === 0`）或读数不自洽时返回 `null`，
 * **不要**退化成「命中 0%」——那会让「还没开始算」看起来像「一个都没命中」。
 */
export function cacheLabel(t: CacheView): string | null {
  const rate = t.cache_hit_rate
  if (rate === null || rate === undefined || !Number.isFinite(rate)) return null
  const seg = t.cache_seg_count ?? 0
  if (seg <= 0) return null
  const hit = t.cache_hit_count ?? 0
  const pct = (rate * 100).toFixed(1)
  return `句级缓存命中 ${hit}/${seg}（${pct}%）`
}

/** 该状态下是否值得展示排队提示。 */
export function showQueue(t: QueueView): boolean {
  return QUEUE_VISIBLE.has(t.status) && queueLabel(t) !== null
}

/** 该状态下是否值得展示缓存读数。 */
export function showCache(t: CacheView & { status: string }): boolean {
  return CACHE_VISIBLE.has(t.status) && cacheLabel(t) !== null
}
