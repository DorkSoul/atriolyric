import type { MediaItem, PreferenceState } from './types'

const NEXT: Record<PreferenceState, PreferenceState> = {
  none: 'liked',
  liked: 'disliked',
  disliked: 'none',
}

/** The heart's cycle: none -> liked -> disliked -> none. */
export function nextPreference(state: PreferenceState): PreferenceState {
  return NEXT[state]
}

export function trackPreference(track: Pick<MediaItem, 'is_favorite' | 'is_disliked'>): PreferenceState {
  if (track.is_disliked) return 'disliked'
  return track.is_favorite ? 'liked' : 'none'
}

export const DISLIKED_MESSAGE = 'Disliked – won’t appear in mixes'
