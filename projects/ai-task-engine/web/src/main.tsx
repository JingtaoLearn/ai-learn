import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { AuthGuard } from './auth/AuthGuard'
import './index.css'
import App from './App.tsx'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <AuthGuard>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </AuthGuard>
  </StrictMode>,
)
