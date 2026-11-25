/** "1 track", "12 tracks". */
export function trackCountLabel(count: number): string {
  return `${count} ${count === 1 ? 'track' : 'tracks'}`
}

/** "1 album", "3 albums". */
export function albumCountLabel(count: number): string {
  return `${count} ${count === 1 ? 'album' : 'albums'}`
}
