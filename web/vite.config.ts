import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// 开发期代理：线上由 nginx 把 `/api/` 转到 api 容器（见 infra/nginx/default.conf），
// 本地 `pnpm dev` 没有 nginx，所以这里补同样的一条，前端代码里一律用相对路径
// `/api/v1/...` —— 不写死 host，也不在代码里分环境。
// 端口用 5174：5173 留给 compose 里的 web 容器（它服务的是构建产物）。
// 不引 @types/node：只为读一个环境变量装一整套 Node 类型不值得，这里显式取。
const nodeProcess = (globalThis as { process?: { env?: Record<string, string | undefined> } }).process;
const apiTarget = nodeProcess?.env?.VITE_API_TARGET ?? 'http://127.0.0.1:8000';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    host: true,
    proxy: {
      '/api': {
        target: apiTarget,
        changeOrigin: true,
        // SSE 不能被缓冲：代理层默认会攒，攒完前端就只看到「一次性吐出」。
        configure: (proxy) => {
          proxy.on('proxyRes', (proxyRes) => {
            proxyRes.headers['cache-control'] = 'no-cache';
          });
        },
      },
    },
  },
});
