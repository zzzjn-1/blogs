import { Navigate, Route, Routes } from 'react-router-dom'
import AppLayout from './components/Layout'
import ProtectedRoute from './components/ProtectedRoute'
import Login from './pages/Login'
import Register from './pages/Register'
import Submit from './pages/Submit'
import TaskDetail from './pages/TaskDetail'
import History from './pages/History'
import FeedConfig from './pages/FeedConfig'

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/register" element={<Register />} />
      <Route
        element={
          <ProtectedRoute>
            <AppLayout />
          </ProtectedRoute>
        }
      >
        <Route path="/" element={<Navigate to="/new" replace />} />
        <Route path="/new" element={<Submit />} />
        <Route path="/tasks/:id" element={<TaskDetail />} />
        <Route path="/history" element={<History />} />
        <Route path="/feed" element={<FeedConfig />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
