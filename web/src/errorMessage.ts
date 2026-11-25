import { ApiError } from './api'

/** The text to show for a failed load: the server's or the network's own
 * message when `request()` produced one, otherwise `fallback`. */
export function errorMessage(error: unknown, fallback: string): string {
  return error instanceof ApiError && error.message ? error.message : fallback
}
