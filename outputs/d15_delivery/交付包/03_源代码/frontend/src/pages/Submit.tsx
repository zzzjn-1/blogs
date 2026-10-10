import { useState } from 'react'
import { App, Button, Card, Form, Input, InputNumber, Slider } from 'antd'
import { useNavigate } from 'react-router-dom'
import { tasksApi } from '../api/tasks'

export default function Submit() {
  const { message } = App.useApp()
  const navigate = useNavigate()
  const [loading, setLoading] = useState(false)

  const onFinish = async (values: {
    topic: string
    duration_min: number
    style: string
    speed: number
  }) => {
    setLoading(true)
    try {
      const task = await tasksApi.create({
        topic: values.topic,
        duration_min: values.duration_min,
        style: values.style || undefined,
        speed: values.speed,
      })
      message.success('已创建任务，正在生成脚本…')
      navigate(`/tasks/${task.id}`)
    } catch (e) {
      message.error((e as Error).message || '创建失败')
    } finally {
      setLoading(false)
    }
  }

  return (
    <Card title="新建一期节目" className="mx-auto max-w-2xl">
      <Form
        layout="vertical"
        initialValues={{ duration_min: 5, speed: 1.0 }}
        onFinish={onFinish}
      >
        <Form.Item
          name="topic"
          label="主题"
          rules={[{ required: true, message: '请输入节目主题' }]}
        >
          <Input placeholder="例如：爱情这件小事有多复杂" maxLength={200} />
        </Form.Item>
        <Form.Item name="duration_min" label="目标时长（分钟）" rules={[{ required: true }]}>
          <InputNumber min={0.5} max={60} step={0.5} className="w-40" />
        </Form.Item>
        <Form.Item name="speed" label="语速（1.0 为基准）">
          <Slider min={0.5} max={2} step={0.05} />
        </Form.Item>
        <Form.Item name="style" label="语言风格（可选）">
          <Input placeholder="例如：轻松闲聊、知识科普" />
        </Form.Item>
        <Button type="primary" htmlType="submit" loading={loading}>
          生成脚本并合成
        </Button>
      </Form>
    </Card>
  )
}
