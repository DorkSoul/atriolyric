import { lazy, type ComponentType } from 'react'

/** A lazily loaded view, plus a way to start downloading its code early. */
export type LazyView<P extends object> = ComponentType<P> & {
  /** Starts the download now (idle time, a hover) so the first render does
   * not wait for it. Safe to call any number of times; a failure is quiet
   * and the next render asks again. */
  preload: () => void
}

/** A view whose code loads on demand (`React.lazy`), shown inside a
 * `<Suspense>` and an error boundary. Unlike a bare `lazy()`, a failed load
 * (offline, a deploy that replaced the file) is not remembered: the next
 * render, such as the boundary's "Reload this view", asks for the file again.
 * Once the code is in (preloaded, or loaded by a first render) the view
 * renders straight away, without suspending even once. */
export function lazyView<P extends object>(load: () => Promise<ComponentType<P>>): LazyView<P> {
  let pending: Promise<ComponentType<P>> | null = null
  let Resolved: ComponentType<P> | null = null
  const start = () =>
    (pending ??= load().then(
      (component) => {
        Resolved = component
        return component
      },
      (error: unknown) => {
        pending = null
        throw error
      },
    ))
  const create = () =>
    lazy(() =>
      start().then(
        (component) => ({ default: component }),
        (error: unknown) => {
          Loaded = create()
          throw error
        },
      ),
    )
  let Loaded = create()
  function LazyView(props: P) {
    const View = Resolved ?? Loaded
    return <View {...props} />
  }
  LazyView.preload = () => {
    start().catch(() => {})
  }
  return LazyView
}
