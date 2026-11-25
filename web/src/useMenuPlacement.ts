import { useEffect, useRef, useState, type CSSProperties } from 'react'

/** Anchors a fixed-position menu panel at a point, nudged back onscreen
 * once its real size is known. */
export function useMenuPlacement(anchor: { x: number; y: number }, deps: unknown[] = []) {
  const ref = useRef<HTMLDivElement>(null)
  const [style, setStyle] = useState<CSSProperties>({ left: anchor.x, top: anchor.y, visibility: 'hidden' })
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const rect = el.getBoundingClientRect()
    // 12px from the window edges, as every pop-up keeps on a phone.
    const margin = 12
    setStyle({
      left: Math.max(margin, Math.min(anchor.x, window.innerWidth - rect.width - margin)),
      top: Math.max(margin, Math.min(anchor.y, window.innerHeight - rect.height - margin)),
      visibility: 'visible',
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [anchor, ...deps])
  return { ref, style }
}
