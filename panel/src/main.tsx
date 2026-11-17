import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import '@fontsource-variable/inter'
import './index.css'
import { App } from './App'
import { I18nProvider } from './i18n'
import { AuthProvider } from './lib/auth'
import { ToastProvider } from './components/Toast'

const root = document.getElementById('root')
if (!root) throw new Error('Element #root not found')

createRoot(root).render(
  <StrictMode>
    <BrowserRouter
      basename={import.meta.env.BASE_URL.replace(/\/$/, '') || '/'}
      future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
    >
      <I18nProvider>
        <ToastProvider>
          <AuthProvider>
            <App />
          </AuthProvider>
        </ToastProvider>
      </I18nProvider>
    </BrowserRouter>
  </StrictMode>,
)
