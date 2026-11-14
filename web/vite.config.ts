/// <reference types="vitest/config" />
import { fileURLToPath } from 'node:url';
import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';
import { mockGuard } from './build/guard';
import { afternoonZone } from './build/test-zone';

// Several agents share a machine, so each can point its panel at its own cloud.
const api = { target: process.env.AGENTOS_API_TARGET ?? 'http://localhost:8300', changeOrigin: false };

export default defineConfig(({ mode }) => ({
  plugins: [react(), tailwindcss(), mockGuard(mode, fileURLToPath(new URL('.', import.meta.url)))],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  server: {
    port: 5310,
    strictPort: true,
    // ws: the lane live view and terminal are WebSockets under /api.
    proxy: { '/api': { ...api, ws: true }, '/v1': { ...api, ws: true } },
  },
  preview: { port: 5310, strictPort: true, proxy: { '/api': { ...api, ws: true }, '/v1': { ...api, ws: true } } },
  build: { target: 'es2023', sourcemap: false },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}', 'build/**/*.test.ts'],
    css: false,
    env: { VITE_API_MODE: 'live', TZ: afternoonZone() },
  },
}));
