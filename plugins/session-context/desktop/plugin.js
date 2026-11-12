// Session Context — a usage chip and a Context tab for the chat in focus.
// Disk plugin: plain ESM, no JSX, only these three imports resolve.
import { host, useValue, useQuery, ScrollArea, CopyButton } from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'session-context'
let rest = null // ctx.rest, set in register(): the door to dashboard/plugin_api.py

// ── formatting ───────────────────────────────────────────────────────────────
const num = n => Number(n || 0).toLocaleString('en-US')
const compact = n => {
  const v = Number(n || 0)
  if (v >= 1e6) return `${(v / 1e6).toFixed(v >= 1e7 ? 0 : 1)}M`
  if (v >= 1e3) return `${Math.round(v / 1e3)}k`
  return String(v)
}
const money = usd => {
  const v = Number(usd || 0)
  return `$${v > 0 && v < 0.01 ? v.toFixed(4) : v.toFixed(2)}`
}
const when = seconds =>
  seconds
    ? new Date(seconds * 1000).toLocaleString(undefined, {
        month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit'
      })
    : '—'

// ── shared pieces ────────────────────────────────────────────────────────────
function Ring({ percent, size = 14 }) {
  const r = 5.5
  const full = 2 * Math.PI * r
  const used = Math.max(0, Math.min(100, Number(percent || 0)))
  const circle = extra =>
    jsx('circle', { cx: 8, cy: 8, r, fill: 'none', stroke: 'currentColor', strokeWidth: 2, ...extra })
  return jsxs('svg', {
    width: size, height: size, viewBox: '0 0 16 16', 'aria-hidden': true,
    children: [
      circle({ opacity: 0.3 }),
      used > 0 &&
        circle({ strokeDasharray: `${(used / 100) * full} ${full}`, strokeLinecap: 'round', transform: 'rotate(-90 8 8)' })
    ]
  })
}

function openContext() {
  if (typeof host.openWorkspace !== 'function') {
    host.notify({ kind: 'info', message: 'This Hermes Desktop version cannot open plugin tabs. Update Hermes Desktop.' })
    return
  }
  host.openWorkspace(ID, { title: 'Context', minWidth: 420, render: () => jsx(ContextView, {}) })
}

// Title bar: the ring only (title-bar buttons are icon sized).
function TitlebarRing() {
  const usage = useValue(host.state.focusedUsage)
  return jsx(Ring, { percent: usage?.context_percent, size: 15 })
}

// Status bar: "126k · $0.09", click opens the Context tab.
function Chip() {
  const usage = useValue(host.state.focusedUsage)
  const sid = useValue(host.state.focusedStoredSessionId)
  if (!usage && !sid) return null
  const total = usage?.total ?? 0
  const pct = usage?.context_percent
  return jsxs('button', {
    type: 'button',
    title: `Session tokens and cost${pct != null ? ` — context window ${pct}% full` : ''}. Click for details.`,
    className:
      'inline-flex items-center gap-1.5 px-1.5 text-[0.6875rem] tabular-nums text-(--ui-text-tertiary) hover:text-(--ui-text-secondary)',
    onClick: openContext,
    children: [jsx(Ring, { percent: pct, size: 12 }), `${compact(total)} · ${money(usage?.cost_usd)}`]
  })
}

// ── Context tab ──────────────────────────────────────────────────────────────
function Field({ label, value, hint }) {
  return jsxs('div', {
    className: 'min-w-0',
    title: hint,
    children: [
      jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: label }),
      jsx('div', { className: 'mt-1 truncate text-sm font-medium tabular-nums', children: value })
    ]
  })
}

function Section({ title, right, children }) {
  return jsxs('div', {
    className: 'mt-8',
    children: [
      jsxs('div', {
        className: 'mb-2 flex items-center justify-between',
        children: [jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: title }), right]
      }),
      children
    ]
  })
}

const boxClass = 'rounded-md border border-(--ui-stroke-secondary)'
const preClass = 'whitespace-pre-wrap break-words font-mono text-xs leading-relaxed'

function SystemPrompt({ text }) {
  const [open, setOpen] = useState(false)
  if (!text) return jsx('div', { className: `${boxClass} p-3 text-sm text-(--ui-text-tertiary)`, children: 'Not stored for this session.' })
  return jsxs('div', {
    className: `${boxClass} p-3`,
    children: [
      jsx('div', { className: `${preClass} ${open ? '' : 'max-h-24 overflow-hidden'}`, children: text }),
      text.length > 400 &&
        jsx('button', {
          type: 'button',
          className: 'mt-2 text-xs text-(--ui-accent)',
          onClick: () => setOpen(!open),
          children: open ? 'Show less' : `Show all (${num(text.length)} characters)`
        })
    ]
  })
}

function MessageRow({ sessionId, message }) {
  const [open, setOpen] = useState(false)
  const detail = useQuery({
    queryKey: [ID, 'message', sessionId, message.id],
    queryFn: () => rest(`/message?session=${encodeURIComponent(sessionId)}&id=${message.id}`),
    enabled: open,
    staleTime: Infinity
  })
  const body = detail.data
  const text = body ? [body.reasoning && `[reasoning]\n${body.reasoning}`, body.content,
    body.tool_calls && `[tool calls]\n${typeof body.tool_calls === 'string' ? body.tool_calls : JSON.stringify(body.tool_calls, null, 2)}`]
    .filter(Boolean).join('\n\n') : ''
  return jsxs('div', {
    className: 'border-b border-(--ui-stroke-secondary) last:border-b-0',
    children: [
      jsxs('button', {
        type: 'button',
        className: 'flex w-full items-center gap-2 px-3 py-2 text-left text-xs hover:opacity-80',
        onClick: () => setOpen(!open),
        children: [
          jsx('span', { className: 'font-medium', children: message.role }),
          jsx('span', {
            className: 'min-w-0 flex-1 truncate text-(--ui-text-tertiary)',
            children: `• #${message.id}${message.tool_name ? ` • ${message.tool_name}` : ''} • ${num(message.chars)} chars`
          }),
          jsx('span', { className: 'shrink-0 text-(--ui-text-tertiary)', children: when(message.timestamp) }),
          jsx('span', { className: 'shrink-0 text-(--ui-text-tertiary)', children: open ? '▾' : '▸' })
        ]
      }),
      open &&
        jsx('div', {
          className: 'px-3 pb-3',
          children: detail.isLoading
            ? jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: 'Loading…' })
            : detail.error
              ? jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: `Could not load: ${detail.error.message || detail.error}` })
              : jsxs('div', {
                  children: [
                    jsx('div', { className: `${preClass} max-h-96 overflow-auto`, children: text || '(empty)' }),
                    text && jsx('div', { className: 'mt-2', children: jsx(CopyButton, { text }) })
                  ]
                })
        })
    ]
  })
}

function ContextView() {
  const sid = useValue(host.state.focusedStoredSessionId)
  const usage = useValue(host.state.focusedUsage)
  const busy = useValue(host.state.busy)
  const query = useQuery({
    queryKey: [ID, 'session', sid],
    queryFn: () => rest(`/session?id=${encodeURIComponent(sid)}`),
    enabled: !!sid,
    refetchInterval: 20000
  })
  // A finished turn changes every number here: refresh as soon as it ends.
  useEffect(() => {
    if (!busy && sid) query.refetch()
  }, [busy, sid])

  const note = text => jsx('div', { className: 'p-6 text-sm text-(--ui-text-tertiary)', children: text })
  if (!sid) return note('Open a chat to see its context. A brand-new chat appears here after its first message.')
  if (query.isLoading) return note('Loading…')
  if (query.error)
    return note(
      `Could not load this session (${query.error.message || query.error}). ` +
        'If you just installed the plugin, enable "session-context" for this profile and restart Hermes.'
    )

  const d = query.data
  const t = d.tokens
  const limit = usage?.context_max
  const fields = [
    ['Session', d.session.title || 'Untitled', d.session.id],
    ['Messages', num(d.counts.total)],
    ['Provider', d.session.provider || '—'],
    ['Model', d.session.model || '—'],
    ['Context Limit', limit ? num(limit) : '—', 'The model\'s context window, reported while the chat is live'],
    ['Total Tokens', num(t.total), 'Input + output + cache read + cache write, over the whole chat'],
    ['Usage', usage?.context_percent != null ? `${usage.context_percent}%${usage.context_used ? ` (${num(usage.context_used)})` : ''}` : '—',
      'How full the context window is right now'],
    ['Input Tokens', num(t.input), 'Prompt tokens that were not served from cache'],
    ['Output Tokens', num(t.output)],
    ['Reasoning Tokens', num(t.reasoning)],
    ['Cache Tokens (read/write)', `${num(t.cache_read)} / ${num(t.cache_write)}`],
    ['User Messages', num(d.counts.user)],
    ['Assistant Messages', num(d.counts.assistant)],
    ['Total Cost', `${money(d.cost.usd)}${d.cost.billed ? '' : d.cost.usd ? ' (estimate)' : ''}`,
      d.cost.billed ? 'Reported by the provider' : 'Hermes\'s estimate from the model\'s listed price'],
    ['API Calls', num(d.api_calls)],
    ['Tool Results', num(d.counts.tool)],
    ['Session Created', when(d.session.created_at)],
    ['Last Activity', when(d.session.last_activity_at)]
  ]

  return jsx(ScrollArea, {
    className: 'h-full',
    children: jsxs('div', {
      className: 'mx-auto max-w-5xl p-6',
      children: [
        jsx('div', {
          className: 'grid grid-cols-2 gap-x-8 gap-y-5',
          children: fields.map(([label, value, hint]) => jsx(Field, { label, value, hint }, label))
        }),
        d.session.segments > 1 &&
          jsx('div', {
            className: 'mt-4 text-xs text-(--ui-text-tertiary)',
            children: `This chat was compressed ${d.session.segments - 1} time(s); totals cover all of it.`
          }),
        d.models.length > 1 &&
          jsx(Section, {
            title: 'By model',
            children: jsx('div', {
              className: boxClass,
              children: d.models.map((m, i) =>
                jsxs('div', {
                  className: 'flex items-center gap-3 border-b border-(--ui-stroke-secondary) px-3 py-2 text-xs last:border-b-0',
                  children: [
                    jsx('span', { className: 'min-w-0 flex-1 truncate font-medium', children: `${m.model}${m.task ? ` (${m.task})` : ''}` }),
                    jsx('span', { className: 'text-(--ui-text-tertiary)', children: m.provider }),
                    jsx('span', { className: 'tabular-nums', children: `${num(m.calls)} calls` }),
                    jsx('span', {
                      className: 'tabular-nums',
                      children: `${compact(m.tokens.input + m.tokens.output + m.tokens.cache_read + m.tokens.cache_write)} tokens`
                    }),
                    jsx('span', { className: 'tabular-nums', children: money(m.cost_usd) })
                  ]
                }, i)
              )
            })
          }),
        jsx(Section, { title: 'System Prompt', children: jsx(SystemPrompt, { text: d.system_prompt }) }),
        jsx(Section, {
          title: 'Raw messages',
          right: jsx('span', { className: 'text-xs text-(--ui-text-tertiary)', children: `${num(d.messages.length)} stored` }),
          children: jsx('div', {
            className: boxClass,
            children: d.messages.length
              ? d.messages.map(m => jsx(MessageRow, { sessionId: sid, message: m }, `${m.session_id}:${m.id}`))
              : jsx('div', { className: 'p-3 text-sm text-(--ui-text-tertiary)', children: 'No messages stored yet.' })
          })
        })
      ]
    })
  })
}

export default {
  id: ID,
  name: 'Session Context',
  register(ctx) {
    rest = ctx.rest
    ctx.registerMany([
      { id: 'chip', area: 'statusBar.right', order: 105, render: () => jsx(Chip, {}) },
      {
        id: 'ring',
        area: 'titleBar.right',
        order: 10,
        data: { id: `${ID}.open`, label: 'Session context', icon: jsx(TitlebarRing, {}), onSelect: openContext }
      },
      {
        id: 'open',
        area: 'palette',
        data: { id: `${ID}.open`, label: 'Open Session Context', keywords: ['context', 'tokens', 'cost', 'usage'], run: openContext }
      }
    ])
  }
}
