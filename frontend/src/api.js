export async function api(path, opts = {}) {
  const r = await fetch('/api' + path, {
    method: opts.method || 'GET', credentials: 'same-origin', signal: opts.signal,
    headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'hbm' },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  })
  const data = await r.json().catch(() => ({}))
  if (r.status === 401 && path !== '/auth/login') window.dispatchEvent(new Event('hbm-unauth'))
  if (!r.ok) throw new Error(data.detail || 'HTTP ' + r.status)
  return data
}
