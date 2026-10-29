import { Toaster as Sonner, type ToasterProps } from "sonner"

// The monitor is dark only (index.html), so no theme provider; sonner's own CSS honours prefers-reduced-motion.
const Toaster = ({ ...props }: ToasterProps) => (
  <Sonner
    theme="dark"
    className="toaster group"
    style={
      {
        "--normal-bg": "var(--popover)",
        "--normal-text": "var(--popover-foreground)",
        "--normal-border": "var(--border)",
        "--border-radius": "var(--radius)",
      } as React.CSSProperties
    }
    toastOptions={{ classNames: { toast: "cn-toast" } }}
    {...props}
  />
)

export { Toaster }
