import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { PublicClientApplication } from '@azure/msal-browser'
import { MsalProvider } from '@azure/msal-react'
import { msalConfig } from './auth/msalConfig'
import { AuthGuard } from './auth/AuthGuard'
import './index.css'
import App from './App.tsx'

const msalInstance = new PublicClientApplication(msalConfig)

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <MsalProvider instance={msalInstance}>
      <AuthGuard>
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </AuthGuard>
    </MsalProvider>
  </StrictMode>,
)
