import { api } from './client'
import type { FeedOut, FeedUpdateIn } from './types'

export const feedApi = {
  get: () => api.get<FeedOut>('/api/feeds/me'),
  update: (body: FeedUpdateIn) => api.put<FeedOut>('/api/feeds/me', body),
}
