import { TopBarBack } from '../headerControlsPortal'
import { MarqueeText } from './MarqueeText'

interface Props {
  backLabel: string
  onBack: () => void
  title: string
}

/** A back button with the current artist/album's full name to its right,
 * scrolling like the Now Playing view's title (MarqueeText) instead of
 * wrapping or getting truncated when it's too long for the row. Shown in the
 * top bar, where other pages show their title (see TopBarBack). */
export function BrowseDetailHeader({ backLabel, onBack, title }: Props) {
  return (
    <TopBarBack label={backLabel} onBack={onBack}>
      <MarqueeText text={title} className="browse-detail-title" />
    </TopBarBack>
  )
}
