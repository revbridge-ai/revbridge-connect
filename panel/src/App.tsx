import { Route, Routes } from 'react-router-dom'
import { RequireAuth } from './layout/AppShell'
import { LoginPage } from './pages/LoginPage'
import { OverviewPage } from './pages/OverviewPage'
import { EmailsPage } from './pages/EmailsPage'
import { ApiKeysPage } from './pages/ApiKeysPage'
import { DomainsPage } from './pages/DomainsPage'
import { SuppressionsPage } from './pages/SuppressionsPage'
import { WebhooksPage } from './pages/WebhooksPage'
import { ApiDocsPage } from './pages/ApiDocsPage'
import { NotFoundPage } from './pages/NotFoundPage'

export function App() {
  return (
    <Routes>
      {/* The login page itself redirects users who already have a session. */}
      <Route path="/login" element={<LoginPage />} />
      <Route element={<RequireAuth />}>
        <Route index element={<OverviewPage />} />
        <Route path="emails" element={<EmailsPage />} />
        <Route path="keys" element={<ApiKeysPage />} />
        <Route path="domains" element={<DomainsPage />} />
        <Route path="suppressions" element={<SuppressionsPage />} />
        <Route path="webhooks" element={<WebhooksPage />} />
        <Route path="docs" element={<ApiDocsPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  )
}
