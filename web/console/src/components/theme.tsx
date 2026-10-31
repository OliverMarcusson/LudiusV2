import { createContext, useContext, useState, type ReactNode } from "react"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
function initialDark() {
  try {
    const saved = localStorage.getItem("ludius-theme")
    return saved !== "light"
  } catch {
    return true
  }
}
document.documentElement.classList.toggle("dark", initialDark())
const ThemeContext = createContext({ dark: initialDark(), toggle: () => {} })
export function ThemeProvider({ children }: { children: ReactNode }) {
  const [dark, setDark] = useState(initialDark)
  function toggle() {
    const next = !dark
    document.documentElement.classList.toggle("dark", next)
    try {
      localStorage.setItem("ludius-theme", next ? "dark" : "light")
    } catch {
    }
    setDark(next)
  }
  return <ThemeContext.Provider value={{ dark, toggle }}>{children}</ThemeContext.Provider>
}
export const useTheme = () => useContext(ThemeContext)
export function ThemeButton() {
  const { dark, toggle } = useTheme()
  return (
    <ToggleGroup
      variant="outline"
      size="sm"
      type="single"
      value={dark ? "dark" : "light"}
      onValueChange={(value) => {
        if (value && value !== (dark ? "dark" : "light")) toggle()
      }}
      aria-label="Colour theme"
      spacing={0}
    >
      <ToggleGroupItem value="light" aria-label="Light mode">
        Light
      </ToggleGroupItem>
      <ToggleGroupItem value="dark" aria-label="Dark mode">
        Dark
      </ToggleGroupItem>
    </ToggleGroup>
  )
}
