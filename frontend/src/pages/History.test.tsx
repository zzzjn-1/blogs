// History（列表页）的渲染层断言。
//
// 列表页的队列/缓存文案和详情页共用同一对纯函数，但**展示条件不同**
// （列表页对「正在跑」也要显示 pos=0，详情页反而不显示）。
// 所以这里单独锁一遍，防止有人「统一」两处逻辑时改坏一处。

import { describe, expect, it, vi } from 'vitest'
import { screen } from '@testing-library/react'
import History from './History'
import { makeTask } from '../test/factories'
import { renderAt } from '../test/renderAt'
import { tasksApi } from '../api/tasks'

vi.mock('../api/tasks', () => ({
  tasksApi: {
    list: vi.fn(),
    get: vi.fn(),
    getScript: vi.fn(),
    create: vi.fn(),
    saveScript: vi.fn(),
    synthesize: vi.fn(),
    retry: vi.fn(),
    cancel: vi.fn(),
    remove: vi.fn(),
    segmentAudio: (id: string, seq: number) => `/api/tasks/${id}/segments/${seq}/audio`,
    audio: (id: string) => `/api/tasks/${id}/audio`,
    download: (id: string) => `/api/tasks/${id}/download`,
  },
}))

const api = vi.mocked(tasksApi)

function renderList(items: ReturnType<typeof makeTask>[]) {
  api.list.mockResolvedValue({ total: items.length, page: 1, page_size: 20, pages: 1, items })
  renderAt('/history', <History />)
}

describe('History 列表摘要', () => {
  it('PENDING + pos=2 → 显示「前面还有 2 个任务」', async () => {
    renderList([makeTask({ id: 'a', topic: 'A 期', status: 'PENDING', queue_position: 2 })])
    expect(await screen.findByText('A 期')).toBeInTheDocument()
    expect(screen.getByText('前面还有 2 个任务')).toBeInTheDocument()
  })

  it('SYNTHESIZING + pos=0 → 列表页仍显示「正在合成」', async () => {
    renderList([makeTask({ id: 'b', topic: 'B 期', status: 'SYNTHESIZING', queue_position: 0 })])
    expect(await screen.findByText('B 期')).toBeInTheDocument()
    expect(screen.getByText('正在合成')).toBeInTheDocument()
  })

  it('DONE 且 seg=0 → 不显示缓存文案（未算 ≠ 0%）', async () => {
    renderList([
      makeTask({ id: 'c', topic: 'C 期', status: 'DONE', cache_hit_rate: null, cache_seg_count: 0 }),
    ])
    expect(await screen.findByText('C 期')).toBeInTheDocument()
    expect(screen.queryByText(/句级缓存命中/)).toBeNull()
  })

  it('DONE + 位次有值（后端残留）→ 不显示排队文案', async () => {
    // 同上：位次有值、状态才是唯一变量，这才真正守住状态白名单
    renderList([makeTask({ id: 'e', topic: 'E 期', status: 'DONE', queue_position: 2 })])
    expect(await screen.findByText('E 期')).toBeInTheDocument()
    expect(screen.queryByText(/前面还有/)).toBeNull()
  })

  it('DONE 且 14/27 → 显示缓存文案', async () => {
    renderList([
      makeTask({
        id: 'd',
        topic: 'D 期',
        status: 'DONE',
        cache_hit_count: 14,
        cache_seg_count: 27,
        cache_hit_rate: 14 / 27,
      }),
    ])
    expect(await screen.findByText('D 期')).toBeInTheDocument()
    expect(screen.getByText(/句级缓存命中 14\/27（51.9%）/)).toBeInTheDocument()
  })
})
