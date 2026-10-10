// TaskDetail 的**渲染层**断言。
//
// 为什么纯函数测过还要测渲染：`taskView.ts` 只保证「文案算得对」，
// 但「算对了却没被渲染出来」（少了 `waiting` 判断、条件写反、放错 Card）
// 纯函数测试**一律发现不了**。这两层要各测各的。

import { describe, expect, it, vi } from 'vitest'
import { screen } from '@testing-library/react'
import { Route, Routes } from 'react-router-dom'
import TaskDetail from './TaskDetail'
import { makeTask } from '../test/factories'
import { renderAt } from '../test/renderAt'
import { tasksApi } from '../api/tasks'

vi.mock('../api/tasks', () => ({
  tasksApi: {
    get: vi.fn(),
    getScript: vi.fn(),
    list: vi.fn(),
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

/** 渲染详情页并等待首次轮询落地（stage 是每例都有的稳定锚点）。 */
async function renderDetail(task: ReturnType<typeof makeTask>) {
  api.get.mockResolvedValue(task)
  // SCRIPT_READY 会拉脚本；给个空脚本兜底，免得别处用例走到这个分支时炸在 undefined.then
  api.getScript.mockResolvedValue({
    task_id: task.id,
    title: '',
    summary: '',
    status: task.status,
    line_count: 0,
    lines: [],
  })
  renderAt(
    `/tasks/${task.id}`,
    <Routes>
      <Route path="/tasks/:id" element={<TaskDetail />} />
    </Routes>,
  )
}

describe('TaskDetail 队列提示', () => {
  it('排队中（pos=2）→ 显示「前面还有 2 个任务」', async () => {
    await renderDetail(makeTask({ status: 'PENDING', queue_position: 2, stage: 'QUEUED' }))
    expect(await screen.findByText('前面还有 2 个任务')).toBeInTheDocument()
  })

  it('pos=null 且 PENDING → 显示「已提交，排队中…」', async () => {
    await renderDetail(makeTask({ status: 'PENDING', queue_position: null, stage: 'QUEUED' }))
    expect(await screen.findByText('已提交，排队中…')).toBeInTheDocument()
  })

  it('pos=0（自己正在跑）→ 不弹队列提示，避免与阶段/进度条重复', async () => {
    await renderDetail(makeTask({ status: 'SYNTHESIZING', queue_position: 0, stage: 'SYNTH' }))
    // 等进度条渲染出来，确认页面已稳定，再断言「没有」才有意义
    expect(await screen.findByText('SYNTH')).toBeInTheDocument()
    expect(screen.queryByText('正在合成')).toBeNull()
  })

  it('终态 + 位次有值（后端残留）→ 不弹队列提示', async () => {
    // 守的是「状态白名单」：位次本身有值，唯一不显示的理由是 DONE 已不在队列。
    // 详情页此前自己写了一套判断（漏了白名单），与列表页不一致 —— 现两处共用 showQueue。
    await renderDetail(makeTask({ status: 'DONE', queue_position: 2, stage: 'PACKAGING' }))
    expect(await screen.findByText('PACKAGING')).toBeInTheDocument()
    expect(screen.queryByText('前面还有 2 个任务')).toBeNull()
  })

  it('SCRIPT_READY（等用户确认）+ 位次有值 → 不弹队列提示', async () => {
    await renderDetail(makeTask({ status: 'SCRIPT_READY', queue_position: 1, stage: 'SCRIPT' }))
    expect(await screen.findByText('SCRIPT')).toBeInTheDocument()
    expect(screen.queryByText('前面还有 1 个任务')).toBeNull()
  })
})

describe('TaskDetail 句级缓存读数', () => {
  it('成片且 14/27 → 详情里出现「句级缓存」项', async () => {
    await renderDetail(
      makeTask({
        status: 'DONE',
        stage: 'PACKAGING',
        cache_hit_count: 14,
        cache_seg_count: 27,
        cache_hit_rate: 14 / 27,
      }),
    )
    expect(await screen.findByText('句级缓存')).toBeInTheDocument()
    expect(screen.getByText('句级缓存命中 14/27（51.9%）')).toBeInTheDocument()
  })

  it('合成中但 seg=0（还没算）→ 不出现「句级缓存」项', async () => {
    await renderDetail(
      makeTask({ status: 'SYNTHESIZING', stage: 'SYNTH', cache_hit_rate: null, cache_seg_count: 0 }),
    )
    expect(await screen.findByText('SYNTH')).toBeInTheDocument()
    expect(screen.queryByText('句级缓存')).toBeNull()
  })

  it('SCRIPT_READY 且读数有效 → 仍不出现（缓存读数只在合成及之后才有意义）', async () => {
    // 这一例专门守 showCache 的**阶段白名单**：读数本身是合法的（0.5 / 4），
    // 唯一不显示的理由就是「脚本阶段还没开始合成」。少了白名单判断就会漏出来。
    await renderDetail(
      makeTask({
        status: 'SCRIPT_READY',
        stage: 'SCRIPT',
        cache_hit_count: 2,
        cache_seg_count: 4,
        cache_hit_rate: 0.5,
      }),
    )
    expect(await screen.findByText('SCRIPT')).toBeInTheDocument()
    expect(screen.queryByText('句级缓存')).toBeNull()
  })
})
