import react from '@vitejs/plugin-react'
import { loadEnv } from 'vite'
import { defineConfig } from 'vitest/config'
import { validateProductionEnv } from './src/config/productionEnv.ts'

// https://vite.dev/config/
export default defineConfig(({ command, mode }) => {
  // A production build must never fall back to local defaults (see src/config/productionEnv.ts).
  if (command === 'build' && mode === 'production') {
    const problems = validateProductionEnv(loadEnv(mode, process.cwd(), 'VITE_'))
    if (problems.length > 0) {
      throw new Error(`Production build refused:\n- ${problems.join('\n- ')}`)
    }
  }
  return {
    plugins: [react()],
    test: {
      environment: 'jsdom',
      include: ['src/**/*.test.{ts,tsx}'],
      setupFiles: ['src/test/setup.ts'],
    },
  }
})
