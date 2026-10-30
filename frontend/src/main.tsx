import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { API_BASE_URL } from './api/baseUrl.ts'
import { AuthProvider } from './auth/AuthProvider.tsx'
import { readAuthConfig } from './auth/config.ts'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <AuthProvider config={readAuthConfig(import.meta.env)} apiBaseUrl={API_BASE_URL}>
      <App />
    </AuthProvider>
  </StrictMode>,
)
