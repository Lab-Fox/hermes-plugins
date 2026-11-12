// Word suggestion engine: pure functions, no DOM, no imports. `build.py` copies
// this file into plugin.js (minus the `export` keywords); the tests import it as is.

export const MAX_LEARNED = 2000
const LEARN_AFTER = 2 // a word typed once may be a typo; twice is vocabulary

/** `list` is a space-separated word list, most frequent first. */
export function createEngine(list, learnedCounts = {}) {
  const words = list.split(' ')
  const rank = new Map(words.map((w, i) => [w, i]))
  const learned = new Map(Object.entries(learnedCounts)) // exact spelling -> times sent

  const learnedKnown = lower => {
    for (const [word, count] of learned) if (count >= LEARN_AFTER && word.toLowerCase() === lower) return true
    return false
  }
  const known = word => rank.has(word.toLowerCase()) || learnedKnown(word.toLowerCase())

  /** Up to `limit` completions for the word being typed at the end of `draft`. */
  function complete(draft, limit = 3) {
    const prefix = trailingWord(draft)
    if (!prefix || prefix.length < 2 || isCommandDraft(draft)) return { prefix: '', items: [] }
    const lower = prefix.toLowerCase()
    const items = []
    const seen = new Set()
    const add = word => {
      const key = word.toLowerCase()
      if (key !== lower && !seen.has(key) && items.length < limit) {
        seen.add(key)
        items.push(word)
      }
    }
    // Your own words first, most used first; they keep the spelling you gave them.
    ;[...learned]
      .filter(([word, count]) => count >= LEARN_AFTER && word.toLowerCase().startsWith(lower))
      .sort((a, b) => b[1] - a[1])
      .forEach(([word]) => add(word))
    // The dictionary only completes plain words: never a path, a number or code.
    if (/^[A-Za-z']+$/.test(prefix)) {
      for (let i = 0; i < words.length && items.length < limit; i++) {
        if (words[i].startsWith(lower)) add(matchCase(prefix, words[i]))
      }
    }
    return { prefix, items }
  }

  /** Spelling fixes for one word, best first. Empty when the word is known or not prose. */
  function corrections(word, limit = 5) {
    if (!word || !/^[A-Za-z']{3,30}$/.test(word) || known(word)) return []
    const lower = word.toLowerCase()
    const reach = lower.length <= 4 ? 1 : 2
    const found = []
    for (let i = 0; i < words.length; i++) {
      const candidate = words[i]
      if (Math.abs(candidate.length - lower.length) > reach) continue
      const distance = editDistance(lower, candidate, reach)
      if (distance <= reach) found.push([distance, i, candidate])
    }
    // A common word two edits away beats a rare word one edit away only on ties.
    found.sort((a, b) => a[0] - b[0] || a[1] - b[1])
    return found.slice(0, limit).map(([, , candidate]) => matchCase(word, candidate))
  }

  /** Count the words of a sent message. Returns true when something changed. */
  function learn(text) {
    let changed = false
    for (const token of learnable(text)) {
      if (rank.has(token.toLowerCase())) continue // already in the dictionary
      learned.set(token, (learned.get(token) || 0) + 1)
      changed = true
    }
    if (learned.size > MAX_LEARNED) {
      const keep = [...learned].sort((a, b) => b[1] - a[1]).slice(0, MAX_LEARNED)
      learned.clear()
      keep.forEach(([word, count]) => learned.set(word, count))
    }
    return changed
  }

  /** Make one word known at once (the "Add to my words" row). */
  function add(word) {
    learned.set(word, Math.max(learned.get(word) || 0, LEARN_AFTER))
  }

  return {
    complete,
    corrections,
    learn,
    add,
    known,
    forget: () => learned.clear(),
    learnedCounts: () => Object.fromEntries(learned),
    learnedSize: () => [...learned.values()].filter(count => count >= LEARN_AFTER).length
  }
}

/** The unfinished word at the very end of the draft ('' after a space or newline). */
export function trailingWord(draft) {
  const match = /(?:^|[\s(\["“])([A-Za-z](?:[A-Za-z0-9'._-]*[A-Za-z0-9])?)$/.exec(draft || '')
  return match ? match[1] : ''
}

/** Slash commands and @-references have their own completion in Hermes. */
export function isCommandDraft(draft) {
  return /^\s*\//.test(draft || '') || /(?:^|\s)@\S*$/.test(draft || '')
}

/** The draft with its trailing word replaced by `word` and a space. */
export function applyCompletion(draft, prefix, word) {
  return `${draft.slice(0, draft.length - prefix.length)}${word} `
}

/** Tokens worth remembering: names and terms, not paths, links, numbers or code. */
export function learnable(text) {
  const out = []
  // Fenced and inline code is not vocabulary.
  const prose = String(text || '').replace(/```[\s\S]*?```/g, ' ').replace(/`[^`]*`/g, ' ')
  for (const raw of prose.split(/\s+/)) {
    const token = raw.replace(/^[^A-Za-z]+|[^A-Za-z0-9]+$/g, '')
    if (token.length < 4 || token.length > 40) continue
    if (!/^[A-Za-z][A-Za-z0-9'._-]*$/.test(token)) continue // drops paths, URLs, a=b, f(x)
    if (/^(https?|www)\b/i.test(token)) continue
    out.push(token)
  }
  return out
}

function matchCase(typed, word) {
  if (typed.length > 1 && typed === typed.toUpperCase() && /[A-Z]/.test(typed)) return word.toUpperCase()
  if (typed[0] !== typed[0].toLowerCase()) return word[0].toUpperCase() + word.slice(1)
  return word
}

/** Damerau-Levenshtein distance, giving up (returning max + 1) once it exceeds `max`. */
export function editDistance(a, b, max) {
  const rows = a.length + 1
  const cols = b.length + 1
  let before = null
  let prev = Array.from({ length: cols }, (_, j) => j)
  for (let i = 1; i < rows; i++) {
    const row = [i]
    let best = i
    for (let j = 1; j < cols; j++) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1
      let value = Math.min(prev[j] + 1, row[j - 1] + 1, prev[j - 1] + cost)
      if (before && i > 1 && j > 1 && a[i - 1] === b[j - 2] && a[i - 2] === b[j - 1]) {
        value = Math.min(value, before[j - 2] + 1) // swapped neighbours: "teh"
      }
      row.push(value)
      if (value < best) best = value
    }
    if (best > max) return max + 1
    before = prev
    prev = row
  }
  return prev[cols - 1]
}
