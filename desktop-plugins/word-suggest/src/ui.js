// Desktop half. `build.py` puts the imports, the word list and engine.mjs above this.

const ID = 'word-suggest'
const EMPTY = { prefix: '', items: [], draft: '' }
let state = EMPTY
let popup = null // one floating element on <body>: it must never push the message box around
const clear = () => { state = EMPTY; if (popup) popup.style.display = 'none' }

let engine = null
let storage = null
let enabled = true

const saveLearned = () => storage.set('learned', engine.learnedCounts())

/** The message box in focus, or null: suggestions never fire in Settings fields or dialogs. */
function focusedEditor() {
  const el = document.activeElement
  return el && el.isContentEditable ? el : null
}

/** True when the caret sits at the very end of the box with nothing selected. Completion
 *  rewrites the end of the draft, so it must not run while the user edits the middle. */
function caretAtEnd(editor) {
  const selection = window.getSelection()
  if (!selection || !selection.isCollapsed || !selection.focusNode || !editor.contains(selection.focusNode)) return false
  const rest = document.createRange()
  rest.selectNodeContents(editor)
  rest.setStart(selection.focusNode, selection.focusOffset)
  return rest.toString().trim() === ''
}

async function refresh() {
  const editor = focusedEditor()
  if (!enabled || !editor) return clear()
  quietSystemSuggestions(editor)
  if (!caretAtEnd(editor)) return clear()
  const draft = await host.composer.getDraft(null)
  if (typeof draft !== 'string') return clear()
  const { prefix, items } = engine.complete(draft)
  if (!items.length) return clear()
  state = { prefix, items, draft }
  show(editor)
}

/** Where the caret is on screen; the box's own corner when the caret has no rectangle yet. */
function caretRect(editor) {
  const selection = window.getSelection()
  if (selection && selection.rangeCount) {
    const rect = selection.getRangeAt(0).getClientRects()[0]
    if (rect && (rect.top || rect.left)) return rect
  }
  const box = editor.getBoundingClientRect()
  return { left: box.left + 12, top: box.top, bottom: box.top + 20 }
}

function show(editor) {
  if (!popup) {
    popup = document.createElement('div')
    popup.setAttribute('data-word-suggest', '')
    popup.style.cssText =
      'position:fixed;z-index:2147483000;display:none;gap:2px;padding:3px;border-radius:8px;font-size:12px;' +
      'white-space:nowrap;background:var(--dt-background,Canvas);color:var(--ui-text-secondary,CanvasText);' +
      'border:1px solid var(--ui-stroke-secondary,GrayText);box-shadow:0 4px 14px rgb(0 0 0 / 0.35)'
    // mousedown, not click: the caret stays in the message box.
    popup.addEventListener('mousedown', event => {
      event.preventDefault()
      const index = event.target.closest?.('[data-index]')?.getAttribute('data-index')
      if (index != null) void accept(Number(index))
    })
    document.body.appendChild(popup)
  }
  popup.replaceChildren(
    ...state.items.map((word, index) => {
      const item = document.createElement('span')
      item.setAttribute('data-index', String(index))
      item.style.cssText = 'padding:3px 9px;border-radius:5px;cursor:pointer'
      item.textContent = word
      if (index === 0) {
        item.style.background = 'color-mix(in srgb, var(--ui-accent, Highlight) 22%, transparent)'
        const hint = document.createElement('span')
        hint.textContent = 'Tab'
        hint.style.cssText = 'margin-left:7px;opacity:0.55;font-size:10px'
        item.appendChild(hint)
      }
      return item
    })
  )
  popup.style.display = 'flex'
  // Above the line being typed, like the Windows popup; below it when there is no room.
  const caret = caretRect(editor)
  const size = popup.getBoundingClientRect()
  const top = caret.top - size.height - 8 >= 4 ? caret.top - size.height - 8 : caret.bottom + 8
  popup.style.top = `${Math.round(top)}px`
  popup.style.left = `${Math.round(Math.max(4, Math.min(caret.left, window.innerWidth - size.width - 4)))}px`
}

// Windows draws its own suggestion popup, which cannot insert into this box, and it has no
// per-app switch. What it does respect is the KIND of field: it stays out of address, email
// and number fields. `inputmode` tells Windows which kind this box is; on a physical keyboard
// it changes nothing else. Which kind silences the popup is found by trying (Settings).
const BLOCK_MODES = ['url', 'email', 'numeric', 'off']
let blockMode = 'url'
const QUIET_NAMES = ['inputmode', 'autocomplete', 'autocorrect']
const quieted = new Map() // editor -> the attribute values it had before
function quietSystemSuggestions(editor) {
  if (!quieted.has(editor)) quieted.set(editor, Object.fromEntries(QUIET_NAMES.map(name => [name, editor.getAttribute(name)])))
  const want = blockMode === 'off' ? quieted.get(editor) : { inputmode: blockMode, autocomplete: 'off', autocorrect: 'off' }
  for (const name of QUIET_NAMES) {
    if (want[name] == null) editor.removeAttribute(name)
    else if (editor.getAttribute(name) !== want[name]) editor.setAttribute(name, want[name])
  }
}
function restoreSystemSuggestions() {
  for (const [editor, before] of quieted) {
    for (const [name, value] of Object.entries(before)) {
      if (value == null) editor.removeAttribute(name)
      else editor.setAttribute(name, value)
    }
  }
  quieted.clear()
}

async function accept(index) {
  const { prefix, items, draft } = state
  const word = items[index]
  if (!word) return
  clear()
  // Re-read: a keystroke may have landed since the strip was drawn.
  const live = await host.composer.getDraft(null)
  if (live !== draft) return
  const done = await host.composer.setDraft(null, applyCompletion(draft, prefix, word))
  if (done) host.composer.focus(null)
}

function Settings() {
  const [on, setOn] = useState(enabled)
  const [count, setCount] = useState(engine.learnedSize())
  const [mode, setMode] = useState(blockMode)
  return jsxs('div', {
    className: 'flex flex-col gap-4 p-4 text-sm',
    children: [
      jsxs('label', {
        className: 'flex items-center gap-3',
        children: [
          jsx(Switch, { checked: on, onCheckedChange: value => { enabled = value; storage.set('enabled', value); setOn(value); if (!value) clear() } }),
          'Suggest words at the cursor while typing (Tab accepts the first)'
        ]
      }),
      jsxs('div', {
        className: 'flex items-center gap-3',
        children: [
          jsx('span', { className: 'text-(--ui-text-secondary)', children: `${count} of your own words learned (kept on this PC only).` }),
          jsx(Button, {
            variant: 'outline', size: 'sm', disabled: !count,
            onClick: () => { engine.forget(); saveLearned(); setCount(0) },
            children: 'Forget them'
          })
        ]
      }),
      jsxs('div', {
        className: 'flex flex-col gap-2',
        children: [
          jsx('span', { children: 'Hide the Windows suggestion popup in Hermes only' }),
          jsx('div', {
            className: 'flex gap-2',
            children: BLOCK_MODES.map(name =>
              jsx(Button, {
                variant: name === mode ? 'default' : 'outline', size: 'sm',
                onClick: () => { blockMode = name; storage.set('blockMode', name); setMode(name) },
                children: name === 'off' ? 'Off' : `Method: ${name}`
              }, name)
            )
          }),
          jsx('span', {
            className: 'text-xs text-(--ui-text-tertiary)',
            children: 'If the Windows popup still shows, try the next method, then click in the message box and type. Windows settings are not touched.'
          })
        ]
      }),
      jsx('div', {
        className: 'text-xs text-(--ui-text-tertiary)',
        children: 'Right-click a misspelled word in the message box for fixes. Nothing is ever changed unless you pick it.'
      })
    ]
  })
}

export default {
  id: ID,
  name: 'Word Suggest',
  register(ctx) {
    storage = ctx.storage
    enabled = storage.get('enabled', true) !== false
    blockMode = BLOCK_MODES.includes(storage.get('blockMode', 'url')) ? storage.get('blockMode', 'url') : 'url'
    engine = createEngine(WORDS, storage.get('learned', {}))

    let timer = null
    const schedule = () => {
      if (timer) timer()
      timer = ctx.setTimeout(() => { timer = null; void refresh() }, 70)
    }
    ctx.addEventListener(document, 'input', schedule, true)
    ctx.addEventListener(document, 'selectionchange', schedule)
    ctx.addEventListener(document, 'focusout', clear, true)
    ctx.addEventListener(document, 'keydown', event => {
      if (event.key !== 'Tab' || event.shiftKey || event.ctrlKey || event.altKey || event.metaKey) return
      if (!state.items.length || !focusedEditor()) return
      event.preventDefault()
      event.stopPropagation()
      void accept(0)
    }, true)
    ctx.addEventListener(window, 'resize', clear)
    ctx.addEventListener(document, 'scroll', clear, true)
    ctx.onDispose(() => { restoreSystemSuggestions(); popup?.remove(); popup = null; state = EMPTY })

    ctx.registerMany([
      {
        id: 'learn',
        area: 'composer.middleware',
        // Passes every message through untouched; it only counts the words you used.
        data: { handler: draft => { try { if (engine.learn(draft.text)) saveLearned() } catch {} clear(); return draft } }
      },
      {
        id: 'spelling',
        area: 'composer.contextMenu',
        data: {
          items: ({ word }) => {
            if (!enabled || !word) return []
            const fixes = engine.corrections(word)
            if (!fixes.length && engine.known(word)) return []
            return [
              ...fixes.map(fix => ({ label: fix, onSelect: action => action.replaceWord(fix) })),
              { label: `Add "${word.slice(0, 40)}" to my words`, onSelect: () => { engine.add(word); saveLearned() } }
            ].slice(0, 6)
          }
        }
      }
    ])
    ctx.registerSettingsPage?.({ id: 'settings', title: 'Word Suggest', icon: 'whole-word', render: () => jsx(Settings, {}) })
  }
}
