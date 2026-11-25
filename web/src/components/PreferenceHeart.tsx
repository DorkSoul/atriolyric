import { DISLIKED_MESSAGE, nextPreference } from '../preference'
import { useToast } from '../ToastContext'
import type { PreferenceState } from '../types'
import { useOptimisticChange } from '../useOptimisticChange'
import { BrokenHeartIcon, HeartIcon } from './Icons'

// Without a subject, the label just says what the next press does.
const LABELS: Record<PreferenceState, string> = {
  none: 'Like',
  liked: 'Dislike',
  disliked: 'Clear dislike',
}

// With a subject the label also says what the heart applies to and where it
// is now, since three states cannot be told apart from the next action alone
// (and a list of unnamed "Like" buttons says nothing about which is which).
const STATE_NAMES: Record<PreferenceState, string> = {
  none: 'Not liked',
  liked: 'Liked',
  disliked: 'Disliked',
}
const NEXT_ACTIONS: Record<PreferenceState, string> = {
  none: 'like',
  liked: 'dislike',
  disliked: 'clear the dislike',
}


interface Props {
  state: PreferenceState
  /** Applies the next state, and has `state` say so before it resolves.
   * Rejects when the change failed - the caller reports the error itself.
   * Called inside this heart's transition, so its synchronous part may set
   * the caller's own optimistic state (useOptimistic) too. */
  onChange: (next: PreferenceState) => Promise<unknown>
  className: string
  /** Added on top of `className` while liked (the filled heart). */
  activeClassName: string
  title?: string
  /** What the heart applies to (a track, album or artist name), for its
   * accessible name. */
  subject?: string
}

/** The three-state heart for songs, albums and artists: outline (none) ->
 * filled (liked) -> broken, muted (disliked) -> outline. */
export function PreferenceHeart({ state, onChange, className, activeClassName, title, subject }: Props) {
  const { showToast } = useToast()
  // The next state is shown at once and only while the change is out: on
  // success `state` has already caught up, on failure the heart goes back to
  // `state` by itself. Pressing again meanwhile is not possible.
  const { shown, busy, change } = useOptimisticChange(state, async (next) => {
    await onChange(next)
    if (next === 'disliked') showToast(DISLIKED_MESSAGE, 'success')
  })

  const stateClass =
    shown === 'liked' ? activeClassName : shown === 'disliked' ? 'preference-disliked' : ''
  return (
    <button
      className={`${className} ${stateClass}`}
      onClick={() => change(nextPreference(shown))}
      disabled={busy}
      aria-label={
        subject ? `${STATE_NAMES[shown]}: ${subject}. Press to ${NEXT_ACTIONS[shown]}` : LABELS[shown]
      }
      title={title}
    >
      {shown === 'disliked' ? <BrokenHeartIcon /> : <HeartIcon filled={shown === 'liked'} />}
    </button>
  )
}
