import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

// Fonts are bundled, never fetched from a CDN: the demo must work on a hotspot or with no network at all.
import '@fontsource-variable/archivo/wdth.css'
import '@fontsource/ibm-plex-mono/400.css'
import '@fontsource/ibm-plex-mono/500.css'
import '@fontsource/instrument-serif/400.css'
import '@fontsource/instrument-serif/400-italic.css'
import '@fontsource/noto-sans-devanagari/400.css'
import '@fontsource/noto-sans-devanagari/600.css'
import '@fontsource/noto-sans-kannada/400.css'
import '@fontsource/noto-sans-kannada/600.css'
import '@fontsource/noto-serif-devanagari/400.css'
import '@fontsource/noto-serif-kannada/400.css'
import './styles.css'

import App from './App'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
