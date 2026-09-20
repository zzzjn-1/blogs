import type { ReactNode } from 'react'
import { render } from '@testing-library/react'
import { App as AntApp } from 'antd'
import { MemoryRouter } from 'react-router-dom'

/**
 * 带齐上下文的渲染器。
 *
 * `App`（antd）与 `MemoryRouter` 是页面的硬依赖：前者提供 `App.useApp()`
 * 拿到的 message 实例，后者提供 `useNavigate` / `useParams`。
 * 缺任何一个都会在挂载阶段直接抛错——所以统一从这里进，用例里不再各写一套。
 */
export function renderAt(path: string, ui: ReactNode) {
  return render(
    <AntApp>
      <MemoryRouter
        initialEntries={[path]}
        // 与 main.tsx 的 BrowserRouter 保持同一组 future flag，
        // 否则测试通过、线上行为不同，等于白测。
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        {ui}
      </MemoryRouter>
    </AntApp>,
  )
}
