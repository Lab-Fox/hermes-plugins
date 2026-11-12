// Run:  node --test src/
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'

import { applyCompletion, createEngine, editDistance, isCommandDraft, learnable, trailingWord } from './engine.mjs'

const WORDS = readFileSync(new URL('./words.txt', import.meta.url), 'utf8').trim()
const engine = () => createEngine(WORDS)

test('completes the word being typed, most common first', () => {
  const { prefix, items } = engine().complete('please expl')
  assert.equal(prefix, 'expl')
  assert.equal(items.length, 3)
  assert.ok(items.includes('explain'), items.join())
  assert.ok(items.every(w => w.startsWith('expl') && w !== 'expl'))
})

test('keeps the capital you typed', () => {
  assert.ok(engine().complete('Becau').items.includes('Because'))
})

test('offers nothing after a space, for one letter, in commands or @-references', () => {
  const e = engine()
  assert.deepEqual(e.complete('hello ').items, [])
  assert.deepEqual(e.complete('h').items, [])
  assert.deepEqual(e.complete('/keys mod').items, [])
  assert.deepEqual(e.complete('look at @fil').items, [])
})

test('the dictionary never completes paths, numbers or dotted names', () => {
  const e = engine()
  assert.deepEqual(e.complete('open src/app').items, [])
  assert.deepEqual(e.complete('version v4.1').items, [])
})

test('accepting replaces only the unfinished word and adds a space', () => {
  assert.equal(applyCompletion('please expl', 'expl', 'explain'), 'please explain ')
})

test('fixes common typos and leaves known words alone', () => {
  const e = engine()
  assert.equal(e.corrections('teh')[0], 'the')
  assert.equal(e.corrections('recieve')[0], 'receive')
  assert.equal(e.corrections('Becuase')[0], 'Because')
  assert.deepEqual(e.corrections('because'), [])
  assert.deepEqual(e.corrections('v4.1'), [])
})

test('learns your own terms after the second use and suggests them first', () => {
  const e = engine()
  e.learn('switch to deepseek-v4.1-flash please')
  assert.deepEqual(e.complete('use deeps').items.filter(w => w.startsWith('deepseek')), [])
  e.learn('deepseek-v4.1-flash again')
  assert.equal(e.complete('use deeps').items[0], 'deepseek-v4.1-flash')
  assert.ok(e.known('deepseek-v4.1-flash'))
})

test('learning skips dictionary words, code, paths and links', () => {
  assert.deepEqual(learnable('run `npm install` in C:\\Users\\x and see https://example.com/a, then NanoGPT.'), ['then', 'NanoGPT'])
  const e = engine()
  assert.equal(e.learn('then because'), false)
})

test('add makes a word known at once; forget clears everything learned', () => {
  const e = engine()
  e.add('Hermes')
  assert.ok(e.known('hermes'))
  assert.deepEqual(e.corrections('Hermes'), [])
  e.forget()
  assert.equal(e.learnedSize(), 0)
})

test('helpers', () => {
  assert.equal(trailingWord('a (wor'), 'wor')
  assert.equal(trailingWord('done.'), '')
  assert.ok(isCommandDraft('  /model'))
  assert.equal(editDistance('teh', 'the', 2), 1)
  assert.equal(editDistance('abcdef', 'uvwxyz', 2), 3)
})
