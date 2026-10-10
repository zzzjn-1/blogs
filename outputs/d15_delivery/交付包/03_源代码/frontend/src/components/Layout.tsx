import { Layout, Space, Typography, Button } from 'antd'
import { Link, Outlet, useNavigate } from 'react-router-dom'
import { useAuth } from '../hooks/useAuth'

const { Header, Content } = Layout

export default function AppLayout() {
  const { user, logout } = useAuth()
  const navigate = useNavigate()

  const onLogout = async () => {
    await logout()
    navigate('/login', { replace: true })
  }

  return (
    <Layout className="h-full">
      <Header className="flex items-center justify-between bg-white px-6 shadow-sm">
        <Space size="large">
          <Typography.Text strong className="text-base">
            双人对话播客生成
          </Typography.Text>
          <Link to="/new">新建节目</Link>
          <Link to="/history">历史</Link>
          <Link to="/feed">频道设置</Link>
        </Space>
        <Space>
          <span className="text-slate-500">{user?.username}</span>
          <Button size="small" onClick={onLogout}>
            退出
          </Button>
        </Space>
      </Header>
      <Content className="overflow-auto bg-slate-50 p-6">
        <Outlet />
      </Content>
    </Layout>
  )
}
