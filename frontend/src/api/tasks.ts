import { API_BASE, api } from './client'
import type { TaskOut, TaskPageOut, ScriptOut, TaskCreateIn, ScriptSaveIn } from './types'

export const tasksApi = {
  create: (body: TaskCreateIn) => api.post<TaskOut>('/api/tasks', body),
  list: (page = 1, pageSize = 20) =>
    api.get<TaskPageOut>(`/api/tasks?page=${page}&page_size=${pageSize}`),
  get: (id: string) => api.get<TaskOut>(`/api/tasks/${id}`),
  getScript: (id: string) => api.get<ScriptOut>(`/api/tasks/${id}/script`),
  saveScript: (id: string, body: ScriptSaveIn) =>
    api.put<ScriptOut>(`/api/tasks/${id}/script`, body),
  synthesize: (id: string) => api.post<TaskOut>(`/api/tasks/${id}/synthesize`),
  retry: (id: string) => api.post<TaskOut>(`/api/tasks/${id}/retry`),
  cancel: (id: string) => api.post<TaskOut>(`/api/tasks/${id}/cancel`),
  remove: (id: string) => api.del<{ detail: string }>(`/api/tasks/${id}`),
  // 媒体直链：浏览器请求带 Cookie 鉴权，支持 HTTP Range（可拖动播放）
  segmentAudio: (id: string, seq: number) =>
    `${API_BASE}/api/tasks/${id}/segments/${seq}/audio`,
  audio: (id: string) => `${API_BASE}/api/tasks/${id}/audio`,
  download: (id: string) => `${API_BASE}/api/tasks/${id}/download`,
}
