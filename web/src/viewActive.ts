import { createContext, useContext } from 'react'

/** False for a tab view that is mounted but hidden (see the `<Activity>` in
 * App.tsx). Hidden React effects are disconnected, but a portal into the
 * header is still rendered, so anything that portals out checks this. */
export const ViewActiveContext = createContext(true)

export function useViewActive(): boolean {
  return useContext(ViewActiveContext)
}
