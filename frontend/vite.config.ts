import react from '@vitejs/plugin-react';
import { resolve } from 'node:path';
import { defineConfig, type Plugin } from 'vite';

/** Friendly URLs in dev/preview: /report -> citizen.html, /sim -> simulation.html */
function pageRoutes(): Plugin {
  const rewrite = (url: string | undefined): string | undefined => {
    if (!url) return url;
    const [path, query] = url.split('?');
    const map: Record<string, string> = {
      '/report': '/citizen.html',
      '/sim': '/simulation.html',
    };
    const target = map[path.replace(/\/$/, '')];
    return target ? (query ? `${target}?${query}` : target) : url;
  };
  return {
    name: 'page-routes',
    configureServer(server) {
      server.middlewares.use((req, _res, next) => {
        req.url = rewrite(req.url);
        next();
      });
    },
    configurePreviewServer(server) {
      server.middlewares.use((req, _res, next) => {
        req.url = rewrite(req.url);
        next();
      });
    },
  };
}

export default defineConfig({
  plugins: [react(), pageRoutes()],
  build: {
    rollupOptions: {
      input: {
        main: resolve(__dirname, 'index.html'),
        citizen: resolve(__dirname, 'citizen.html'),
        simulation: resolve(__dirname, 'simulation.html'),
      },
    },
  },
  server: {
    port: 5173,
    host: '0.0.0.0',
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8742',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
});
