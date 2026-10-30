import fs from 'fs';
import path from 'path';
import { defineConfig, type Plugin } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { VitePWA } from 'vite-plugin-pwa';

// VITE_SUPABASE_ANON_KEY is intentionally NOT required here: a missing key
// disables the savings leaderboard at runtime (see src/lib/supabase.ts) rather
// than failing the build, so the package/app stays publishable without it.
// jarvis gui sets the proxy target without exposing an absolute API URL to the
// browser, so requests stay on Vite's origin even on a custom frontend port.
const apiTarget = process.env.OPENJARVIS_VITE_PROXY_TARGET
  || process.env.VITE_API_URL
  || 'http://localhost:8000';

// Serves the ONNX Runtime Web WebAssembly files the wake-word detector loads
// lazily from /ort/. They are copied from node_modules at build time instead of
// being tracked in the repository, and are kept out of the PWA precache below.
function wakeRuntimeAssets(): Plugin {
  const dist = path.resolve(import.meta.dirname, 'node_modules/onnxruntime-web/dist');
  const files = ['ort-wasm-simd-threaded.mjs', 'ort-wasm-simd-threaded.wasm'];
  let outDir = '';
  return {
    name: 'jarvis-wake-ort-runtime',
    configResolved(config) {
      outDir = path.resolve(config.root, config.build.outDir);
    },
    configureServer(server) {
      server.middlewares.use('/ort/', (req, res, next) => {
        const name = path.basename((req.url || '').split('?')[0]);
        if (!files.includes(name)) return next();
        res.setHeader('Content-Type', name.endsWith('.wasm') ? 'application/wasm' : 'text/javascript');
        fs.createReadStream(path.join(dist, name)).pipe(res);
      });
    },
    closeBundle() {
      if (!fs.existsSync(dist)) return;
      fs.mkdirSync(path.join(outDir, 'ort'), { recursive: true });
      for (const name of files) fs.copyFileSync(path.join(dist, name), path.join(outDir, 'ort', name));
    },
  };
}

export default defineConfig({
  resolve: {
    // Use ONNX Runtime Web's build that loads its WebAssembly from /ort/ (see
    // wakeRuntimeAssets) instead of bundling a second 15 MB copy into assets/.
    conditions: ['onnxruntime-web-use-extern-wasm'],
    alias: {
      '@': path.resolve(import.meta.dirname, './src'),
    },
  },
  plugins: [
    react(),
    tailwindcss(),
    wakeRuntimeAssets(),
    VitePWA({
      registerType: 'autoUpdate',
      manifest: {
        name: 'OpenJarvis',
        short_name: 'Jarvis',
        description: 'On-device AI assistant',
        theme_color: '#161618',
        background_color: '#161618',
        display: 'standalone',
        icons: [
          { src: 'pwa-192x192.png', sizes: '192x192', type: 'image/png' },
          { src: 'pwa-512x512.png', sizes: '512x512', type: 'image/png' },
        ],
      },
      workbox: {
        globPatterns: ['**/*.{js,css,html,ico,png,svg}'],
        // Third-party wake-word models and the WebAssembly runtime are large,
        // local-only assets: never precache them.
        globIgnores: ['wake-models/**', 'ort/**', '**/*.onnx', '**/*.wasm'],
        navigateFallbackDenylist: [/^\/v1\//, /^\/health/, /^\/dashboard/, /^\/api\//],
      },
    }),
  ],
  build: {
    outDir: '../src/openjarvis/server/static',
    emptyOutDir: true,
    // Preserve the Vite 6 browser baseline for existing desktop webviews.
    target: ['es2020', 'edge88', 'firefox78', 'chrome87', 'safari14'],
    rolldownOptions: {
      output: {
        codeSplitting: {
          groups: [
            { name: 'react', test: /node_modules[\\/](react|react-dom)[\\/]/ },
            {
              name: 'markdown',
              test: /node_modules[\\/](react-markdown|rehype-highlight|remark-gfm)[\\/]/,
            },
            { name: 'charts', test: /node_modules[\\/]recharts[\\/]/ },
            { name: 'router', test: /node_modules[\\/]react-router[\\/]/ },
          ],
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      // ws: true is required for the /v1/agents/events WebSocket. Without it
      // Vite proxies the HTTP request but not the upgrade, so the socket never
      // opens — no error, no close event, just silence — and every live agent
      // view sits empty in dev while working in a production build.
      '/v1': {
        target: apiTarget,
        changeOrigin: true,
        ws: true,
      },
      '/health': apiTarget,
      '/api': apiTarget,
    },
  },
});
