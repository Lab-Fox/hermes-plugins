"""Local corpus: SQLite + FTS5.

One file (``corpus.db``) under the plugin's data directory holds everything a
crawl learns: documents, the link graph, crawl runs and their resumable
frontier, and per-host clearance cookies. WAL mode + thread-local connections so
concurrent crawl workers do not fight over a single handle.

Content-hash and SimHash dedupe mean a re-crawl refreshes a page instead of
duplicating it, and a syndicated article does not appear three times.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from .urlutil import host_of, url_key

logger = logging.getLogger("netcrawler.store")

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sites (
    id         INTEGER PRIMARY KEY,
    host       TEXT UNIQUE NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    docs       INTEGER DEFAULT 0,
    notes      TEXT
);

CREATE TABLE IF NOT EXISTS docs (
    id           INTEGER PRIMARY KEY,
    site_id      INTEGER REFERENCES sites(id) ON DELETE CASCADE,
    url          TEXT NOT NULL,
    url_key      TEXT UNIQUE NOT NULL,
    title        TEXT,
    description  TEXT,
    lang         TEXT,
    canonical    TEXT,
    markdown     TEXT,
    text         TEXT,
    fit_markdown TEXT,
    meta_json    TEXT,
    jsonld_json  TEXT,
    links_json   TEXT,
    content_hash TEXT,
    simhash      INTEGER,
    method       TEXT,
    provenance   TEXT,
    http_status  INTEGER,
    words        INTEGER,
    bytes        INTEGER,
    fetched_at   TEXT NOT NULL,
    run_id       TEXT
);
CREATE INDEX IF NOT EXISTS idx_docs_site ON docs(site_id);
CREATE INDEX IF NOT EXISTS idx_docs_hash ON docs(content_hash);
CREATE INDEX IF NOT EXISTS idx_docs_run  ON docs(run_id);
CREATE INDEX IF NOT EXISTS idx_docs_fetched ON docs(fetched_at);

CREATE TABLE IF NOT EXISTS links (
    src_id  INTEGER REFERENCES docs(id) ON DELETE CASCADE,
    dst_url TEXT NOT NULL,
    anchor  TEXT
);
CREATE INDEX IF NOT EXISTS idx_links_src ON links(src_id);
CREATE INDEX IF NOT EXISTS idx_links_dst ON links(dst_url);

CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
    title, description, text,
    content='docs', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS docs_ai AFTER INSERT ON docs BEGIN
    INSERT INTO docs_fts(rowid, title, description, text)
    VALUES (new.id, new.title, new.description, new.text);
END;
CREATE TRIGGER IF NOT EXISTS docs_ad AFTER DELETE ON docs BEGIN
    INSERT INTO docs_fts(docs_fts, rowid, title, description, text)
    VALUES ('delete', old.id, old.title, old.description, old.text);
END;
CREATE TRIGGER IF NOT EXISTS docs_au AFTER UPDATE ON docs BEGIN
    INSERT INTO docs_fts(docs_fts, rowid, title, description, text)
    VALUES ('delete', old.id, old.title, old.description, old.text);
    INSERT INTO docs_fts(rowid, title, description, text)
    VALUES (new.id, new.title, new.description, new.text);
END;

CREATE TABLE IF NOT EXISTS runs (
    id         TEXT PRIMARY KEY,
    seed       TEXT NOT NULL,
    status     TEXT NOT NULL,
    opts_json  TEXT,
    started_at TEXT NOT NULL,
    ended_at   TEXT,
    counts_json TEXT
);

CREATE TABLE IF NOT EXISTS frontier (
    run_id   TEXT NOT NULL,
    url      TEXT NOT NULL,
    depth    INTEGER DEFAULT 0,
    priority REAL DEFAULT 0,
    state    TEXT DEFAULT 'queued',
    PRIMARY KEY (run_id, url)
);

CREATE TABLE IF NOT EXISTS host_state (
    host          TEXT PRIMARY KEY,
    cookies_json  TEXT,
    user_agent    TEXT,
    updated_at    TEXT,
    last_status   INTEGER,
    blocked_until REAL DEFAULT 0
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_U64 = 1 << 64


def _signed64(value: Any) -> int:
    """SQLite INTEGER is signed 64-bit; SimHash is unsigned 64-bit."""
    try:
        number = int(value or 0) & (_U64 - 1)
    except (TypeError, ValueError):
        return 0
    return number - _U64 if number >= (1 << 63) else number


def _unsigned64(value: Any) -> int:
    try:
        return int(value or 0) & (_U64 - 1)
    except (TypeError, ValueError):
        return 0


class Corpus:
    """Thread-safe wrapper over one SQLite corpus file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._ready = False
        # Every thread's connection, so close() can release the file (Windows keeps
        # the handle — and the WAL — locked until the last connection goes).
        self._conns: list[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()

    # ---------------------------------------------------------------- plumbing
    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=30.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
            with self._conns_lock:
                self._conns.append(conn)
        if not self._ready:
            with self._init_lock:
                if not self._ready:
                    conn.executescript(SCHEMA)
                    self._ready = True
        return conn

    def close(self) -> None:
        with self._conns_lock:
            connections = list(self._conns)
            self._conns.clear()
        for conn in connections:
            try:
                conn.close()
            except Exception:
                pass
        self._local.conn = None

    # ------------------------------------------------------------------- sites
    def _site_id(self, url: str) -> int:
        host = host_of(url)
        conn = self.conn
        row = conn.execute("SELECT id FROM sites WHERE host=?", (host,)).fetchone()
        now = utcnow()
        if row:
            conn.execute("UPDATE sites SET last_seen=? WHERE id=?", (now, row["id"]))
            return int(row["id"])
        cur = conn.execute(
            "INSERT INTO sites(host, first_seen, last_seen, docs) VALUES (?,?,?,0)",
            (host, now, now),
        )
        return int(cur.lastrowid)

    # -------------------------------------------------------------------- docs
    def upsert_doc(self, doc: dict[str, Any], run_id: str = "") -> dict[str, Any]:
        """Insert or refresh one document. Returns ``{id, new, duplicate}``."""
        url = doc.get("final_url") or doc.get("url") or ""
        key = url_key(url)
        if not key:
            return {"id": 0, "new": False, "duplicate": True}
        conn = self.conn
        site_id = self._site_id(url)
        now = utcnow()
        links = doc.get("links") or []
        existing = conn.execute(
            "SELECT id, content_hash, simhash FROM docs WHERE url_key=?", (key,)
        ).fetchone()

        values = (
            site_id, url, key, doc.get("title") or "", doc.get("description") or "",
            doc.get("lang") or "", doc.get("canonical") or "", doc.get("markdown") or "",
            doc.get("text") or "", doc.get("fit_markdown") or "",
            json.dumps({k: doc.get(k) for k in ("author", "date", "sitename", "kind", "pdf_pages", "gates_clicked")}),
            json.dumps(doc.get("jsonld") or [], ensure_ascii=False)[:2_000_000],
            json.dumps(links, ensure_ascii=False)[:4_000_000],
            doc.get("content_hash") or "", _signed64(doc.get("simhash") or 0),
            doc.get("method") or "", doc.get("provenance") or "live",
            int(doc.get("status") or 0), int(doc.get("words") or 0),
            int(len((doc.get("text") or "").encode("utf-8", "replace"))),
            now, run_id,
        )

        duplicate = False
        incoming_hash = doc.get("content_hash") or ""
        if existing:
            duplicate = bool(existing["content_hash"]) and existing["content_hash"] == incoming_hash
            conn.execute(
                """UPDATE docs SET site_id=?, url=?, url_key=?, title=?, description=?, lang=?,
                   canonical=?, markdown=?, text=?, fit_markdown=?, meta_json=?, jsonld_json=?,
                   links_json=?, content_hash=?, simhash=?, method=?, provenance=?, http_status=?,
                   words=?, bytes=?, fetched_at=?, run_id=? WHERE id=?""",
                values + (existing["id"],),
            )
            doc_id = int(existing["id"])
            conn.execute("DELETE FROM links WHERE src_id=?", (doc_id,))
        else:
            if incoming_hash:
                # Same content served at a different URL (syndication, mirrors, A/B hosts).
                hit = conn.execute(
                    "SELECT 1 FROM docs WHERE content_hash=? LIMIT 1", (incoming_hash,)
                ).fetchone()
                duplicate = hit is not None
            cur = conn.execute(
                """INSERT INTO docs(site_id, url, url_key, title, description, lang, canonical,
                   markdown, text, fit_markdown, meta_json, jsonld_json, links_json, content_hash,
                   simhash, method, provenance, http_status, words, bytes, fetched_at, run_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                values,
            )
            doc_id = int(cur.lastrowid)

        rows = [(doc_id, link.get("url"), (link.get("text") or "")[:300])
                for link in links if isinstance(link, dict) and link.get("url")]
        if rows:
            conn.executemany("INSERT INTO links(src_id, dst_url, anchor) VALUES (?,?,?)", rows)

        conn.execute("UPDATE sites SET docs=(SELECT COUNT(*) FROM docs WHERE site_id=?) WHERE id=?",
                     (site_id, site_id))
        conn.commit()
        return {"id": doc_id, "new": existing is None, "duplicate": duplicate}

    def get_doc(self, *, doc_id: Optional[int] = None, url: Optional[str] = None) -> Optional[dict[str, Any]]:
        conn = self.conn
        if doc_id:
            row = conn.execute("SELECT * FROM docs WHERE id=?", (int(doc_id),)).fetchone()
        elif url:
            row = conn.execute("SELECT * FROM docs WHERE url_key=?", (url_key(url),)).fetchone()
        else:
            return None
        return dict(row) if row else None

    def has_url(self, url: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM docs WHERE url_key=?", (url_key(url),)).fetchone()
        return row is not None

    def near_duplicate(self, sim: int, *, max_distance: int = 3, window: int = 400) -> Optional[int]:
        """Find a recent document close enough in SimHash Hamming space."""
        if not sim:
            return None
        target = _unsigned64(sim)
        rows = self.conn.execute(
            "SELECT id, simhash FROM docs WHERE simhash != 0 ORDER BY id DESC LIMIT ?", (window,)
        ).fetchall()
        for row in rows:
            other = _unsigned64(row["simhash"] or 0)
            if other and bin(other ^ target).count("1") <= max_distance:
                return int(row["id"])
        return None

    # ------------------------------------------------------------------ search
    @staticmethod
    def _fts_query(query: str) -> str:
        """Sanitize free text into FTS5 syntax with prefix matching.

        Prefix matters: a corpus search for "quote" must find the token
        "quotes". Terms are reduced to alphanumerics so no FTS operator can be
        injected and no query can raise a syntax error.
        """
        import re

        terms = re.findall(r"[0-9A-Za-z\u00c0-\u024f]{2,}", query or "")
        if not terms:
            return ""
        return " OR ".join(f"{term.lower()}*" for term in terms[:24])

    def search(
        self,
        query: str,
        *,
        site: str = "",
        limit: int = 10,
        snippet_chars: int = 300,
    ) -> list[dict[str, Any]]:
        conn = self.conn
        match = self._fts_query(query)
        sql_base = (
            "SELECT d.id, d.url, d.title, d.method, d.fetched_at, d.words, s.host, "
            "snippet(docs_fts, 2, '«', '»', ' … ', 24) AS snippet, bm25(docs_fts) AS rank "
            "FROM docs_fts JOIN docs d ON d.id = docs_fts.rowid "
            "LEFT JOIN sites s ON s.id = d.site_id "
        )
        params: list[Any] = []
        where = []
        if site:
            where.append("s.host LIKE ?")
            params.append(f"%{site.lower()}%")
        if match:
            where.append("docs_fts MATCH ?")
            params.append(match)
            sql = sql_base + ("WHERE " + " AND ".join(where) if where else "") + \
                " ORDER BY rank LIMIT ?"
        else:
            where.append("(d.text LIKE ? OR d.title LIKE ?)")
            params.extend([f"%{query}%", f"%{query}%"])
            sql = sql_base + "WHERE " + " AND ".join(where) + " ORDER BY d.fetched_at DESC LIMIT ?"
        params.append(int(limit))
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            logger.warning("FTS query failed (%s); falling back to LIKE", exc)
            rows = conn.execute(
                sql_base + "WHERE d.text LIKE ? ORDER BY d.fetched_at DESC LIMIT ?",
                (f"%{query}%", int(limit)),
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            if item.get("snippet"):
                item["snippet"] = str(item["snippet"])[:snippet_chars]
            out.append(item)
        return out

    # -------------------------------------------------------------------- misc
    def list_sites(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT s.host, s.docs, s.first_seen, s.last_seen, "
            "COALESCE((SELECT MAX(fetched_at) FROM docs d WHERE d.site_id=s.id),'') AS last_fetch "
            "FROM sites s ORDER BY s.docs DESC, s.host ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict[str, Any]:
        conn = self.conn
        docs = conn.execute("SELECT COUNT(*) c, COALESCE(SUM(words),0) w FROM docs").fetchone()
        sites = conn.execute("SELECT COUNT(*) c FROM sites").fetchone()
        links = conn.execute("SELECT COUNT(*) c FROM links").fetchone()
        runs = conn.execute("SELECT COUNT(*) c FROM runs").fetchone()
        try:
            size = self.path.stat().st_size
        except OSError:
            size = 0
        for suffix in ("-wal", "-shm"):
            try:
                size += (self.path.parent / (self.path.name + suffix)).stat().st_size
            except OSError:
                pass
        return {
            "documents": int(docs["c"]), "words": int(docs["w"]),
            "sites": int(sites["c"]), "links": int(links["c"]),
            "runs": int(runs["c"]), "db_bytes": size,
            "db_path": str(self.path),
        }

    def prune(self, max_docs: int) -> int:
        if max_docs <= 0:
            return 0
        conn = self.conn
        total = int(conn.execute("SELECT COUNT(*) c FROM docs").fetchone()["c"])
        if total <= max_docs:
            return 0
        excess = total - max_docs
        ids = [int(r["id"]) for r in conn.execute(
            "SELECT id FROM docs ORDER BY fetched_at ASC LIMIT ?", (excess,)
        ).fetchall()]
        if ids:
            conn.executemany("DELETE FROM docs WHERE id=?", [(i,) for i in ids])
            conn.commit()
        return len(ids)

    # ------------------------------------------------------------------- runs
    def save_run(self, run_id: str, seed: str, opts: dict[str, Any], status: str = "running") -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO runs(id, seed, status, opts_json, started_at, counts_json) "
            "VALUES (?,?,?,?,?,?)",
            (run_id, seed, status, json.dumps(opts, ensure_ascii=False), utcnow(), "{}"),
        )
        self.conn.commit()

    def update_run(self, run_id: str, *, status: Optional[str] = None,
                   counts: Optional[dict[str, Any]] = None) -> None:
        sets = []
        params: list[Any] = []
        if status:
            sets.append("status=?")
            params.append(status)
            if status in ("done", "failed", "cancelled"):
                sets.append("ended_at=?")
                params.append(utcnow())
        if counts is not None:
            sets.append("counts_json=?")
            params.append(json.dumps(counts, ensure_ascii=False))
        if not sets:
            return
        params.append(run_id)
        self.conn.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id=?", params)
        self.conn.commit()

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        for key in ("opts_json", "counts_json"):
            try:
                item[key.replace("_json", "")] = json.loads(item.get(key) or "{}")
            except json.JSONDecodeError:
                item[key.replace("_json", "")] = {}
        return item

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, seed, status, started_at, ended_at, counts_json FROM runs "
            "ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["counts"] = json.loads(item.pop("counts_json") or "{}")
            except json.JSONDecodeError:
                item["counts"] = {}
            out.append(item)
        return out

    # --------------------------------------------------------------- frontier
    def frontier_save(self, run_id: str, items: Iterable[tuple[str, int, float]]) -> None:
        rows = [(run_id, url, depth, priority) for url, depth, priority in items]
        if rows:
            self.conn.executemany(
                "INSERT OR IGNORE INTO frontier(run_id, url, depth, priority, state) "
                "VALUES (?,?,?,?,'queued')", rows,
            )
            self.conn.commit()

    def frontier_mark(self, run_id: str, url: str, state: str) -> None:
        self.conn.execute("UPDATE frontier SET state=? WHERE run_id=? AND url=?", (state, run_id, url))
        self.conn.commit()

    def frontier_load(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT url, depth, priority FROM frontier WHERE run_id=? AND state='queued' "
            "ORDER BY priority DESC, depth ASC", (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ host state
    def set_host_cookies(self, host: str, cookies: dict[str, str], status: int = 0) -> None:
        self.conn.execute(
            "INSERT INTO host_state(host, cookies_json, updated_at, last_status) VALUES (?,?,?,?) "
            "ON CONFLICT(host) DO UPDATE SET cookies_json=excluded.cookies_json, "
            "updated_at=excluded.updated_at, last_status=excluded.last_status",
            (host, json.dumps(cookies or {}), utcnow(), status),
        )
        self.conn.commit()

    def get_host_cookies(self, host: str) -> dict[str, str]:
        row = self.conn.execute(
            "SELECT cookies_json FROM host_state WHERE host=?", (host,)
        ).fetchone()
        if not row:
            return {}
        try:
            return json.loads(row["cookies_json"] or "{}")
        except json.JSONDecodeError:
            return {}

    def all_host_cookies(self) -> dict[str, dict[str, str]]:
        out: dict[str, dict[str, str]] = {}
        for row in self.conn.execute("SELECT host, cookies_json FROM host_state").fetchall():
            try:
                cookies = json.loads(row["cookies_json"] or "{}")
            except json.JSONDecodeError:
                continue
            if cookies:
                out[str(row["host"])] = cookies
        return out

    def export_docs(self, *, site: str = "", limit: int = 1000) -> list[dict[str, Any]]:
        conn = self.conn
        if site:
            rows = conn.execute(
                "SELECT d.id, d.url, d.title, d.markdown, d.fetched_at, s.host FROM docs d "
                "LEFT JOIN sites s ON s.id=d.site_id WHERE s.host LIKE ? "
                "ORDER BY d.fetched_at DESC LIMIT ?", (f"%{site.lower()}%", int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT d.id, d.url, d.title, d.markdown, d.fetched_at, s.host FROM docs d "
                "LEFT JOIN sites s ON s.id=d.site_id ORDER BY d.fetched_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]