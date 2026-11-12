"""Structured extraction: CSS/XPath → records (JSON or CSV).

Spec shape (what ``net_extract`` accepts):

```json
{
  "item": "article.product",              // repeating container, optional
  "fields": {
    "title": "h2 a",                      // CSS, text
    "url":   {"selector": "h2 a", "attr": "href"},
    "price": "span.price",
    "tags":  {"selector": ".tag", "all": true}
  },
  "limit": 100
}
```

A field may also be a plain string prefixed with ``xpath:`` for XPath, and
``{selector, attr}`` reads an attribute instead of text. When ``item`` is
omitted, one record is produced from the whole document.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from typing import Any, Optional

from lxml import etree, html as lhtml

logger = logging.getLogger("netcrawler.selectors")


def _parse(html: str):
    parser = lhtml.HTMLParser(recover=True, huge_tree=True)
    try:
        return lhtml.fromstring(html or "", parser=parser)
    except (etree.ParserError, ValueError):
        return None


def _clean(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return " ".join(value.split()).strip()


def _select_css(node, selector: str) -> list[Any]:
    """CSS selection that works for both lxml elements and selectolax nodes."""
    # A selectolax node selects within itself natively; serializing it through
    # lxml would fail, so nested field lookups must go through .css().
    if hasattr(node, "css") and not hasattr(node, "xpath"):
        try:
            return node.css(selector)
        except Exception:
            return []

    parser_cls = None
    try:  # selectolax >= 1.0
        from selectolax.lexbor import LexborHTMLParser as parser_cls  # type: ignore
    except ImportError:
        try:  # older selectolax
            from selectolax.parser import HTMLParser as parser_cls  # type: ignore
        except ImportError:
            parser_cls = None
    if parser_cls is not None:
        try:
            html = etree.tostring(node, encoding="unicode", method="html")
            return parser_cls(html).css(selector)
        except Exception:
            pass
    return _select_css_lxml(node, selector)


_COMPOUND_RE = re.compile(r"^(?P<tag>[a-zA-Z][\w-]*)?(?P<id>#[\w-]+)?(?P<classes>(?:\.[\w-]+)*)$")


def _compound_xpath(token: str) -> Optional[str]:
    match = _COMPOUND_RE.match(token.strip())
    if not match:
        return None
    tag = match.group("tag") or "*"
    ident = (match.group("id") or "").lstrip("#")
    classes = [c for c in (match.group("classes") or "").split(".") if c]
    conditions = []
    if ident:
        conditions.append(f"@id='{ident}'")
    for cls in classes:
        conditions.append(f"contains(concat(' ', normalize-space(@class), ' '), ' {cls} ')")
    return tag + ("[" + " and ".join(conditions) + "]" if conditions else "")


def _select_css_lxml(node, selector: str) -> list[Any]:
    """Approximate CSS with XPath: supports ``tag.class#id`` chains and combinators."""
    try:
        from cssselect import GenericTranslator  # optional dependency

        try:
            return node.xpath(GenericTranslator().css_to_xpath(selector))
        except Exception:
            pass
    except ImportError:
        pass

    tokens = [t for t in re.split(r"\s*>\s*|\s+", selector.strip()) if t]
    if not tokens:
        return []
    first = _compound_xpath(tokens[0]) or "*"
    expr = ".//" + first
    for token in tokens[1:]:
        part = _compound_xpath(token)
        if not part:
            return []
        expr += "//" + part
    try:
        return node.xpath(expr)
    except Exception:
        return []


def _node_text(node: Any) -> str:
    text_content = getattr(node, "text_content", None)
    if callable(text_content):
        try:
            value = text_content()
            if value:
                return _clean(value)
        except Exception:
            pass
    text_attr = getattr(node, "text", None)
    if callable(text_attr):
        for kwargs in ({"strip": True}, {}):
            try:
                value = text_attr(**kwargs)
                if value:
                    return _clean(value)
            except TypeError:
                continue
            except Exception:
                break
    elif isinstance(text_attr, str):
        return _clean(text_attr)
    return ""


def _node_attr(node: Any, attr: str) -> str:
    get = getattr(node, "attributes", None)
    if isinstance(get, dict):
        return _clean(get.get(attr))
    try:
        return _clean(node.get(attr))
    except Exception:
        return ""


def _select(node, selector: str) -> list[Any]:
    selector = (selector or "").strip()
    if not selector:
        return []
    if selector.startswith(("xpath:", "/", "//", "./")):
        expr = selector[6:] if selector.startswith("xpath:") else selector
        try:
            return node.xpath(expr)
        except Exception:
            return []
    return _select_css(node, selector)


def _field_value(node, spec: Any, multiple: bool) -> Any:
    if isinstance(spec, str):
        selector, attr, take_all = spec, "", multiple
    elif isinstance(spec, dict):
        selector = str(spec.get("selector") or spec.get("css") or spec.get("xpath") or "")
        if selector and not selector.startswith(("/", "//", "xpath:")) and spec.get("xpath"):
            selector = "xpath:" + str(spec.get("selector"))
        attr = str(spec.get("attr") or "")
        take_all = bool(spec.get("all")) or multiple
    else:
        return "" if not multiple else []

    nodes = _select(node, selector)
    if not nodes:
        return [] if take_all else ""
    values: list[str] = []
    for candidate in nodes:
        if isinstance(candidate, str):
            values.append(_clean(candidate))
        elif attr:
            values.append(_node_attr(candidate, attr))
        else:
            values.append(_node_text(candidate))
    values = [v for v in values if v]
    if take_all:
        return values
    return values[0] if values else ""


def extract_records(html: str, spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Apply a spec to one document and return records."""
    root = _parse(html)
    if root is None:
        return []
    fields: dict[str, Any] = dict(spec.get("fields") or spec.get("columns") or {})
    if not fields:
        return []
    item_selector = str(spec.get("item") or spec.get("row") or "").strip()
    limit = int(spec.get("limit") or 0) or 0

    containers = _select(root, item_selector) if item_selector else [root]
    if not containers and item_selector:
        # Selector matched nothing: one record from the document is more useful
        # than a silent empty list, and the caller sees field-level empties.
        containers = [root]

    records: list[dict[str, Any]] = []
    for node in containers:
        record: dict[str, Any] = {}
        for name, field_spec in fields.items():
            multiple = bool(isinstance(field_spec, dict) and field_spec.get("all"))
            record[name] = _field_value(node, field_spec, multiple)
        if any(v for v in record.values()):
            records.append(record)
        if limit and len(records) >= limit:
            break
    return records


def records_to_csv(records: list[dict[str, Any]]) -> str:
    if not records:
        return ""
    columns: list[str] = []
    for record in records:
        for key in record:
            if key not in columns:
                columns.append(key)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for record in records:
        row = {k: (" | ".join(v) if isinstance(v, list) else v) for k, v in record.items()}
        writer.writerow(row)
    return buffer.getvalue()


def records_to_json(records: list[dict[str, Any]]) -> str:
    return json.dumps(records, ensure_ascii=False, indent=2)