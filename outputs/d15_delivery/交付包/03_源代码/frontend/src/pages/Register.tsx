import { useState } from 'react'
import { App, Button, Card, Form, Input } from 'antd'
import { Link, useNavigate } from 'react-router-dom'
import { useAuth } from '../hooks/useAuth'

export default function Register() {
  const { register } = useAuth()
  const { message } = App.useApp()
  const navigate = useNavigate()
  const [loading, setLoading] = useState(false)

  const onFinish = async (values: { username: string; password: string }) => {
    setLoading(true)
    try {
      await register(values.username, values.password)
      message.success('注册成功')
      navigate('/new', { replace: true })
    } catch (e) {
      message.error((e as Error).message || '注册失败')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex h-full items-center justify-center bg-slate-50">
      <Card title="注册 · 双人对话播客生成" className="w-96">
        <Form layout="vertical" onFinish={onFinish}>
          <Form.Item
            name="username"
            label="用户名（3~32 位字母数字 _ . -）"
            rules={[{ required: true, message: '请输入用户名' }]}
          >
            <Input autoFocus />
          </Form.Item>
          <Form.Item name="password" label="密码（6 位以上）" rules={[{ required: true, message: '请输入密码' }]}>
            <Input.Password />
          </Form.Item>
          <Button type="primary" htmlType="submit" block loading={loading}>
            注册并登录
          </Button>
        </Form>
        <div className="mt-3 text-center text-sm text-slate-500">
          已有账号？<Link to="/login">去登录</Link>
        </div>
      </Card>
    </div>
  )
}
