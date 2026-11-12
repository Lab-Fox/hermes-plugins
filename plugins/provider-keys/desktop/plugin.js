// Provider Keys — a status-bar menu to see and switch the API key a provider starts with.
// Disk plugin: plain ESM, no JSX. Talks to dashboard/plugin_api.py; never sees key material.
import {
  host, useValue, useQuery, useQueryClient,
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger
} from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'provider-keys'
let rest = null

const clock = seconds => new Date(seconds * 1000).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })

function keyNote(key) {
  if (key.resting) return key.until ? `limited until ${clock(key.until)}` : `limited${key.since ? ` since ${clock(key.since)}` : ''}`
  if (key.skip) return 'manual only'
  return key.active ? `starts first · ${key.masked}` : key.masked
}

function KeyRows({ provider, onUse }) {
  return provider.keys.map(key =>
    jsxs(DropdownMenuItem, {
      onSelect: () => onUse(provider.name, key.label),
      className: 'flex items-center gap-2',
      children: [
        jsx('span', { className: 'w-3 shrink-0 text-(--ui-accent)', children: key.label === provider.using ? '●' : '' }),
        jsx('span', { className: `min-w-0 flex-1 truncate ${key.resting ? 'text-(--ui-text-tertiary)' : ''}`, children: key.label }),
        jsx('span', { className: 'shrink-0 pl-4 text-[0.6875rem] text-(--ui-text-tertiary)', children: keyNote(key) })
      ]
    }, `${provider.name}:${key.label}`)
  )
}

const heading = text => jsx('div', { className: 'px-2 py-1 text-[0.6875rem] text-(--ui-text-tertiary)', children: text })

function KeyMenu() {
  const sid = useValue(host.state.focusedStoredSessionId)
  const busy = useValue(host.state.busy)
  const client = useQueryClient()
  const [other, setOther] = useState('')
  const queryKey = [ID, 'state', sid || '']
  const query = useQuery({
    queryKey,
    queryFn: () => rest(`/state?session=${encodeURIComponent(sid || '')}`),
    refetchInterval: 30000
  })
  // A turn that just ended may have moved to another key: show it.
  useEffect(() => { if (!busy) query.refetch() }, [busy])

  const data = query.data
  if (!data || !data.providers.length) return null
  const current = data.providers.find(p => p.name === data.current) || null
  const shown = data.providers.find(p => p.name === other) || current
  const limited = current ? current.keys.filter(k => k.resting).length : 0

  const post = async (path, body) => {
    try {
      const next = await rest(path, { method: 'POST', body })
      client.setQueryData(queryKey, old => ({ ...old, providers: next.providers, now: next.now }))
      host.notify({ kind: 'info', message: next.message })
    } catch (error) {
      host.notifyError(error, 'Could not change the key')
    }
  }

  return jsxs(DropdownMenu, {
    onOpenChange: open => { if (open) { setOther(''); query.refetch() } },
    children: [
      jsx(DropdownMenuTrigger, {
        asChild: true,
        children: jsxs('button', {
          type: 'button',
          title: 'API key in use for this provider. Click to switch.',
          className: 'inline-flex items-center gap-1 px-1.5 text-[0.6875rem] text-(--ui-text-tertiary) hover:text-(--ui-text-secondary)',
          children: [
            current ? `${current.name} · ${current.using || 'all keys limited'}` : 'keys',
            limited > 0 && jsx('span', { className: 'text-(--ui-accent)', children: ` (${limited} limited)` })
          ]
        })
      }),
      jsxs(DropdownMenuContent, {
        align: 'end',
        className: 'max-h-[70vh] min-w-64 overflow-y-auto',
        children: [
          shown
            ? jsxs('div', {
                children: [
                  heading(`${shown.name} — ● is the key in use; click one to switch to it`),
                  jsx(KeyRows, { provider: shown, onUse: (provider, label) => post('/use', { provider, label }) }),
                  jsx(DropdownMenuSeparator, {}),
                  jsx(DropdownMenuItem, {
                    onSelect: () => post('/auto', { provider: shown.name, on: !shown.auto }),
                    children: shown.auto ? 'Auto-switch on a limit: on (click to turn off)' : 'Auto-switch on a limit: off (click to turn on)'
                  })
                ]
              })
            : heading('This chat\'s provider has no saved keys'),
          data.providers.length > (shown ? 1 : 0) && jsx(DropdownMenuSeparator, {}),
          data.providers.length > (shown ? 1 : 0) && heading('Other providers'),
          ...data.providers
            .filter(p => p !== shown)
            .map(p =>
              jsxs(DropdownMenuItem, {
                // Keep the menu open and show that provider's keys instead.
                onSelect: event => { event.preventDefault(); setOther(p.name) },
                className: 'flex items-center gap-2',
                children: [
                  jsx('span', { className: 'min-w-0 flex-1 truncate', children: p.name }),
                  jsx('span', {
                    className: 'shrink-0 pl-4 text-[0.6875rem] text-(--ui-text-tertiary)',
                    children: `${p.using || 'all limited'} · ${p.keys.length} key${p.keys.length === 1 ? '' : 's'}`
                  })
                ]
              }, p.name)
            )
        ]
      })
    ]
  })
}

export default {
  id: ID,
  name: 'Provider Keys',
  register(ctx) {
    rest = ctx.rest
    ctx.register({ id: 'menu', area: 'statusBar.right', order: 104, render: () => jsx(KeyMenu, {}) })
  }
}
