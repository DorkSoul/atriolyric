/** `behavior` for a scripted scroll: smooth, unless the user asked for reduced motion. */
export function scrollBehavior(): ScrollBehavior {
  const reduce = typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches
  return reduce ? 'auto' : 'smooth'
}
