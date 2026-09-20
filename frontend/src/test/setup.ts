import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

// 每个用例后卸载组件：antd 会在 window 上挂监听，不清理会污染下一个用例。
afterEach(() => {
  cleanup()
})

// jsdom 不实现 matchMedia，而 antd 的响应式组件（Row/Col、List、Descriptions）
// 在挂载时会读它。不打桩会在渲染阶段直接抛 TypeError。
if (!window.matchMedia) {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  })
}

// 同上：rc-resize-observer 依赖 ResizeObserver，jsdom 未实现。
if (!('ResizeObserver' in window)) {
  class RO {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  Object.defineProperty(window, 'ResizeObserver', { writable: true, value: RO })
}
