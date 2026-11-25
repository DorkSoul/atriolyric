import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type Dispatch,
  type ReactNode,
  type SetStateAction,
} from 'react'
import { setDebugLogSignedIn } from './clientDebugLog'
import { api, ApiError, clearBrowseCache, primeOfflineLibrary, setUnauthorizedHandler } from './api'
import { useToast } from './ToastContext'
import type { CurrentUser } from './types'
import { forgetOfflineData, keepsAnswer, rememberAnswer } from './native/offlineCache'
import { forgetOfflinePrime } from './native/offlineLibrary'
import { forgetLocalPlayback } from './native/localPlayback'
import { isNativeApp } from './native/bridge'
import { forgetDownloadRecords, startCollectionSync } from './native/downloadActions'

/** Which account the Android app's offline queue and kept collections belong to. */
const OFFLINE_ACCOUNT_KEY = 'atriolyric.offlineAccount'

interface AuthContextValue {
  user: CurrentUser | null
  loading: boolean
  /** The session ended while signed in (not a fresh visit or a logout). */
  sessionExpired: boolean
  login: (username: string, password: string) => Promise<void>
  logout: () => Promise<void>
  // Expose React's full setter type so callers can pass a functional
  // updater (e.g. merging an accent-color broadcast) instead of only a
  // complete replacement object.
  setUser: Dispatch<SetStateAction<CurrentUser | null>>
}

const AuthContext = createContext<AuthContextValue | null>(null)

// No answer at all (offline, DNS, the server down) or the proxy in front of a
// stopped server answering for it - as opposed to the server saying who we are.
function isUnreachable(error: unknown): boolean {
  // Status 0 is `request()`'s own "no answer" (a timeout or a dead connection).
  return !(error instanceof ApiError) || error.status === 0 || error.status >= 500
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const { muteErrorToasts } = useToast()
  const [user, setUser] = useState<CurrentUser | null>(null)
  const [loading, setLoading] = useState(true)
  // The first /api/auth/me could not reach the server. Showing the login form
  // then would ask a signed-in user to sign in again for a network problem.
  const [unreachable, setUnreachable] = useState(false)
  const [checking, setChecking] = useState(true)
  const [sessionExpired, setSessionExpired] = useState(false)
  const userRef = useRef(user)
  useEffect(() => {
    userRef.current = user
    // Debug tracing posts to an endpoint that needs a session.
    setDebugLogSignedIn(user != null)
    // The Android app fetches the library for offline use (a no-op elsewhere).
    if (user != null) {
      primeOfflineLibrary()
      // ...and keeps its downloaded albums, artists, playlists and mixes current.
      startCollectionSync()
    }
  }, [user])
  const checkRef = useRef(0)

  // State is only set once the answer is in; a retry marks itself as
  // checking first (see `retry`).
  // `initial`: the page's first check, which public/boot.js may already have
  // sent while the app was downloading; a retry asks afresh.
  const checkSession = useCallback((initial = false) => {
    const check = ++checkRef.current
    api
      .me({ probe: true, initial })
      .then((me) => {
        if (check !== checkRef.current) return
        setUser(me)
        setUnreachable(false)
      })
      .catch((e: unknown) => {
        if (check !== checkRef.current) return
        // A 401 is the normal signed-out answer: show the login page.
        setUnreachable(isUnreachable(e))
      })
      .finally(() => {
        if (check !== checkRef.current) return
        setChecking(false)
        setLoading(false)
      })
  }, [])

  useEffect(() => {
    checkSession(true)
    return () => {
      // A StrictMode remount or unmount: the answer belongs to no one.
      checkRef.current += 1
    }
  }, [checkSession])

  const retry = useCallback(() => {
    setChecking(true)
    checkSession()
  }, [checkSession])

  // Try again by itself when the connection comes back or the user returns.
  useEffect(() => {
    if (!unreachable) return
    const retryWhenVisible = () => {
      if (document.visibilityState === 'visible') retry()
    }
    window.addEventListener('online', retryWhenVisible)
    document.addEventListener('visibilitychange', retryWhenVisible)
    return () => {
      window.removeEventListener('online', retryWhenVisible)
      document.removeEventListener('visibilitychange', retryWhenVisible)
    }
  }, [unreachable, retry])

  // Any 401 while signed in (see api.ts) ends the session here, once: the
  // requests already in flight fail the same way a moment later and find no
  // user, and their error toasts are held back so the login page is not
  // buried under them.
  useEffect(() => {
    setUnauthorizedHandler(() => {
      if (userRef.current == null) return
      userRef.current = null
      muteErrorToasts(5_000)
      clearBrowseCache()
      setSessionExpired(true)
      setUser(null)
    })
    return () => setUnauthorizedHandler(null)
  }, [muteErrorToasts])

  // Whoever is signed in when the app starts owns what it kept offline (an
  // app updated from a version that did not note the account).
  useEffect(() => {
    if (!user || !isNativeApp()) return
    try {
      if (localStorage.getItem(OFFLINE_ACCOUNT_KEY) == null) localStorage.setItem(OFFLINE_ACCOUNT_KEY, String(user.id))
    } catch {
      // No storage: nothing kept to protect either.
    }
  }, [user])

  /** Notes which account this app's offline state belongs to; true when it
   * belonged to another one. */
  function rememberAccount(userId: number): boolean {
    try {
      const before = localStorage.getItem(OFFLINE_ACCOUNT_KEY)
      localStorage.setItem(OFFLINE_ACCOUNT_KEY, String(userId))
      return before != null && before !== String(userId)
    } catch {
      return false
    }
  }

  async function login(username: string, password: string) {
    // The Android app keeps answers for offline use: none of another account's.
    await forgetOfflineData()
    forgetOfflinePrime()
    const loggedIn = await api.login(username, password)
    // The queue played offline and the collections kept downloaded are the
    // account's own choices: kept when the same account signs in again (a
    // session that ran out), dropped for another one - a queue handed back
    // would otherwise become the new account's queue.
    if (isNativeApp() && rememberAccount(loggedIn.id)) {
      forgetLocalPlayback()
      forgetDownloadRecords()
    }
    // Who is signed in, for a start with no connection (the sign-in answer is
    // the same as /api/auth/me's, which this page never asked successfully).
    if (keepsAnswer('/api/auth/me', 'GET')) void rememberAnswer('/api/auth/me', loggedIn)
    setSessionExpired(false)
    setUser(loggedIn)
  }

  async function logout() {
    try {
      await api.logout()
    } catch {
      // Signed out here regardless: a failed request (offline) must not
      // leave the user stuck in the app. The server still ends the session
      // when it expires.
    }
    await forgetOfflineData()
    forgetOfflinePrime()
    setSessionExpired(false)
    setUser(null)
  }

  return (
    <AuthContext value={{ user, loading, sessionExpired, login, logout, setUser }}>
      {unreachable ? <ServerUnreachable checking={checking} onRetry={retry} /> : children}
    </AuthContext>
  )
}

function ServerUnreachable({ checking, onRetry }: { checking: boolean; onRetry: () => void }) {
  return (
    <div className="login-page">
      <div className="login-form">
        <h1>
          <img src="/favicon.svg" alt="" className="app-logo" />
          Atriolyric
        </h1>
        <output>Can&apos;t reach the server. It will try again when you are back online.</output>
        <button type="button" onClick={onRetry} disabled={checking}>
          {checking ? 'Retrying…' : 'Retry'}
        </button>
      </div>
    </div>
  )
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}
