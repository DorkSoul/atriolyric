import { createServer as createHttpServer } from 'node:http'
import { connect, createServer, type Socket } from 'node:net'

// The phone reaches the dev server through `adb reverse`, which runs over the
// adb link, not the phone's network: turning Wi-Fi and mobile data off does
// not touch it, and neither DevTools' offline emulation nor removing the
// reverse rule closes connections already open. So the phone's traffic goes
// through this small TCP relay, which can cut every connection (and refuse
// new ones) like a network that went away, and let them through again.
//   phone localhost:3000 -> adb reverse -> RELAY_PORT -> 127.0.0.1:3000 (Vite)
// It runs in the runner's main process (global setup); tests switch it
// through a control port.

export const RELAY_PORT = 3100
const CONTROL_PORT = 3101

export async function startRelay(targetPort: number): Promise<() => Promise<void>> {
  const open = new Set<Socket>()
  // Every socket, the Vite ones too, so the relay can close at the end.
  const all = new Set<Socket>()
  let online = true
  // The Vite dev client's own sockets (HMR, and its "is the server back?"
  // pings) are never cut: when its HMR socket drops, the dev client reloads
  // the page once the server answers again - a dev-server behaviour that
  // the production app (no Vite client) does not have. They are told apart
  // by the WebSocket subprotocol in the first request.
  const relay = createServer((client) => {
    all.add(client)
    client.on('close', () => all.delete(client))
    client.on('error', () => undefined)
    client.once('data', (first) => {
      const infra = /sec-websocket-protocol:\s*vite-(hmr|ping)/i.test(first.toString('latin1'))
      if (!online && !infra) {
        client.resetAndDestroy()
        return
      }
      const upstream = connect(targetPort, '127.0.0.1')
      upstream.on('error', () => undefined)
      all.add(upstream)
      upstream.on('close', () => all.delete(upstream))
      if (!infra) {
        for (const s of [client, upstream]) {
          open.add(s)
          s.on('close', () => open.delete(s))
        }
      }
      upstream.write(first)
      client.pipe(upstream)
      upstream.pipe(client)
      client.on('close', () => upstream.destroy())
      upstream.on('close', () => client.destroy())
    })
  })
  const control = createHttpServer((req, res) => {
    if (req.url === '/offline') {
      online = false
      for (const s of open) s.resetAndDestroy()
    } else if (req.url === '/online') {
      online = true
    }
    res.end(JSON.stringify({ online, open: open.size }))
  })
  await new Promise<void>((resolve) => relay.listen(RELAY_PORT, '127.0.0.1', resolve))
  await new Promise<void>((resolve) => control.listen(CONTROL_PORT, '127.0.0.1', resolve))
  return async () => {
    for (const s of all) s.destroy()
    await new Promise((resolve) => relay.close(resolve))
    control.closeAllConnections()
    await new Promise((resolve) => control.close(resolve))
  }
}

/** Cut (false) or restore (true) the phone's connections to the servers. */
export async function setPhoneNetwork(online: boolean): Promise<{ online: boolean; open: number }> {
  const res = await fetch(`http://127.0.0.1:${CONTROL_PORT}/${online ? 'online' : 'offline'}`)
  return (await res.json()) as { online: boolean; open: number }
}
