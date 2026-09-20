import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import type { UserOut } from '../api/types'
import { authApi } from '../api/auth'
import { API_BASE } from '../api/client'

interface AuthState {
  user: UserOut | null
  loading: boolean
  login: (username: string, password: string) => Promise<void>
  register: (username: string, password: string) => Promise<void>
  logout: () => Promise<void>
}

const AuthContext = createContext<AuthState | undefined>(undefined)

const STORAGE_KEY = 'pc_user'

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<UserOut | null>(null)
  const [loading, setLoading] = useState(true)

  // 启动时：从 localStorage 恢复显示用 user；再探测 Cookie 是否仍有效，
  // 无效则清掉（后端无 /me 接口，无法直接取回 username，故以 localStorage 持久化）。
  useEffect(() => {
    let cancelled = false
    const saved = localStorage.getItem(STORAGE_KEY)
    if (saved) {
      try {
        setUser(JSON.parse(saved) as UserOut)
      } catch {
        localStorage.removeItem(STORAGE_KEY)
      }
    }
    ;(async () => {
      try {
        const res = await fetch(`${API_BASE}/api/tasks?page=1&page_size=1`, {
          credentials: 'include',
        })
        if (!res.ok && res.status === 401) {
          localStorage.removeItem(STORAGE_KEY)
          if (!cancelled) setUser(null)
        }
      } catch {
        /* 网络错误不处理，保留现状 */
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  const persist = (u: UserOut | null) => {
    if (u) localStorage.setItem(STORAGE_KEY, JSON.stringify(u))
    else localStorage.removeItem(STORAGE_KEY)
    setUser(u)
  }

  const doLogin = async (username: string, password: string) => {
    const t = await authApi.login(username, password)
    persist(t.user)
  }
  const doRegister = async (username: string, password: string) => {
    const t = await authApi.register(username, password)
    persist(t.user)
  }
  const doLogout = async () => {
    await authApi.logout()
    persist(null)
  }

  return (
    <AuthContext.Provider value={{ user, loading, login: doLogin, register: doRegister, logout: doLogout }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}
