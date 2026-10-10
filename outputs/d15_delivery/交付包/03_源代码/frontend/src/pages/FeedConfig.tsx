import { useEffect, useState } from 'react'
import { App, Button, Card, Form, Input, Switch, Typography } from 'antd'
import { feedApi } from '../api/feed'
import type { FeedOut } from '../api/types'

export default function FeedConfig() {
  const { message } = App.useApp()
  const [form] = Form.useForm()
  const [feed, setFeed] = useState<FeedOut | null>(null)
  const [saving, setSaving] = useState(false)
  const [resetting, setResetting] = useState(false)

  useEffect(() => {
    feedApi
      .get()
      .then((f) => {
        setFeed(f)
        form.setFieldsValue({
          title: f.title,
          description: f.description,
          category: f.category,
          explicit: f.explicit,
        })
      })
      .catch((e) => message.error((e as Error).message))
  }, [form, message])

  const onSave = async () => {
    const v = await form.validateFields()
    setSaving(true)
    try {
      const f = await feedApi.update(v)
      setFeed(f)
      message.success('已保存')
    } catch (e) {
      message.error((e as Error).message || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const onResetToken = async () => {
    setResetting(true)
    try {
      const f = await feedApi.update({ reset_token: true })
      setFeed(f)
      message.success('订阅地址已重置，旧地址立即失效')
    } catch (e) {
      message.error((e as Error).message)
    } finally {
      setResetting(false)
    }
  }

  const origin = window.location.origin
  const feedUrl = feed ? `${origin}/feed/${feed.user_token}.xml` : ''
  const coverUrl = feed?.cover_url || ''

  return (
    <Card title="频道设置与 RSS" className="mx-auto max-w-2xl">
      <Form form={form} layout="vertical">
        <Form.Item name="title" label="频道标题">
          <Input />
        </Form.Item>
        <Form.Item name="description" label="频道描述">
          <Input.TextArea />
        </Form.Item>
        <Form.Item name="category" label="分类（iTunes）">
          <Input placeholder="如 Technology / Society" />
        </Form.Item>
        <Form.Item name="explicit" label="包含露骨内容" valuePropName="checked">
          <Switch />
        </Form.Item>
        <Button type="primary" loading={saving} onClick={onSave}>
          保存
        </Button>
      </Form>

      <Typography.Title level={5} className="mt-6">
        订阅源
      </Typography.Title>
      <div className="rounded bg-slate-50 p-3 text-sm">
        <div className="mb-1">RSS 地址：</div>
        <Typography.Paragraph copyable>{feedUrl || '加载中…'}</Typography.Paragraph>
        <div className="mb-1 mt-2">封面地址：</div>
        <Typography.Paragraph copyable>{coverUrl || '未配置'}</Typography.Paragraph>
      </div>
      <Button danger className="mt-3" loading={resetting} onClick={onResetToken}>
        重置订阅地址（旧地址立即失效）
      </Button>
    </Card>
  )
}
