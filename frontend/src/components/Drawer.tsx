import { useEffect, type ReactNode } from 'react'

interface DrawerProps {
  title: string
  onClose: () => void
  children: ReactNode
}

/** Shared evidence drawer. It is deliberately a modal side sheet: the map remains visible
 * behind the evidence chain, while Escape/backdrop/close all have predictable semantics. */
export default function Drawer({ title, onClose, children }: DrawerProps) {
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [onClose])

  return (
    <div className="drawer-backdrop" onMouseDown={onClose}>
      <aside
        aria-label={title}
        aria-modal="true"
        className="drawer"
        role="dialog"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="drawer-head">
          <div>
            <span className="drawer-kicker">evidence chain</span>
            <h2>{title}</h2>
          </div>
          <button type="button" className="drawer-close" aria-label="Close evidence" onClick={onClose}>
            ×
          </button>
        </header>
        <div className="drawer-body">{children}</div>
      </aside>
    </div>
  )
}
