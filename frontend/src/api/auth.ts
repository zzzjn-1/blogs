import { api } from './client'
import type { TokenOut } from './types'

export const authApi = {
  register: (username: string, password: string) =>
    api.post<TokenOut>('/api/auth/register', { username, password }),
  login: (username: string, password: string) =>
    api.post<TokenOut>('/api/auth/login', { username, password }),
  logout: () => api.post<{ detail: string }>('/api/auth/logout'),
}
