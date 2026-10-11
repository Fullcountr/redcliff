import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import '@fontsource/schibsted-grotesk/400.css'
import '@fontsource/schibsted-grotesk/600.css'
import '@fontsource/schibsted-grotesk/700.css'
import '@fontsource/fragment-mono/400.css'
import '@xyflow/react/dist/style.css'
import './index.css'
import { ReactFlowProvider } from '@xyflow/react'
import App from './App.tsx'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ReactFlowProvider>
      <App />
    </ReactFlowProvider>
  </StrictMode>,
)
