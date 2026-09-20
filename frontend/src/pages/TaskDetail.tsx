import { useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  Alert,
  App,
  Button,
  Card,
  Descriptions,
  Input,
  Popconfirm,
  Progress,
  Select,
  Space,
  Spin,
  Tag,
  Typography,
} from 'antd'
import { tasksApi } from '../api/tasks'
import type { ScriptLineIn, ScriptOut, TaskOut } from '../api/types'
import { cacheLabel, queueLabel, showCache, showQueue } from '../lib/taskView'

const TERMINAL = new Set(['DONE', 'FAILED', 'CANCELED'])
const EDITABLE = 'SCRIPT_READY'

function statusColor(s: string): string {
  if (s === 'DONE') return 'green'
  if (s === 'FAILED' || s === 'CANCELED') return 'red'
  return 'processing'
}

export default function TaskDetail() {
  const { id = '' } = useParams()
  const { message } = App.useApp()
  const navigate = useNavigate()

  const [task, setTask] = useState<TaskOut | null>(null)
  const [script, setScript] = useState<ScriptOut | null>(null)
  const [lines, setLines] = useState<ScriptLineIn[]>([])
  const [loadingScript, setLoadingScript] = useState(false)
  const [saving, setSaving] = useState(false)
  const [synthesizing, setSynthesizing] = useState(false)
  const [busy, setBusy] = useState(false)
  const scriptFetched = useRef(false)

  // 切换任务时重置
  useEffect(() => {
    scriptFetched.current = false
    setTask(null)
    setScript(null)
    setLines([])
  }, [id])

  // 进度轮询：每 2s 拉一次，直到终态
  useEffect(() => {
    let alive = true
    let timer: number | undefined
    const poll = async () => {
      try {
        const t = await tasksApi.get(id)
        if (!alive) return
        setTask(t)
        if (TERMINAL.has(t.status) && timer) {
          window.clearInterval(timer)
        }
      } catch (e) {
        if (alive) message.error((e as Error).message)
      }
    }
    poll()
    timer = window.setInterval(poll, 2000)
    return () => {
      alive = false
      if (timer) window.clearInterval(timer)
    }
  }, [id, message])

  // 拉取脚本（每任务一次）。
  // ⚠️ DONE 也要拉：成片卡里的「脚本回看」就渲染在 `status === 'DONE'` 分支下，
  //    早先只按 SCRIPT_READY 拉，于是 DONE 时 `script` 恒为 null ——
  //    JSX 里写好的脚本回看成了**死代码**，成片页只看得到播放器和下载按钮。
  //    D14 浏览器留证时截图发现的（验收自测表里「成片页可回看脚本」那条原本会被漏过）。
  //    编辑能力仍只对 SCRIPT_READY 开放（见下方「脚本编辑」卡的条件），此处只读。
  useEffect(() => {
    if ((task?.status === EDITABLE || task?.status === 'DONE') && !scriptFetched.current) {
      scriptFetched.current = true
      setLoadingScript(true)
      tasksApi
        .getScript(id)
        .then((s) => {
          setScript(s)
          setLines(s.lines.map((l) => ({ speaker: l.speaker as 'A' | 'B', text: l.text })))
        })
        .catch((e) => message.error((e as Error).message))
        .finally(() => setLoadingScript(false))
    }
  }, [task?.status, id, message])

  const onChangeLine = (idx: number, patch: Partial<ScriptLineIn>) => {
    setLines((prev) => prev.map((l, i) => (i === idx ? { ...l, ...patch } : l)))
  }

  const onSave = async () => {
    setSaving(true)
    try {
      const s = await tasksApi.saveScript(id, { lines })
      setScript(s)
      message.success('脚本已保存')
    } catch (e) {
      message.error((e as Error).message || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const onSynthesize = async () => {
    setSynthesizing(true)
    try {
      await tasksApi.synthesize(id)
      scriptFetched.current = false
      message.success('已提交合成，开始生成音频…')
    } catch (e) {
      message.error((e as Error).message || '合成提交失败')
    } finally {
      setSynthesizing(false)
    }
  }

  const onRetry = async () => {
    setBusy(true)
    try {
      await tasksApi.retry(id)
      scriptFetched.current = false
      message.success('已重试')
    } catch (e) {
      message.error((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const onCancel = async () => {
    setBusy(true)
    try {
      await tasksApi.cancel(id)
      message.success('已取消')
    } catch (e) {
      message.error((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const onDelete = async () => {
    setBusy(true)
    try {
      await tasksApi.remove(id)
      message.success('已删除')
      navigate('/history')
    } catch (e) {
      message.error((e as Error).message)
      setBusy(false)
    }
  }

  if (!task) {
    return (
      <div className="flex h-64 items-center justify-center">
        <Spin />
      </div>
    )
  }

  const canCancel = task.status === 'PENDING' || task.status === 'SCRIPT_READY'

  // D12 队列提示：只在「确实在等别人」时有信息量。
  // 「该不该显示」与列表页**共用同一个闸门 `showQueue`** —— 两处各写一套判断，
  // 就是下一次漂移的种子（列表页会白名单过滤，详情页不会，两边迟早对不上）。
  // 唯一外加的例外是 pos=0：那是自己正在跑，「阶段 + 进度条」已说明一切，不再重复提示。
  const qLabel = queueLabel(task)
  const waiting = showQueue(task) && task.queue_position !== 0 && qLabel !== null
  const cLabel = showCache(task) ? cacheLabel(task) : null

  return (
    <div className="mx-auto max-w-3xl space-y-4">
      <Card>
        <div className="mb-2 flex items-center justify-between">
          <Typography.Title level={4} className="!mb-0">
            {task.script_title || task.topic}
          </Typography.Title>
          <Tag color={statusColor(task.status)}>{task.status}</Tag>
        </div>
        <Descriptions size="small" column={2}>
          <Descriptions.Item label="阶段">{task.stage || '-'}</Descriptions.Item>
          <Descriptions.Item label="进度">{task.progress}%</Descriptions.Item>
          <Descriptions.Item label="目标时长">
            {Math.round(task.target_duration_sec / 60)} 分钟
          </Descriptions.Item>
          <Descriptions.Item label="语速">{task.speed}x</Descriptions.Item>
          {cLabel && <Descriptions.Item label="句级缓存">{cLabel}</Descriptions.Item>}
        </Descriptions>
        {waiting && <Alert type="info" showIcon message={qLabel} className="mt-2" />}
        {!TERMINAL.has(task.status) && <Progress percent={task.progress} status="active" />}
        {task.error_msg && <Alert type="error" message={task.error_msg} className="mt-2" />}

        <Space className="mt-3">
          {task.status === EDITABLE && (
            <Button type="primary" loading={synthesizing} onClick={onSynthesize}>
              确认合成
            </Button>
          )}
          {task.status === 'FAILED' && (
            <Button loading={busy} onClick={onRetry}>
              重试
            </Button>
          )}
          {canCancel && (
            <Popconfirm title="确认取消该任务？" onConfirm={onCancel}>
              <Button danger loading={busy}>
                取消
              </Button>
            </Popconfirm>
          )}
          <Popconfirm title="确认删除该任务及成片？" onConfirm={onDelete}>
            <Button danger>删除</Button>
          </Popconfirm>
        </Space>
      </Card>

      {/* 脚本编辑（D8） */}
      {task.status === EDITABLE && (
        <Card
          title="脚本编辑（行级说话人切换 / 逐句试听）"
          extra={
            <Button onClick={onSave} loading={saving}>
              保存脚本
            </Button>
          }
        >
          {loadingScript ? (
            <div className="flex h-32 items-center justify-center">
              <Spin />
            </div>
          ) : (
            <div className="space-y-2">
              {lines.map((ln, i) => {
                const seg = script?.lines[i]
                const done = seg?.seg_status === 'DONE'
                return (
                  <div key={i} className="flex items-start gap-2 rounded border p-2">
                    <Select
                      value={ln.speaker}
                      style={{ width: 96 }}
                      onChange={(v) => onChangeLine(i, { speaker: v })}
                      options={[
                        { value: 'A', label: 'A 说话人' },
                        { value: 'B', label: 'B 说话人' },
                      ]}
                    />
                    <Input.TextArea
                      value={ln.text}
                      autoSize={{ minRows: 1, maxRows: 4 }}
                      onChange={(e) => onChangeLine(i, { text: e.target.value })}
                    />
                    <div className="w-44 shrink-0">
                      {done ? (
                        <audio
                          controls
                          src={tasksApi.segmentAudio(id, seg?.seq ?? 0)}
                          className="w-full"
                        />
                      ) : (
                        <span className="text-xs text-slate-400">尚未合成</span>
                      )}
                    </div>
                  </div>
                )
              })}
            </div>
          )}
          <Typography.Paragraph type="secondary" className="mt-2 text-xs">
            保存后再点「确认合成」：未改动的句子命中句级缓存，不会重复推理。
          </Typography.Paragraph>
        </Card>
      )}

      {/* 成片播放 / 下载（D9） */}
      {task.status === 'DONE' && (
        <Card title="成片">
          <audio controls src={tasksApi.audio(id)} className="w-full" />
          <div className="mt-2">
            <Button href={tasksApi.download(id)} type="primary">
              下载 mp3
            </Button>
          </div>
          {script && (
            <div className="mt-4">
              <Typography.Title level={5}>脚本回看</Typography.Title>
              <div className="space-y-1">
                {script.lines.map((l) => (
                  <div key={l.seq} className="text-sm">
                    <Tag color={l.speaker === 'A' ? 'blue' : 'orange'}>{l.speaker}</Tag>
                    {l.text}
                  </div>
                ))}
              </div>
            </div>
          )}
        </Card>
      )}
    </div>
  )
}
