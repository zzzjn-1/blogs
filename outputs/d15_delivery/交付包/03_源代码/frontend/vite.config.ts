import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 后端 API 与公开 feed 经 dev 代理转发（同源，Cookie 鉴权可正常传递）。
// 生产部署若前后端同域则无需代理；跨域则设置 VITE_API_BASE 指向后端并启用后端 CORS。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // 同源代理：开发时前端与后端在同机，用 127.0.0.1 最稳（任何机器都能解析到本机）。
      // 若要让同一局域网内其他设备（手机/另一台电脑）访问 dev server，
      // 把 target 改成该机的局域网 IP（如 http://192.168.1.100:8000）即可。
      // 注意：这与 .env 的 PUBLIC_BASE_URL 是两回事——后者是写给外部播客客户端的
      // RSS enclosure/cover 绝对地址，应保持为可被客户端访问到的真实地址。
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      // ⚠️ 必须是正则，且**不能**写成前缀 '/feed'：
      // 前端有一个页面路由就叫 `/feed`（频道设置），而 vite 的字符串键是**前缀匹配**，
      // 写 '/feed' 会把整棵 /feed 子树都转给后端 —— 于是直接访问或刷新 `/feed`
      // 拿到的是后端的 `{"detail":"Not Found"}`（后端只有 /feed/{token}.xml 等带子段的路径），
      // 页面打不开。D14 浏览器留证时实测踩到。
      // 收紧为「/feed/ 后至少还有一段」，裸 /feed 便落回 SPA 的 index.html。
      '^/feed/.+$': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
})
