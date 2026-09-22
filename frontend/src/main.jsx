import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.jsx'

// No StrictMode: it double-mounts every component in development (mount ->
// unmount -> mount again) specifically to catch effects that don't clean
// up idempotently. react-force-graph-2d (CatalogGraph.jsx, CurationGraph.jsx)
// wraps a canvas + its own internal animation loop via an imperative ref -
// it isn't built to survive being torn down and immediately rebuilt like
// that, and the double-mount is what was causing the graphs to work
// briefly and then lock up: two simulation instances would start,
// silently interfere, and the visible one would stop updating once the
// other's cooldown timer fired. StrictMode has no effect in production
// builds either way - this only changes dev-server behavior.
createRoot(document.getElementById('root')).render(<App />)
