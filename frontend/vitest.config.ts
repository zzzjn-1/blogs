import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// 单测配置独立于 vite.config.ts，原因是那一份带 dev server 代理，测试用不到；
// 拆开也避免 `test` 字段被 build 路径读到。
//
// 环境选 jsdom：本项目要测的不只是纯函数，还有 antd 组件的真实渲染
// （队列提示、句级缓存读数只在特定状态下出现）。
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    // 每个用例前重置 mock 调用记录，避免跨用例串味
    clearMocks: true,
    // 组件测试里不解析 CSS，省掉 tailwind 处理开销
    css: false,
  },
})
