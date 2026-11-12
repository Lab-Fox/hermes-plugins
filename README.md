# Hermes plugins

Plugins for [Hermes Agent](https://github.com/NousResearch/hermes-agent) and Hermes Desktop.
Built and used on Windows 11 with Hermes Desktop.

| Plugin | What it does | Folders |
|---|---|---|
| **Provider Keys** | Several API keys per model provider, a key menu in the Desktop status bar, and an automatic move to the next key when one runs out | `plugins/provider-keys`, `plugins/model-providers/free-providers` |
| **NanoGPT Accounts** | Named NanoGPT keys (for example subscription and Pay As You Go) switched by hand | `plugins/nanogpt-accounts`, `plugins/model-providers/nanogpt` |
| **OpenCode Accounts** | Named OpenCode Console keys switched by hand, used by both OpenCode Zen and Go | `plugins/opencode-accounts`, `plugins/model-providers/opencode-accounts` |
| **Session Context** | A tokens-and-cost chip and a Context tab for the open chat: provider, model, token breakdown, cost, message counts, system prompt, raw messages | `plugins/session-context` |
| **Word Suggest** | Word completion at the cursor, spelling fixes on right-click and learning of your own terms, in the Desktop message box | `desktop-plugins/word-suggest` |
| **Resourceful** | Gets the agent to use what it has: names the installed skills, tools and connectors that fit each message, and suggests a web search when it keeps failing the same way | `plugins/resourceful` |
| **NetCrawler** | Keyless web search across several engines, a fetch ladder that falls back to a real browser for pages that need it, site crawling, structured extraction and a local searchable corpus | `plugins/netcrawler` |
| **NanoGPT Image** | Image generation and image-to-image through NanoGPT's image API | `plugins/nanogpt-image` |

## Install

The folders mirror the Hermes home folder (`%LOCALAPPDATA%\hermes` on Windows, `~/.hermes`
elsewhere, or wherever `HERMES_HOME` points). Copy the ones you want to the same place there.

On Windows, from a clone of this repository:

```powershell
.\install.ps1
```

Then enable the plugins that have a command or a backend, and restart the gateway:

```bash
hermes plugins enable provider-keys
hermes plugins enable session-context
hermes plugins enable nanogpt-accounts
hermes plugins enable opencode-accounts
hermes plugins enable resourceful
hermes plugins enable netcrawler
hermes plugins enable nanogpt-image
hermes gateway restart
```

NetCrawler needs the Python packages listed in `plugins/netcrawler/pyproject.toml` (curl_cffi,
trafilatura, Patchright and a few more). `hermes plugins doctor netcrawler` checks that it imports.

The folders under `plugins/model-providers/` need no enabling: Hermes finds them at start.
Desktop parts start switched off. Turn them on in Hermes Desktop under **Capabilities → Plugins**
(Provider Keys, Session Context, Word Suggest).

## Provider Keys

Keys are saved under labels and stored in the Hermes `.env` file. They are never typed into a chat
and never returned by the plugin's routes.

```bash
hermes keys providers                 # providers with switchable keys
hermes keys add groq main             # prompts for the key, hidden
hermes keys list
hermes keys use groq main             # start with this key
hermes keys auto groq off             # never change key by itself for this provider
hermes keys skip groq paid on         # keep one key out of automatic switching
hermes keys models all                # show paid models in model lists too (default: free only)
```

In a chat: `/keys`, `/keys use <provider> <label>`, `/keys auto ...`, `/keys skip ...`,
`/keys models all|free`.

In Hermes Desktop the status bar shows the key in use, for example `tokenharbor · main`.
Click it to switch key, see which keys are limited and until when, or turn automatic switching
on or off.

**Automatic switching.** Hermes's own credential pool does the switching; the plugin feeds it the
saved keys in order. Hermes treats every HTTP 429 as a brief rate limit and waits (up to ten
minutes) before trying the next key. `limits.py` recognises "allowance used up", "quota exceeded",
"daily limit" and similar wording and makes Hermes move on at once, while per-minute throttles keep
Hermes's normal short wait.

**Providers.** Adds ModelScope, Chutes, LLM7, OrcaRouter, Token Harbor, APInex, BeatAPI, Groq,
Cerebras, Mistral, SambaNova, SiliconFlow, Gonka Broker, Aion Labs, Agnes AI, Nscale and Cohere as
OpenAI-compatible providers, and makes keys switchable for the built-in NVIDIA, OpenRouter,
Kilo Code, Gemini, Hugging Face and Ollama Cloud providers. Free offers change often; check each
provider's site.

> **Read the provider's terms first.** Most providers allow one free account per person. Rotating
> several free accounts on one provider can get all of them banned. Use multiple keys where the
> provider permits it, for example a free key and a paid key, or keys from separate plans.

## NanoGPT Accounts and OpenCode Accounts

```bash
hermes nanogpt            # /nanogpt in a chat: list keys, `use <label>`, `status`
hermes opencode           # /opencode in a chat: list accounts, `use <label>`
```

Switching is always manual in these two.

## Session Context

Adds a chip to the status bar (`126k · $0.09`) and a ring to the title bar; either opens a
**Context** tab for the chat in focus. Totals cover the whole chat, including the parts before a
compression. The message list loads a message's text only when you expand it. The plugin reads
Hermes's session database and writes nothing.

## Word Suggest

Type two or more letters at the end of a message: up to three completions appear at the cursor.
Tab accepts the first; a click accepts any. Right-click a misspelled word for fixes. A term you
send twice is learned and suggested first (stored on your machine only). Nothing is changed unless
you pick it, and slash commands, `@` references, paths and code are left alone.

Windows shows its own suggestion popup, which cannot insert into the message box. The plugin's
settings (**Settings → Plugins → Word Suggest**) have three methods to try for hiding it in Hermes
only; whether one works depends on Windows.

To change the plugin, edit the files in `desktop-plugins/word-suggest/src/` and run
`python src/build.py`, which writes `plugin.js`.

## Resourceful

Models often grind on a problem that a quick search or an installed skill would solve, and miss
tools that Hermes keeps behind `tool_search`. This plugin works at three points:

- **Standing rules** in each new chat's instructions: search when stuck, reuse existing libraries,
  check and combine installed skills and tools, look behind `tool_search`.
- **Per message:** names the installed skills and tools that match what you asked, points at
  connectors when you name an app (GitHub, Gmail, Notion page, ...), and suggests searching when the
  message carries a pasted error or a "how do I / is there a library" question.
- **When stuck:** after the same failure twice, adds a note to that tool result suggesting a search
  for the exact error.

It stays quiet for refusals (commands you denied, guard blocks, pending approvals), Hermes timeouts,
expected non-zero exits (`grep` with no match, `diff`) and ordinary failing tests. Error text copied
into a note is cleaned so it cannot carry instructions. Matching is local: no model calls, no
network, nothing stored. The rules section applies from the next new chat after enabling.

## NetCrawler

Tools: `net_search`, `net_fetch`, `net_crawl`, `net_extract`, `net_corpus`, `net_status`. Search
needs no API key. Fetching starts with a fast HTTP request and moves to a real browser (an installed
Chrome or Edge, via Patchright) when a page needs JavaScript or shows a consent or age gate. It
follows robots.txt and keeps a per-site delay by default (both in the plugin settings). Respect each
site's terms of use.

## NanoGPT Image

Set `NANOGPT_API_KEY` (or a separate `NANOGPT_IMAGE_API_KEY`, so images can use a subscription's
image quota) and choose `nanogpt` as the image provider (`hermes tools`). The model comes from
`NANOGPT_IMAGE_MODEL` or `image_gen.model`; any NanoGPT image model id works. It uses a helper
module inside Hermes (`plugins.image_gen._common`) that is not part of the published plugin API, so
a Hermes update could require a change.

## Tests

```bash
cd plugins/provider-keys     && python -m unittest discover -s tests
cd plugins/nanogpt-accounts  && python -m unittest discover -s tests
cd plugins/opencode-accounts && python -m unittest discover -s tests
cd desktop-plugins/word-suggest && node --test src/
cd plugins/resourceful       && python -m unittest discover -s tests
cd plugins/netcrawler        && python -m pytest tests/test_core.py
```

`plugins/session-context/tests` runs only from an installed copy, with Hermes's own Python environment (it needs FastAPI and `hermes_state`).

## Status

The Python logic and the Word Suggest engine have tests. The Desktop interfaces were written
against the Hermes Desktop plugin SDK and are used by the author, but have no automated
tests. Automatic key switching is tested against Hermes's recovery code with fake keys.

## License

MIT, see [LICENSE](LICENSE). The word list in `desktop-plugins/word-suggest` (`src/words.txt`, also
embedded in `plugin.js`) is the 36,000 most frequent English words from
[FrequencyWords](https://github.com/hermitdave/FrequencyWords) by Hermit Dave, built from
OpenSubtitles data and licensed [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/).
