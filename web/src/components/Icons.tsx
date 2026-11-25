import type { SVGProps } from 'react'
import type { Tab } from '../nav'

/** Plain vector glyphs, not Unicode media-control characters - some
 * platforms render those (⏸ especially) via a colorful emoji font no CSS
 * `color` can override (the pause button can show up orange).
 * `fill="currentColor"` ties these to the button's own text color instead,
 * so they always match. */
function IconBase(props: SVGProps<SVGSVGElement>) {
  return <svg viewBox="0 0 24 24" width="1em" height="1em" fill="currentColor" aria-hidden="true" {...props} />
}

export function PlayIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <IconBase {...props}>
      <path d="M8 5v14l11-7z" />
    </IconBase>
  )
}

export function InfoIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <IconBase {...props}>
      <circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" strokeWidth="2" />
      <path d="M11 10h2v7h-2zm0-4h2v2h-2z" />
    </IconBase>
  )
}

export function PauseIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <IconBase {...props}>
      <path d="M6 5h4v14H6zm8 0h4v14h-4z" />
    </IconBase>
  )
}

export function SkipPrevIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <IconBase {...props}>
      <path d="M6 6h2v12H6zm3.5 6l8.5 6V6z" />
    </IconBase>
  )
}

export function SkipNextIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <IconBase {...props}>
      <path d="M6 18l8.5-6L6 6v12zM16 6v12h2V6h-2z" />
    </IconBase>
  )
}

export function HeartIcon({ filled, ...props }: SVGProps<SVGSVGElement> & { filled: boolean }) {
  return (
    <IconBase {...props}>
      {filled ? (
        <path d="M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z" />
      ) : (
        <path d="M16.5 3c-1.74 0-3.41.81-4.5 2.09C10.91 3.81 9.24 3 7.5 3 4.42 3 2 5.42 2 8.5c0 3.78 3.4 6.86 8.55 11.54L12 21.35l1.45-1.32C18.6 15.36 22 12.28 22 8.5 22 5.42 19.58 3 16.5 3zm-4.4 15.55l-.1.1-.1-.1C7.14 14.24 4 11.39 4 8.5 4 6.5 5.5 5 7.5 5c1.54 0 3.04.99 3.57 2.36h1.87C13.46 5.99 14.96 5 16.5 5c2 0 3.5 1.5 3.5 3.5 0 2.89-3.14 5.74-7.9 10.05z" />
      )}
    </IconBase>
  )
}

/** The disliked state of the song/album/artist heart: a filled heart with a
 * crack cut through it (the crack is a hole via evenodd, so it shows the
 * background in any theme). */
export function BrokenHeartIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <IconBase {...props}>
      <path
        fillRule="evenodd"
        d="M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35zM12.2 5.6l-2.6 4.7 2.9 2.6-2.2 4.4 1.1 2.6.5-.3-.8-2.3 2.3-4.6-2.8-2.5 2.5-4.4z"
      />
    </IconBase>
  )
}

export function ShuffleIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <IconBase {...props}>
      <path d="M10.59 9.17L5.41 4 4 5.41l5.17 5.17 1.42-1.41zM14.5 4l2.04 2.04L4 18.59 5.41 20 17.96 7.46 20 9.5V4h-5.5zm.33 9.41l-1.41 1.41 3.13 3.13L14.5 20H20v-5.5l-2.04 2.04-3.13-3.13z" />
    </IconBase>
  )
}

export function RepeatIcon({ mode, ...props }: SVGProps<SVGSVGElement> & { mode: 'off' | 'all' | 'one' }) {
  return (
    <IconBase {...props}>
      <path d="M7 7h10a2 2 0 0 1 2 2v3h-2V9H7v3L3 8l4-4v3zm10 10H7a2 2 0 0 1-2-2v-3h2v3h10v-3l4 4-4 4v-3z" />
      {mode === 'one' && (
        <text x="12" y="14.5" textAnchor="middle" fontSize="7" fontWeight="700" fill="currentColor">
          1
        </text>
      )}
    </IconBase>
  )
}

export function QueueIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <IconBase {...props}>
      <path d="M15 6H3v2h12V6zm0 4H3v2h12v-2zM3 16h8v-2H3v2zm15-4.83V4h-2v7.17c-.31-.11-.65-.17-1-.17-1.66 0-3 1.34-3 3s1.34 3 3 3 3-1.34 3-3v-3h3V9h-3v2.17z" />
    </IconBase>
  )
}

export function LyricsIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <IconBase {...props}>
      <path d="M4 5h16v2H4zm0 4h16v2H4zm0 4h10v2H4zm0 4h13v2H4z" />
    </IconBase>
  )
}

export function DragHandleIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <IconBase {...props}>
      <path d="M3 6h18v2H3zm0 5h18v2H3zm0 5h18v2H3z" />
    </IconBase>
  )
}

export function RefreshIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <IconBase {...props}>
      <path d="M12 5V1L7 6l5 5V7c3.31 0 6 2.69 6 6 0 1.01-.25 1.97-.7 2.8l1.46 1.46A7.93 7.93 0 0020 13c0-4.42-3.58-8-8-8zm-6 8c0-1.01.25-1.97.7-2.8L5.24 8.74A7.93 7.93 0 004 13c0 4.42 3.58 8 8 8v4l5-5-5-5v4c-3.31 0-6-2.69-6-6z" />
    </IconBase>
  )
}

export function ChevronDownIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <IconBase {...props}>
      <path d="M6 9l6 6 6-6" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
    </IconBase>
  )
}

export function MenuIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <IconBase {...props}>
      <path d="M4 6h16M4 12h16M4 18h16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </IconBase>
  )
}

/** A bar chart in a circle: the Library info screen. */
export function LibraryInfoIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <IconBase {...props}>
      <circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" strokeWidth="1.8" />
      <path d="M8.5 15v-3m3.5 3V8m3.5 7v-5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </IconBase>
  )
}

/** Compact monochrome glyphs for the mobile tab shortcuts. */
export function TabIcon({ tab, ...props }: SVGProps<SVGSVGElement> & { tab: Tab }) {
  switch (tab) {
    case 'home':
      return <IconBase {...props}><path d="m3 10 9-7 9 7v10a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1V10z" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" /></IconBase>
    case 'songs':
      return <IconBase {...props}><path d="M9 18V5l12-2v13" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" /><ellipse cx="6" cy="18" rx="3" ry="2" /><ellipse cx="18" cy="16" rx="3" ry="2" /></IconBase>
    case 'albums':
      return <IconBase {...props}><rect x="3" y="4" width="18" height="16" rx="2" fill="none" stroke="currentColor" strokeWidth="1.8" /><circle cx="12" cy="12" r="4.2" fill="none" stroke="currentColor" strokeWidth="1.8" /><circle cx="12" cy="12" r="1" /></IconBase>
    case 'artists':
      return <IconBase {...props}><circle cx="9" cy="8" r="3.2" fill="none" stroke="currentColor" strokeWidth="1.8" /><path d="M3 20v-1.2a5.6 5.6 0 0 1 11.2 0V20H3zm12.1-14a3.1 3.1 0 0 1 0 6.1m2.2 2.2a4.8 4.8 0 0 1 3.7 4.7v1h-4.1" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" /></IconBase>
    case 'library-info':
      return <LibraryInfoIcon {...props} />
    case 'recent-played':
      return <IconBase {...props}><path d="M3.5 11a8.5 8.5 0 1 1 2.1 5.7M3 5v6h6m3-6v7l4 2" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" /></IconBase>
    case 'videos':
      return <IconBase {...props}><rect x="3" y="5" width="18" height="14" rx="2" fill="none" stroke="currentColor" strokeWidth="1.8" /><path d="m10 9 5 3-5 3z" /></IconBase>
    case 'files':
      return <IconBase {...props}><path d="M3 6a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6z" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" /></IconBase>
    case 'radio':
      return <IconBase {...props}><rect x="3" y="8" width="18" height="12" rx="2" fill="none" stroke="currentColor" strokeWidth="1.8" /><path d="m7 8 10-5M7 13h5m-5 3h3m7-2h.01" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" /><circle cx="17" cy="15" r="1.7" fill="none" stroke="currentColor" strokeWidth="1.5" /></IconBase>
    case 'playlists':
      return <IconBase {...props}><path d="M4 5h10M4 10h10M4 15h6" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" /><path d="M16 11v8l5-2.5V9l-5 2z" /><circle cx="14.5" cy="19" r="1.5" /><circle cx="19.5" cy="17" r="1.5" /></IconBase>
    case 'now-playing':
      return <IconBase {...props}><circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" strokeWidth="1.8" /><path d="M8 14.5v-2m2.7 2.5V9m2.6 6v-4.5m2.7 4v-2" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" /></IconBase>
    case 'want-list':
      return <IconBase {...props}><path d="M6 3h12a1 1 0 0 1 1 1v17l-7-4-7 4V4a1 1 0 0 1 1-1z" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" /><path d="M12 7v6m-3-3h6" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" /></IconBase>
    case 'admin':
      return <IconBase {...props}><path d="M12 3 20 6v5c0 5-3.4 8.2-8 10-4.6-1.8-8-5-8-10V6l8-3z" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" /><circle cx="12" cy="9" r="2" fill="none" stroke="currentColor" strokeWidth="1.5" /><path d="M8.5 16a3.5 3.5 0 0 1 7 0" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" /></IconBase>
    case 'settings':
      return <SettingsIcon {...props} />
  }
}

export function EyeIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <IconBase {...props}>
      <path d="M12 4.5C7 4.5 2.73 7.61 1 12c1.73 4.39 6 7.5 11 7.5s9.27-3.11 11-7.5c-1.73-4.39-6-7.5-11-7.5zm0 12.5c-2.76 0-5-2.24-5-5s2.24-5 5-5 5 2.24 5 5-2.24 5-5 5zm0-8c-1.66 0-3 1.34-3 3s1.34 3 3 3 3-1.34 3-3-1.34-3-3-3z" />
    </IconBase>
  )
}

export function EyeOffIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <IconBase {...props}>
      <path d="M12 7c2.76 0 5 2.24 5 5 0 .65-.13 1.26-.36 1.83l2.92 2.92c1.51-1.26 2.7-2.89 3.44-4.75-1.73-4.39-6-7.5-11-7.5-1.27 0-2.49.2-3.64.57l2.17 2.17c.56-.22 1.16-.24 1.47-.24zM2 4.27l2.28 2.28.46.45C3.08 8.3 1.78 10.02 1 12c1.73 4.39 6 7.5 11 7.5 1.55 0 3.03-.3 4.38-.84l.43.43L19.73 22 21 20.73 3.27 3 2 4.27zM7.53 9.8l1.55 1.55c-.05.21-.08.43-.08.65 0 1.66 1.34 3 3 3 .22 0 .44-.03.65-.08l1.55 1.55c-.67.33-1.41.53-2.2.53-2.76 0-5-2.24-5-5 0-.79.2-1.53.53-2.2zm4.31-.78l3.15 3.15.02-.16c0-1.66-1.34-3-3-3l-.17.01z" />
    </IconBase>
  )
}

export function SearchIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <IconBase {...props}>
      <path d="M15.5 14h-.79l-.28-.27C15.41 12.59 16 11.11 16 9.5 16 5.91 13.09 3 9.5 3S3 5.91 3 9.5 5.91 16 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z" />
    </IconBase>
  )
}

export function SortIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <IconBase {...props}>
      <path d="M3 6h18a1 1 0 0 1 0 2H3a1 1 0 0 1 0-2zm0 5h12a1 1 0 0 1 0 2H3a1 1 0 0 1 0-2zm0 5h6a1 1 0 0 1 0 2H3a1 1 0 0 1 0-2z" />
    </IconBase>
  )
}

export function GridViewIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <IconBase {...props}>
      <path d="M4 4h7v7H4V4zm9 0h7v7h-7V4zM4 13h7v7H4v-7zm9 0h7v7h-7v-7z" />
    </IconBase>
  )
}

export function ListViewIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <IconBase {...props}>
      <path d="M3 5h18a1 1 0 0 1 0 2H3a1 1 0 0 1 0-2zm0 6h18a1 1 0 0 1 0 2H3a1 1 0 0 1 0-2zm0 6h18a1 1 0 0 1 0 2H3a1 1 0 0 1 0-2z" />
    </IconBase>
  )
}

export function CheckIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <IconBase {...props}>
      <path d="M9 16.2 4.8 12l-1.4 1.4L9 19 21 7l-1.4-1.4z" />
    </IconBase>
  )
}

export function ArrowUpIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <IconBase {...props}>
      <path d="M12 7.5 6.5 15h11z" />
    </IconBase>
  )
}

export function ArrowDownIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <IconBase {...props}>
      <path d="M12 16.5 17.5 9h-11z" />
    </IconBase>
  )
}

export function SettingsIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <IconBase {...props}>
      <path d="M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58a.5.5 0 0 0 .12-.61l-1.92-3.32a.5.5 0 0 0-.59-.22l-2.39.96a7.03 7.03 0 0 0-1.62-.94l-.36-2.54a.5.5 0 0 0-.5-.42h-3.84a.5.5 0 0 0-.5.42l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96a.5.5 0 0 0-.59.22L2.74 8.85a.5.5 0 0 0 .12.61l2.03 1.58c-.05.3-.09.63-.09.94 0 .31.02.64.07.94l-2.03 1.58a.5.5 0 0 0-.12.61l1.92 3.32c.14.24.42.34.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.25.42.5.42h3.84c.25 0 .45-.18.5-.42l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.24.09.5 0 .59-.22l1.92-3.32a.5.5 0 0 0-.12-.61zM12 15.6A3.6 3.6 0 1 1 12 8.4a3.6 3.6 0 0 1 0 7.2z" />
    </IconBase>
  )
}

const outlineProps = {
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 2,
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
} as const

/** Floppy-disk save glyph: outline when `filled` is false, solid (body filled,
 * label/shutter cut out) when true. */
export function FloppyIcon({ filled, ...props }: SVGProps<SVGSVGElement> & { filled: boolean }) {
  return (
    <IconBase {...props}>
      {filled ? (
        <>
          <path
            fillRule="evenodd"
            d="M5 3h11.17L21 7.83V19a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2zm2 1v5h8V4H7zm-1 9a1 1 0 0 0-1 1v6h14v-6a1 1 0 0 0-1-1H6z"
          />
        </>
      ) : (
        <>
          <path {...outlineProps} d="M5 3h11.17L21 7.83V19a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z" />
          <path {...outlineProps} d="M7 3v6h8V3M6.5 21v-7h11v7" />
        </>
      )}
    </IconBase>
  )
}

/** Play triangle: outline when `filled` is false, solid when true. */
export function PlayToggleIcon({ filled, ...props }: SVGProps<SVGSVGElement> & { filled: boolean }) {
  return (
    <IconBase {...props}>
      {filled ? (
        <path d="M8 5v14l11-7z" stroke="currentColor" strokeWidth="2" strokeLinejoin="round" />
      ) : (
        <path {...outlineProps} d="M8 5v14l11-7z" />
      )}
    </IconBase>
  )
}

/** Shuffle crossing arrows: outline strokes when `filled` is false, the solid
 * glyph when true. */
export function ShuffleToggleIcon({ filled, ...props }: SVGProps<SVGSVGElement> & { filled: boolean }) {
  if (filled) return <ShuffleIcon {...props} />
  return (
    <IconBase {...props}>
      <path {...outlineProps} d="M16 3h5v5M4 20 21 3M21 16v5h-5M15 15l6 6M4 4l5 5" />
    </IconBase>
  )
}

/** Three dots in a row: the phone tab bar's "More". */
export function MoreHorizontalIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <IconBase {...props}>
      <circle cx="5" cy="12" r="1.9" />
      <circle cx="12" cy="12" r="1.9" />
      <circle cx="19" cy="12" r="1.9" />
    </IconBase>
  )
}

export function CloseIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <IconBase {...props}>
      <path d="M6 6l12 12M18 6 6 18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </IconBase>
  )
}

const tabStroke = { ...outlineProps, strokeWidth: 1.8 } as const

export function ChevronLeftIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <IconBase {...props}>
      <path {...tabStroke} strokeWidth={2} d="M15 6l-6 6 6 6" />
    </IconBase>
  )
}

export function ChevronRightIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <IconBase {...props}>
      <path {...tabStroke} strokeWidth={2} d="M9 6l6 6-6 6" />
    </IconBase>
  )
}

/** "Back": an arrow pointing left, drawn so it centres on the cap height. */
export function ArrowLeftIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <IconBase {...props}>
      <path {...outlineProps} d="M19 12H5m6-6-6 6 6 6" />
    </IconBase>
  )
}

/** A speaker with sound waves: the playback-device picker and volume. */
export function SpeakerIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <IconBase {...props}>
      <path d="M4 9h3.5L12 5v14l-4.5-4H4z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
      <path {...tabStroke} d="M15.5 9a4 4 0 0 1 0 6m2.5-8.5a7.5 7.5 0 0 1 0 11" />
    </IconBase>
  )
}

export function StopIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <IconBase {...props}>
      <rect x="6" y="6" width="12" height="12" rx="1.5" />
    </IconBase>
  )
}

export function WarningIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <IconBase {...props}>
      <path {...tabStroke} d="M12 3.5 2.5 20h19z" />
      <path {...outlineProps} d="M12 10v4.5m0 2.5h.01" />
    </IconBase>
  )
}

export function ExternalLinkIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <IconBase {...props}>
      <path {...tabStroke} d="M14 4h6v6m0-6-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5" />
    </IconBase>
  )
}

/** A record: album artwork fallback. */
export function DiscIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <IconBase {...props}>
      <circle {...tabStroke} cx="12" cy="12" r="9" />
      <circle {...tabStroke} cx="12" cy="12" r="3" />
      <circle cx="12" cy="12" r="0.9" />
    </IconBase>
  )
}

/** A microphone: artist artwork fallback. */
export function MicIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <IconBase {...props}>
      <rect {...tabStroke} x="9" y="3" width="6" height="11" rx="3" />
      <path {...tabStroke} d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21m-3.5 0h7" />
    </IconBase>
  )
}

export function VideoIcon(props: SVGProps<SVGSVGElement>) {
  return <TabIcon tab="videos" {...props} />
}

export function SparkleIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <IconBase {...props}>
      <path d="M12 2.5l1.9 5.6 5.6 1.9-5.6 1.9L12 17.5l-1.9-5.6L4.5 10l5.6-1.9z" />
      <path d="M18.5 15l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8z" />
    </IconBase>
  )
}

/** A single quaver: the generic music fallback. */
export function MusicNoteIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <IconBase {...props}>
      <path {...tabStroke} d="M10 18V5l9-2v12" />
      <ellipse cx="7" cy="18" rx="3" ry="2.2" />
      <ellipse cx="16" cy="15" rx="3" ry="2.2" />
    </IconBase>
  )
}

/** Sliders: "Customise". */
export function CustomiseIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <IconBase {...props}>
      <path {...tabStroke} d="M4 7h9m4 0h3M4 17h3m4 0h9" />
      <circle {...tabStroke} cx="15" cy="7" r="2" />
      <circle {...tabStroke} cx="9" cy="17" r="2" />
    </IconBase>
  )
}

const turnProps = {
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.75,
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
} as const

/** An anticlockwise turn arrow marked "30": skip back thirty seconds. */
export function SkipBack30Icon(props: SVGProps<SVGSVGElement>) {
  return (
    <IconBase {...turnProps} {...props}>
      <path d="M3 12a9 9 0 1 0 3-6.7" />
      <path d="M3 3v6h6" />
      <text x="12" y="15.5" textAnchor="middle" fontSize="7.5" fontWeight="700" fill="currentColor" stroke="none">
        30
      </text>
    </IconBase>
  )
}

/** A clockwise turn arrow marked "30": skip forward thirty seconds. */
export function SkipForward30Icon(props: SVGProps<SVGSVGElement>) {
  return (
    <IconBase {...turnProps} {...props}>
      <path d="M21 12a9 9 0 1 1-3-6.7" />
      <path d="M21 3v6h-6" />
      <text x="12" y="15.5" textAnchor="middle" fontSize="7.5" fontWeight="700" fill="currentColor" stroke="none">
        30
      </text>
    </IconBase>
  )
}

/** A clockwise turn arrow with no label: rotate the screen. */
export function RotateIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <IconBase {...turnProps} {...props}>
      <path d="M21 12a9 9 0 1 1-3-6.7" />
      <path d="M21 3v6h-6" />
    </IconBase>
  )
}
