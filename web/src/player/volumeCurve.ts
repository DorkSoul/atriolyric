// The volume slider's position (0..1) as the gain the players apply.
//
// Hearing is logarithmic: the same step in decibels sounds like the same step
// in loudness. A slider passed straight through as amplitude spends almost
// all of its audible change in a small part of its travel, so instead each
// percent of travel is the same number of decibels, from VOLUME_RANGE_DB below
// full at the bottom to full at the top, and 0 is silence.

export const VOLUME_RANGE_DB = 50

export function volumeGain(position: number): number {
  if (!(position > 0)) return 0
  if (position >= 1) return 1
  return 10 ** (((position - 1) * VOLUME_RANGE_DB) / 20)
}
