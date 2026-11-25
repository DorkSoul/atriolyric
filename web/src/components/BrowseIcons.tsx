import type { SVGProps } from 'react'

// Small glyphs for the secondary browse screens (Playlists, Radio, Files,
// Want list), drawn like Icons.tsx: currentColor, 1em, hidden from readers.
function Base(props: SVGProps<SVGSVGElement>) {
  return <svg viewBox="0 0 24 24" width="1em" height="1em" fill="currentColor" aria-hidden="true" {...props} />
}

export function PlusIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <Base {...props}>
      <path d="M11 5a1 1 0 0 1 2 0v6h6a1 1 0 0 1 0 2h-6v6a1 1 0 0 1-2 0v-6H5a1 1 0 0 1 0-2h6z" />
    </Base>
  )
}

export function DownloadIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <Base {...props}>
      <path d="M11 4a1 1 0 0 1 2 0v8.59l2.3-2.3a1 1 0 1 1 1.4 1.42l-4 4a1 1 0 0 1-1.4 0l-4-4a1 1 0 1 1 1.4-1.42l2.3 2.3zM5 18a1 1 0 0 1 1 1h12a1 1 0 0 1 2 0 2 2 0 0 1-2 2H6a2 2 0 0 1-2-2 1 1 0 0 1 1-1z" />
    </Base>
  )
}

export function FolderIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <Base {...props}>
      <path d="M3 6.5A2.5 2.5 0 0 1 5.5 4h3.59a2 2 0 0 1 1.41.59L12.41 6.5h6.09A2.5 2.5 0 0 1 21 9v8.5a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 17.5z" />
    </Base>
  )
}

export function FileMusicIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <Base {...props}>
      <path d="M17 3v10.55A4 4 0 1 0 19 17V7h3V3zM9 5v8.55A4 4 0 1 0 11 17V9l4-1V4z" />
    </Base>
  )
}

export function RadioIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <Base {...props}>
      <path d="M17.4 3.1a1 1 0 0 1 .5 1.3L16.7 7H19a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2h9.5l1.6-3.4a1 1 0 0 1 1.3-.5zM8 11a3 3 0 1 0 0 6 3 3 0 0 0 0-6zm6 1a1 1 0 1 0 0 2h4a1 1 0 1 0 0-2zm0 3a1 1 0 1 0 0 2h4a1 1 0 1 0 0-2z" />
    </Base>
  )
}

export function TrashIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <Base {...props}>
      <path d="M9 3a1 1 0 0 0-1 1v1H5a1 1 0 0 0 0 2h.1l.8 12.1A2 2 0 0 0 7.9 21h8.2a2 2 0 0 0 2-1.9L18.9 7h.1a1 1 0 1 0 0-2h-3V4a1 1 0 0 0-1-1zm1 2h4v0h-4zM7.1 7h9.8l-.8 12H7.9zM10 9a1 1 0 0 0-1 1v6a1 1 0 1 0 2 0v-6a1 1 0 0 0-1-1zm4 0a1 1 0 0 0-1 1v6a1 1 0 1 0 2 0v-6a1 1 0 0 0-1-1z" />
    </Base>
  )
}
