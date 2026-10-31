import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import "./index.css"
import App from "./components/console.tsx"
import { PortraitProvider } from "./components/portraits"
import { ThemeProvider } from "./components/theme"

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ThemeProvider>
      <PortraitProvider>
        <App />
      </PortraitProvider>
    </ThemeProvider>
  </StrictMode>
)
