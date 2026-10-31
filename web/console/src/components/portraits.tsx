import { createContext, useContext, useEffect, useState, type ReactNode } from "react"
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar"
import { Button } from "@/components/ui/button"
import type { Person } from "@/lib/console"

type PortraitState = { images: Record<string, string>; enabled: boolean; logo?: string | null }
const PortraitContext = createContext<
  PortraitState & {
    busy: string | null
    error: string | null
    generate: (user: string, replace?: boolean) => Promise<void>
  }
>({
  images: {},
  enabled: false,
  busy: null,
  error: null,
  generate: async () => {}
})
export function PortraitProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<PortraitState>({
    images: {},
    enabled: false
  })
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  async function refresh() {
    const response = await fetch("/api/portraits", { cache: "no-store" })
    if (!response.ok) throw new Error("Portraits unavailable")
    setState(await response.json())
  }
  useEffect(() => {
    void refresh().catch(() => setError("Portraits unavailable"))
  }, [])
  async function generate(user: string, replace = false) {
    if (busy !== null) return
    setBusy(user)
    setError(null)
    try {
      const path = user ? `/api/portraits/${encodeURIComponent(user)}` : "/api/logo"
      const response = await fetch(
        `${path}${replace ? "?replace=true" : ""}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}"
        }
      )
      if (!response.ok) {
        const problem = await response.json()
        throw new Error(problem.detail || problem.error || "Image generation failed")
      }
      await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : "Image generation failed")
    } finally {
      setBusy(null)
    }
  }
  return (
    <PortraitContext.Provider value={{ ...state, busy, error, generate }}>
      {children}
    </PortraitContext.Provider>
  )
}
export function Portrait({
  person,
  className
}: {
  person: Pick<Person, "user" | "name">
  className?: string
}) {
  const { images } = useContext(PortraitContext)
  const src = images[person.user]
  return (
    <Avatar className={className} aria-hidden="true">
      {src && <AvatarImage src={src} alt="" />}
      <AvatarFallback className="font-medium text-foreground">
        {person.name
          .split(" ")
          .map((n) => n[0])
          .slice(0, 2)
          .join("")}
      </AvatarFallback>
    </Avatar>
  )
}
export function PortraitButton({ person }: { person: Person }) {
  const { images, enabled, busy, generate } = useContext(PortraitContext)
  return (
    <Button
      variant="outline"
      size="sm"
      disabled={!enabled || busy !== null}
      aria-label={`${images[person.user] ? "Regenerate" : "Generate"} portrait for ${person.name}`}
      onClick={() => void generate(person.user, !!images[person.user])}
    >
      {busy === person.user
        ? "Generating…"
        : images[person.user]
          ? "Regenerate portrait"
          : "Generate portrait"}
    </Button>
  )
}
export function CompanyLogo({ className }: { className?: string }) {
  const { logo } = useContext(PortraitContext)
  return logo ? <img src={logo} alt="" className={className} /> : null
}
export function LogoButton() {
  const { logo, enabled, busy, generate } = useContext(PortraitContext)
  return (
    <Button
      variant="outline"
      size="sm"
      disabled={!enabled || busy !== null}
      onClick={() => void generate("", !!logo)}
    >
      {busy === "" ? "Generating…" : logo ? "Regenerate logo" : "Generate logo"}
    </Button>
  )
}
export function PortraitStatus() {
  const { error, enabled } = useContext(PortraitContext)
  return error ? (
    <p role="alert" className="text-sm text-lost">
      {error}
    </p>
  ) : !enabled ? (
    <p className="text-sm text-muted-foreground">
      Image generation is unavailable until OpenRouter is configured.
    </p>
  ) : null
}
