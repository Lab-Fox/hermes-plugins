"""Tool schemas — exactly what the model reads."""

NET_SEARCH = {
    "name": "net_search",
    "description": (
        "Search the web with no API key and get back real page content, not just links. "
        "Queries several free engines in parallel (DuckDuckGo, Bing, Yahoo, Google News, "
        "Marginalia, optionally SearXNG/Brave/ddgs) and merges them by reciprocal rank. "
        "By default it also fetches the top results down an anti-bot ladder and returns "
        "their extracted text, so one call answers a research question. "
        "Use this instead of web_search when you need the page contents, when results "
        "must come from several engines, or when a single engine keeps returning nothing."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "limit": {"type": "integer", "description": "Number of ranked results (default 8, max 30)."},
            "fetch": {
                "type": "boolean",
                "description": "Fetch and extract the top results (default true). Set false for links only.",
            },
            "read_count": {
                "type": "integer",
                "description": "How many of the top results to actually fetch (default 3, max 8).",
            },
            "vertical": {
                "type": "string",
                "enum": ["web", "news", "reference", "code", "papers", "discussions"],
                "description": "Restrict or extend the source set: news, Wikipedia, Stack Overflow, arXiv, Reddit/HN.",
            },
            "engines": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional engine allow-list, e.g. ['ddg-html','bing-rss']. Omit for all.",
            },
            "site": {
                "type": "string",
                "description": "Restrict results to a domain (appended as a site: operator and post-filtered).",
            },
            "use_hermes_search": {
                "type": "boolean",
                "description": "Also include the configured Hermes web_search provider as a voter (default true).",
            },
            "max_chars": {
                "type": "integer",
                "description": "Cap for each fetched page's returned text (default 4000).",
            },
        },
        "required": ["query"],
    },
}

NET_FETCH = {
    "name": "net_fetch",
    "description": (
        "Fetch ONE url and return its clean markdown, metadata and links. "
        "This is the tool for a page a plain fetch refused: it escalates from a "
        "TLS-impersonating HTTP request to a real stealth browser that dismisses "
        "'you must be 18' age gates, cookie/consent walls and newsletter modals, and waits "
        "out JavaScript challenges. It also reads PDFs, and recovers client-rendered data "
        "from embedded state (__NEXT_DATA__ and friends). "
        "Use mode='browser' to force the browser, mode='http' to stay fast, 'auto' (default) to escalate only when needed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Absolute http(s) URL."},
            "mode": {"type": "string", "enum": ["auto", "http", "browser"], "description": "Fetch strategy (default auto)."},
            "headless": {"type": "boolean", "description": "Run the browser headless (default true)."},
            "save": {"type": "boolean", "description": "Store the document in the local corpus (default true)."},
            "query": {"type": "string", "description": "Optional focus query: also returns a pruned 'fit_markdown'."},
            "max_chars": {"type": "integer", "description": "Cap for returned markdown (default 12000)."},
        },
        "required": ["url"],
    },
}

NET_CRAWL = {
    "name": "net_crawl",
    "description": (
        "Crawl a whole site into the local corpus. Discovers URLs from sitemaps, robots.txt "
        "and link following, obeys robots.txt and crawl-delay, throttles per host, and "
        "supports bfs, dfs and best-first traversal (best-first ranks links by relevance to "
        "'query' and stops early once the relevant part of the site is exhausted). "
        "Runs as a background job and returns immediately with a job_id — poll it with "
        "net_status, then search the results with net_corpus."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Seed URL (site root or a section)."},
            "max_pages": {"type": "integer", "description": "Page cap (default 40, max 2000)."},
            "max_depth": {"type": "integer", "description": "Link depth (default 2, max 8)."},
            "strategy": {"type": "string", "enum": ["bfs", "dfs", "best-first"], "description": "Traversal order (default bfs)."},
            "query": {"type": "string", "description": "What you are looking for — drives best-first ranking, fit_markdown and adaptive stopping."},
            "include": {"type": "string", "description": "Regex: only URLs matching this."},
            "exclude": {"type": "string", "description": "Regex: skip URLs matching this."},
            "use_sitemap": {"type": "boolean", "description": "Seed from sitemaps (default true)."},
            "respect_robots": {"type": "boolean", "description": "Obey robots.txt (default true)."},
            "allow_browser": {"type": "boolean", "description": "Allow browser escalation during the crawl (default true)."},
            "allow_subdomains": {"type": "boolean", "description": "Stay within the registrable domain (default true)."},
            "concurrency": {"type": "integer", "description": "Parallel fetches (default 4, max 8)."},
            "deadline_s": {"type": "integer", "description": "Wall-clock budget for the whole crawl (default 600)."},
            "adaptive": {"type": "boolean", "description": "Stop early when pages stop adding new relevant terms (default true)."},
            "wait_s": {"type": "integer", "description": "If >0, block up to this many seconds and return the final summary."},
        },
        "required": ["url"],
    },
}

NET_STATUS = {
    "name": "net_status",
    "description": (
        "Check, list or cancel crawl jobs. action='status' (default) reports one job's progress "
        "and counters; 'list' shows recent jobs; 'cancel' stops a running crawl."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "job_id": {"type": "string", "description": "Job id from net_crawl."},
            "action": {"type": "string", "enum": ["status", "list", "cancel"], "description": "What to do (default status)."},
        },
        "required": [],
    },
}

NET_EXTRACT = {
    "name": "net_extract",
    "description": (
        "Extract structured records (JSON or CSV) from a page using CSS or XPath selectors. "
        "Give an optional repeating 'item' container and a field map; each field may be a "
        "selector string, or {selector, attr, all}. If the page is not already in the corpus "
        "it is fetched first (with the full anti-bot ladder). Optionally MCP-style pagination "
        "is not supported: call it per page."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Page URL (fetched if not already stored)."},
            "doc_id": {"type": "integer", "description": "Alternatively: a corpus document id."},
            "spec": {
                "type": "object",
                "description": (
                    "Extraction spec: {item: 'css selector', fields: {name: 'sel' | "
                    "{selector, attr, all}}, limit: n}"
                ),
            },
            "format": {"type": "string", "enum": ["json", "csv"], "description": "Output format (default json)."},
            "refresh": {"type": "boolean", "description": "Re-fetch even if the URL is already stored."},
        },
        "required": ["spec"],
    },
}

NET_CORPUS = {
    "name": "net_corpus",
    "description": (
        "Query and manage the local crawl corpus (SQLite + FTS5). action='search' runs a "
        "ranked full-text search with snippets; 'get' returns one stored document; 'sites' "
        "lists crawled hosts; 'stats' shows totals; 'export' writes markdown files for a site "
        "or the whole corpus. Use it after net_crawl or net_fetch to retrieve what was saved, "
        "across sessions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["search", "get", "sites", "stats", "export"],
                "description": "Operation (default search).",
            },
            "query": {"type": "string", "description": "Full-text query for action=search."},
            "site": {"type": "string", "description": "Restrict to a host substring."},
            "limit": {"type": "integer", "description": "Max rows (default 10)."},
            "doc_id": {"type": "integer", "description": "Document id for action=get."},
            "url": {"type": "string", "description": "URL for action=get."},
            "path": {"type": "string", "description": "Output directory for action=export."},
        },
        "required": [],
    },
}

ALL = [NET_SEARCH, NET_FETCH, NET_CRAWL, NET_STATUS, NET_EXTRACT, NET_CORPUS]