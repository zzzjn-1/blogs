import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { App, Button, Card, Empty, List, Tag, Typography } from 'antd'
import { tasksApi } from '../api/tasks'
import type { TaskOut } from '../api/types'
import { cacheLabel, queueLabel, showCache, showQueue } from '../lib/taskView'

function statusColor(s: string): string {
  if (s === 'DONE') return 'green'
  if (s === 'FAILED' || s === 'CANCELED') return 'red'
  return 'processing'
}

export default function History() {
  const { message } = App.useApp()
  const navigate = useNavigate()
  const [items, setItems] = useState<TaskOut[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(false)
  const pageSize = 20

  const load = async (p: number) => {
    setLoading(true)
    try {
      const r = await tasksApi.list(p, pageSize)
      setItems(r.items)
      setTotal(r.total)
      setPage(p)
    } catch (e) {
      message.error((e as Error).message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load(1)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <Card title="历史节目" className="mx-auto max-w-3xl">
      {items.length === 0 && !loading ? (
        <Empty description="还没有节目，去新建一期吧" />
      ) : (
        <List
          loading={loading}
          dataSource={items}
          renderItem={(t) => (
            <List.Item
              actions={[
                <Button key="open" type="link" onClick={() => navigate(`/tasks/${t.id}`)}>
                  打开
                </Button>,
              ]}
            >
              <List.Item.Meta
                title={
                  <span>
                    {t.script_title || t.topic}{' '}
                    <Tag color={statusColor(t.status)}>{t.status}</Tag>
                  </span>
                }
                description={
                  <span>
                    {`${Math.round(t.target_duration_sec / 60)} 分钟 · ${t.line_count} 句 · ${t.progress}%`}
                    {showQueue(t) && (
                      <span className="ml-2 text-slate-400">{queueLabel(t)}</span>
                    )}
                    {showCache(t) && (
                      <span className="ml-2 text-slate-400">{cacheLabel(t)}</span>
                    )}
                  </span>
                }
              />
            </List.Item>
          )}
        />
      )}
      <div className="mt-3 flex justify-end">
        <Button disabled={page <= 1} className="mr-2" onClick={() => load(page - 1)}>
          上一页
        </Button>
        <Button disabled={page * pageSize >= total} onClick={() => load(page + 1)}>
          下一页
        </Button>
      </div>
    </Card>
  )
}
