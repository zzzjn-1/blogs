// `taskView.ts` 的三条语义断言（原 scripts/verify_taskview.mjs 迁移而来）。
//
// 这三条都是「写错了界面也不报错」的类型：只显示得有点怪，控制台干干净净。
// 所以它们必须由断言锁住，而不是靠肉眼 review。
//
//   1. queue_position === 0 表示**正在跑**，不是「前面还有 0 个」；
//   2. cache_hit_rate === null 表示**还没算**，不是「命中 0%」；
//   3. seg === 0 时即使 rate 有值也不展示（读数不自洽）。
//
// 运行：npm run test

import { describe, expect, it } from 'vitest'
import { cacheLabel, queueLabel, showCache, showQueue } from './taskView'

describe('queueLabel —— 0 是「正在跑」，不是「前面还有 0 个」', () => {
  it('pos=0 → 正在合成', () => {
    expect(queueLabel({ status: 'SYNTHESIZING', queue_position: 0 })).toBe('正在合成')
  })

  it('pos=1 → 前面还有 1 个任务', () => {
    expect(queueLabel({ status: 'PENDING', queue_position: 1 })).toBe('前面还有 1 个任务')
  })

  it('pos=3 → 前面还有 3 个任务', () => {
    expect(queueLabel({ status: 'PENDING', queue_position: 3 })).toBe('前面还有 3 个任务')
  })

  it('null + 等待确认（SCRIPT_READY）→ 不显示', () => {
    expect(queueLabel({ status: 'SCRIPT_READY', queue_position: null })).toBeNull()
  })

  it('null + 终态 → 不显示', () => {
    expect(queueLabel({ status: 'DONE', queue_position: null })).toBeNull()
  })

  it('null + PENDING → 排队中（唯一例外）', () => {
    expect(queueLabel({ status: 'PENDING', queue_position: null })).toBe('已提交，排队中…')
  })

  it('字段缺失（旧后端）→ 不炸', () => {
    expect(queueLabel({ status: 'PENDING' })).toBe('已提交，排队中…')
  })
})

describe('cacheLabel —— rate=null 是「还没算」，不是「命中 0%」', () => {
  it('rate=null → 不显示', () => {
    expect(
      cacheLabel({ cache_hit_rate: null, cache_seg_count: 0, cache_hit_count: 0 }),
    ).toBeNull()
  })

  it('rate=0 且 seg>0 → 显示（与 null 严格区分）', () => {
    expect(
      cacheLabel({ cache_hit_rate: 0, cache_seg_count: 4, cache_hit_count: 0 }),
    ).toBe('句级缓存命中 0/4（0.0%）')
  })

  it('D12 实测值 14/27 → 与实施报告口径一致', () => {
    expect(
      cacheLabel({ cache_hit_rate: 14 / 27, cache_seg_count: 27, cache_hit_count: 14 }),
    ).toBe('句级缓存命中 14/27（51.9%）')
  })

  it('全命中 12/12 → 100.0%', () => {
    expect(
      cacheLabel({ cache_hit_rate: 1, cache_seg_count: 12, cache_hit_count: 12 }),
    ).toBe('句级缓存命中 12/12（100.0%）')
  })

  it('seg=0 兜底：rate 有值也不显示', () => {
    expect(
      cacheLabel({ cache_hit_rate: 0.5, cache_seg_count: 0, cache_hit_count: 0 }),
    ).toBeNull()
  })
})

describe('showQueue / showCache —— 不该出现的地方不出现', () => {
  it('终态不显示队列', () => {
    expect(showQueue({ status: 'DONE', queue_position: null })).toBe(false)
  })

  // 下面三条是变异测试 F6 逼出来的：只写「终态 + null」是不够的 ——
  // 那种输入下 queueLabel 本来就是 null，状态白名单**根本没参与判定**，
  // 于是把白名单删掉测试照样全绿（检查等于不存在）。
  // 必须让「状态」成为唯一变量：位次有值、但该状态不该显示。
  it('终态 + 位次有值（后端残留）→ 仍不显示', () => {
    expect(showQueue({ status: 'DONE', queue_position: 2 })).toBe(false)
  })

  it('SCRIPT_READY（等用户确认，不在队列）+ 位次有值 → 不显示', () => {
    expect(showQueue({ status: 'SCRIPT_READY', queue_position: 1 })).toBe(false)
  })

  it('FAILED + 位次 0 → 不显示', () => {
    expect(showQueue({ status: 'FAILED', queue_position: 0 })).toBe(false)
  })

  it('排队中显示队列', () => {
    expect(showQueue({ status: 'PENDING', queue_position: 2 })).toBe(true)
  })

  it('列表页对正在跑也显示', () => {
    expect(showQueue({ status: 'SYNTHESIZING', queue_position: 0 })).toBe(true)
  })

  it('脚本阶段不显示缓存', () => {
    expect(
      showCache({
        status: 'SCRIPT_READY',
        cache_hit_rate: 0.5,
        cache_seg_count: 4,
        cache_hit_count: 2,
      }),
    ).toBe(false)
  })

  it('成片显示缓存', () => {
    expect(
      showCache({ status: 'DONE', cache_hit_rate: 0.5, cache_seg_count: 4, cache_hit_count: 2 }),
    ).toBe(true)
  })

  it('未开始算不显示缓存', () => {
    expect(showCache({ status: 'SYNTHESIZING', cache_hit_rate: null, cache_seg_count: 0 })).toBe(
      false,
    )
  })
})
