import { Toaster as Sonner, type ToasterProps } from "sonner"
import { useTheme } from "@/components/theme"

const Toaster = ({ ...props }: ToasterProps) => {
  const { dark } = useTheme()
  return (
    <Sonner
      theme={dark ? "dark" : "light"}
      className="toaster group"
      style={
        {
          "--normal-bg": "var(--popover)",
          "--normal-text": "var(--popover-foreground)",
          "--normal-border": "var(--border)",
          "--border-radius": "var(--radius)"
        } as React.CSSProperties
      }
      toastOptions={{ classNames: { toast: "cn-toast" } }}
      {...props}
    />
  )
}

export { Toaster }
