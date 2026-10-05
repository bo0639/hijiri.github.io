#!/usr/bin/env python3
"""ガチャガチャ（カプセルトイ）の発売ラインアップ自動収集スクリプト。

gacha/sources.json に定義した各メーカー公式の発売スケジュールページから
  商品名 / 価格 / 入荷めど（日付・第N週・上中下旬・月内）/ 画像 / 商品ページURL
を抜き出し、gacha/feed.json に書き出す。

GitHub Actions（.github/workflows/update_gacha_lineup.yml）から週2回実行される。
標準ライブラリのみで動作する。

ページのHTML構造はメーカーごとに異なり、予告なく変わるため、特定のクラス名には頼らず
  「商品ページへのリンクを含む小さなまとまり（カード）」を探し、
  その中の価格・入荷めど、無ければ直前の見出し（例: 「10月第2週」）を採用する
という汎用的な抽出を行う。取得に失敗したソースは status に理由を残し、他は続行する。

  python scripts/update_gacha_lineup.py               # 収集して feed.json を更新
  python scripts/update_gacha_lineup.py --file x.html --source bandai   # 保存済みHTMLで抽出を試す
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from html.parser import HTMLParser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES_PATH = os.path.join(ROOT, "gacha", "sources.json")
FEED_PATH = os.path.join(ROOT, "gacha", "feed.json")
# 設定すると取得したHTMLをここに保存する（ページ構造が変わったときの調査用。Actionsの成果物として残る）
DUMP_DIR = os.environ.get("GACHA_DUMP_DIR", "")

JST = dt.timezone(dt.timedelta(hours=9))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 25
PAUSE = 2                 # 同じサイトへの連続アクセスを避ける待ち時間（秒）
CARD_MAX_TEXT = 360       # これより文字数の多いまとまりは「カード」とみなさない
KEEP_PAST_DAYS = 45       # 入荷めどの終わりがこれより前の商品は捨てる
KEEP_FUTURE_DAYS = 300    # 誤抽出の遠い未来を弾く

# ── 入荷めど（日付・週・旬・月）の解釈 ────────────────────────────
# index.html の parseArrival() と同じ規則。片方を直したらもう片方も直す。

RANGE_SEP = r"\s*(?:[~〜\-ー－–・、]|から)\s*"
RE_WEEK = re.compile(
    r"(?:(20\d{2})\s*年\s*)?(\d{1,2})\s*月\s*第?\s*([1-5])\s*週?"
    r"(?:" + RANGE_SEP + r"(?:(\d{1,2})\s*月\s*)?第?\s*([1-5])\s*)?週")
RE_WEEK_ONLY = re.compile(r"第\s*([1-5])\s*(?:" + RANGE_SEP + r"第?\s*([1-5])\s*)?週")
RE_JUN = re.compile(
    r"(?:(20\d{2})\s*年\s*)?(\d{1,2})\s*月\s*(上|中|下)\s*旬"
    r"(?:" + RANGE_SEP + r"(?:(\d{1,2})\s*月\s*)?(上|中|下)\s*旬)?")
RE_DAY_WEEK = re.compile(r"(?:(20\d{2})\s*年\s*)?(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*週")
RE_YMD = re.compile(r"(20\d{2})\s*[/.\-年]\s*(\d{1,2})\s*[/.\-月]\s*(\d{1,2})(?!\d)")
RE_MD = re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日")
RE_MD_SLASH = re.compile(r"(?<![\d/])(\d{1,2})\s*/\s*(\d{1,2})(?![\d/])")
RE_MONTH = re.compile(r"(?:(20\d{2})\s*年\s*)?(?<!\d)(\d{1,2})\s*月(?!\s*\d)")
RE_YEAR_MONTH = re.compile(r"(20\d{2})\s*[./]\s*(\d{1,2})")
RE_PRICE = re.compile(r"(?<![\d,])(\d{3,4}|\d,\d{3})\s*円")

JUN_DAYS = {"上": (1, 10), "中": (11, 20), "下": (21, 31)}


def norm(s: str) -> str:
    """全角英数・記号を半角に寄せ、空白を詰める。"""
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"\s+", " ", s).strip()


def month_end(y: int, m: int) -> int:
    nxt = dt.date(y + (m == 12), m % 12 + 1, 1)
    return (nxt - dt.timedelta(days=1)).day


def guess_year(m: int, today: dt.date) -> int:
    """年が書かれていない月を、今日に近い年（約4か月前〜8か月先）に寄せる。"""
    best = None
    for y in (today.year - 1, today.year, today.year + 1):
        diff = (dt.date(y, m, 15) - today).days
        if -125 <= diff <= 260 and (best is None or abs(diff) < best[0]):
            best = (abs(diff), y)
    return best[1] if best else today.year


def week_range(y: int, m: int, n: int) -> tuple[dt.date, dt.date]:
    """その月の第n週（月曜はじまり、1日を含む週が第1週）を月内に収めて返す。"""
    first = dt.date(y, m, 1)
    start = first - dt.timedelta(days=first.weekday()) + dt.timedelta(weeks=n - 1)
    end = start + dt.timedelta(days=6)
    last = dt.date(y, m, month_end(y, m))
    return max(start, first), min(end, last)


def _iso(d: dt.date) -> str:
    return d.isoformat()


def parse_arrival(text: str, today: dt.date, ctx_month: tuple[int, int] | None = None,
                  allow_slash: bool = True) -> dict | None:
    """「10月第2週」「10月第2〜3週」「11月上旬」「2026年12月」「10/18」などを
    {kind, label, from, to} に変換する。読めなければ None。
    allow_slash=False にすると「10/18」形式を読まない（商品名の「らんま1/2」を日付と誤読しないため）。"""
    t = norm(text)
    if not t:
        return None

    def ym(y, m):
        m = int(m)
        if not 1 <= m <= 12:
            return None
        if y:
            return int(y), m
        if ctx_month:  # ページの「2026.10」などの年月に寄せる
            cy, cm = ctx_month
            return cy + (1 if m < cm - 6 else -1 if m > cm + 6 else 0), m
        return guess_year(m, today), m

    m = RE_DAY_WEEK.search(t)
    if m:
        base = ym(m.group(1), m.group(2))
        if base:
            try:
                a = dt.date(base[0], base[1], int(m.group(3)))
            except ValueError:
                a = None
            if a:
                return {"kind": "week", "label": f"{a.month}月{a.day}日週", "from": _iso(a),
                        "to": _iso(a + dt.timedelta(days=6))}

    m = RE_WEEK.search(t)
    if m:
        base = ym(m.group(1), m.group(2))
        if base:
            y, mo = base
            w1 = int(m.group(3))
            w2 = int(m.group(5)) if m.group(5) else w1
            mo2 = int(m.group(4)) if m.group(4) else mo
            y2 = y + (1 if mo2 < mo else 0)
            if 1 <= mo2 <= 12:
                a, _ = week_range(y, mo, w1)
                _, b = week_range(y2, mo2, w2)
                if b >= a:
                    lab = f"{mo}月第{w1}週" if (w1, mo) == (w2, mo2) else (
                        f"{mo}月第{w1}〜{w2}週" if mo == mo2 else f"{mo}月第{w1}週〜{mo2}月第{w2}週")
                    return {"kind": "week", "label": lab, "from": _iso(a), "to": _iso(b)}

    m = RE_JUN.search(t)
    if m:
        base = ym(m.group(1), m.group(2))
        if base:
            y, mo = base
            j1, j2 = m.group(3), m.group(5) or m.group(3)
            mo2 = int(m.group(4)) if m.group(4) else mo
            y2 = y + (1 if mo2 < mo else 0)
            if 1 <= mo2 <= 12:
                a = dt.date(y, mo, JUN_DAYS[j1][0])
                b = dt.date(y2, mo2, min(JUN_DAYS[j2][1], month_end(y2, mo2)))
                if b >= a:
                    lab = f"{mo}月{j1}旬" if (j1, mo) == (j2, mo2) else (
                        f"{mo}月{j1}旬〜{j2}旬" if mo == mo2 else f"{mo}月{j1}旬〜{mo2}月{j2}旬")
                    return {"kind": "jun", "label": lab, "from": _iso(a), "to": _iso(b)}

    for rx, has_year in ((RE_YMD, True), (RE_MD, False), (RE_MD_SLASH, False)):
        if rx is RE_MD_SLASH and not allow_slash:
            continue
        m = rx.search(t)
        if not m:
            continue
        g = m.groups()
        y, mo, d = (int(g[0]), int(g[1]), int(g[2])) if has_year else (None, int(g[0]), int(g[1]))
        if not 1 <= mo <= 12:
            continue
        y = y or guess_year(mo, today)
        try:
            day = dt.date(y, mo, d)
        except ValueError:
            continue
        return {"kind": "date", "label": f"{mo}月{d}日", "from": _iso(day), "to": _iso(day)}

    if ctx_month:
        m = RE_WEEK_ONLY.search(t)
        if m:
            y, mo = ctx_month
            w1 = int(m.group(1))
            w2 = int(m.group(2)) if m.group(2) else w1
            a, _ = week_range(y, mo, w1)
            _, b = week_range(y, mo, w2)
            lab = f"{mo}月第{w1}週" if w1 == w2 else f"{mo}月第{w1}〜{w2}週"
            return {"kind": "week", "label": lab, "from": _iso(a), "to": _iso(b)}

    m = RE_MONTH.search(t)
    if m:
        base = ym(m.group(1), m.group(2))
        if base:
            y, mo = base
            return {"kind": "month", "label": f"{mo}月", "from": _iso(dt.date(y, mo, 1)),
                    "to": _iso(dt.date(y, mo, month_end(y, mo)))}
    return None


def parse_month_heading(text: str, today: dt.date) -> tuple[int, int] | None:
    """「2026年10月」「10月発売」のような短い見出しから (年, 月) を取る。"""
    t = norm(text)
    if len(t) > 24:
        return None
    m = RE_YEAR_MONTH.fullmatch(t)
    if m and 1 <= int(m.group(2)) <= 12:
        return int(m.group(1)), int(m.group(2))
    m = RE_MONTH.search(t)
    if not m or not 1 <= int(m.group(2)) <= 12:
        return None
    mo = int(m.group(2))
    return (int(m.group(1)) if m.group(1) else guess_year(mo, today)), mo


def parse_price(text: str) -> int | None:
    m = RE_PRICE.search(norm(text))
    if not m:
        return None
    v = int(m.group(1).replace(",", ""))
    return v if 100 <= v <= 3000 else None


# ── 小さなDOM（html.parser で木を組み立てる） ─────────────────────

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
        "param", "source", "track", "wbr"}
BLOCK = {"p", "div", "li", "ul", "ol", "dl", "dt", "dd", "tr", "td", "th", "table",
         "section", "article", "h1", "h2", "h3", "h4", "h5", "h6", "header", "footer",
         "figure", "figcaption", "br"}
SKIP = {"script", "style", "noscript", "template", "svg", "head"}
# 月切り替えタブ・セレクトの選択肢などは見出しとして扱わない
CONTEXT_SKIP = {"#root", "a", "option", "select", "button", "script", "style"}


class Node:
    __slots__ = ("tag", "attrs", "children", "parent", "idx", "_text")

    def __init__(self, tag: str, attrs: dict, parent: "Node | None"):
        self.tag, self.attrs, self.parent = tag, attrs, parent
        self.children: list = []
        self.idx = 0
        self._text: str | None = None

    def text(self) -> str:
        if self._text is None:
            parts = []
            for c in self.children:
                if isinstance(c, str):
                    parts.append(c)
                elif c.tag not in SKIP:
                    t = c.text()
                    parts.append(("\n" + t + "\n") if c.tag in BLOCK else t)
            self._text = re.sub(r"[ \t\r\f\v]+", " ", re.sub(r"\n\s*\n+", "\n", "".join(parts))).strip()
        return self._text

    def iter(self):
        yield self
        for c in self.children:
            if isinstance(c, Node):
                yield from c.iter()

    def contains(self, other: "Node") -> bool:
        while other is not None:
            if other is self:
                return True
            other = other.parent
        return False


class TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {}, None)
        self.cur = self.root
        self.count = 0

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: (v or "") for k, v in attrs}, self.cur)
        self.count += 1
        node.idx = self.count
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.cur = self.cur.parent

    def handle_endtag(self, tag):
        n = self.cur
        while n is not None and n.tag != tag:
            n = n.parent
        if n is not None and n.parent is not None:
            self.cur = n.parent

    def handle_data(self, data):
        if self.cur.tag not in SKIP:
            self.cur.children.append(data)


def build_tree(body: str) -> Node:
    tb = TreeBuilder()
    tb.feed(body)
    tb.close()
    return tb.root


# ── ページからの商品抽出 ───────────────────────────────────────

JUNK_LINE = re.compile(
    r"^(?:new|NEW!?|再販|再登場|詳細|詳しく|もっと見る|more|一覧|発売(?:予定|日)?|価格|"
    r"\d+(?:種|個)|全\d+種.*|税込.*|[\d,]+\s*円.*|※.*|>|›|»)$", re.I)


def img_src(node: Node, base: str) -> str:
    for n in node.iter():
        if n.tag != "img":
            continue
        for key in ("data-src", "data-original", "data-lazy-src", "src"):
            v = n.attrs.get(key, "").strip()
            if v and not v.startswith("data:") and not re.search(r"spacer|blank|loading|icon|logo", v, re.I):
                return urllib.parse.urljoin(base, v)
        v = n.attrs.get("srcset", "").split(",")[0].strip().split(" ")[0]
        if v:
            return urllib.parse.urljoin(base, v)
    return ""


def clean(s: str) -> str:
    """表示用の名前。全角記号などは元の表記のまま、空白だけ詰める。"""
    return re.sub(r"\s+", " ", s or "").strip()


CHROME_RE = re.compile(r"(?:^|[\s_-])(?:header|footer|nav|navi|gnav|menu|breadcrumbs?|pankuzu|sidebar|pager|pagination)(?:$|[\s_-])", re.I)


def in_chrome(node: Node) -> bool:
    """ヘッダー・フッター・メニュー・パンくずなど、商品一覧ではない部分か。"""
    n = node
    while n is not None:
        if n.tag in ("header", "footer", "nav", "aside"):
            return True
        if CHROME_RE.search(n.attrs.get("class", "") + " " + n.attrs.get("id", "")):
            return True
        n = n.parent
    return False


def pick_name(card: Node, today: dt.date) -> str:
    # クラス名に name / title / ttl を含む要素を最優先にする
    for n in card.iter():
        cls = (n.attrs.get("class", "") + " " + n.attrs.get("id", "")).lower()
        if n is not card and re.search(r"(?:^|[\s_-])(?:name|title|ttl|item-?name|product-?name)", cls):
            t = clean(n.text())
            if 2 < len(t) <= 90 and not JUNK_LINE.match(norm(t)) and not parse_arrival(t, today):
                return t
    lines = [clean(x) for x in card.text().split("\n")]
    lines = [x for x in lines if 2 < len(x) <= 90 and not JUNK_LINE.match(norm(x))
             and not RE_PRICE.fullmatch(norm(x)) and not (parse_arrival(x, today) and len(x) < 20)]
    if lines:
        return max(lines, key=len)
    for n in card.iter():
        if n.tag == "img" and len(clean(n.attrs.get("alt", ""))) > 2:
            return clean(n.attrs["alt"])
    return ""


def link_ok(href: str, base: str, src: dict) -> str | None:
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
        return None
    url = urllib.parse.urljoin(base, href).split("#")[0]
    host = urllib.parse.urlparse(url).netloc
    hosts = src.get("hosts") or [urllib.parse.urlparse(base).netloc]
    if host not in hosts:
        return None
    pat = src.get("item_href")
    if pat and not re.search(pat, url):
        return None
    if not pat and url.rstrip("/") == base.split("?")[0].rstrip("/"):
        return None
    return url


def extract_items(body: str, base: str, src: dict, today: dt.date) -> list[dict]:
    root = build_tree(body)
    nodes = [n for n in root.iter() if n.tag not in SKIP]
    links: dict[str, Node] = {}
    for n in nodes:
        if n.tag == "a" and not in_chrome(n):
            url = link_ok(n.attrs.get("href", ""), base, src)
            if url and url not in links:
                links[url] = n

    def distinct_links(node: Node) -> int:
        seen = set()
        for n in node.iter():
            if n.tag == "a":
                u = link_ok(n.attrs.get("href", ""), base, src)
                if u:
                    seen.add(u)
                    if len(seen) > 1:
                        break
        return len(seen)

    cards: list[tuple[str, Node]] = []
    for url, a in links.items():
        card = a
        while (card.parent is not None and card.parent.tag != "#root"
               and len(card.parent.text()) <= CARD_MAX_TEXT and distinct_links(card.parent) <= 1):
            card = card.parent
        cards.append((url, card))

    # カードの外にある短い見出しを、文書順に「入荷めど／月」の文脈として拾う
    in_card = set()
    for _, c in cards:
        for n in c.iter():
            in_card.add(id(n))
    contexts: list[tuple[int, dict | None, tuple | None]] = []
    cur_mh = None  # 直前に出てきたページの年月（「2026.10」など）
    for n in nodes:
        if id(n) in in_card or n.tag in CONTEXT_SKIP or in_chrome(n):
            continue
        t = norm(n.text())
        if not t or len(t) > 80:
            continue
        mh = parse_month_heading(t, today)
        cur_mh = mh or cur_mh
        arr = parse_arrival(t, today, cur_mh, allow_slash=False)
        if arr or mh:
            contexts.append((n.idx, arr, mh))

    def context_for(idx: int):
        """カードより前にある最も近い見出しの入荷めどと月。"""
        arr, mh = None, None
        for i, a, m in contexts:
            if i > idx:
                break
            arr = a or arr
            mh = m or mh
        return arr, mh

    items = []
    for url, card in cards:
        text = card.text()
        price = parse_price(text)
        ctx_arr, ctx_month = context_for(card.idx)
        name = pick_name(card, today)
        own_arr = None
        for line in text.split("\n"):
            if clean(line) == name:
                continue  # 商品名の中の数字（「1/2」「12月の…」）を入荷めどと読まない
            own_arr = parse_arrival(line, today, ctx_month, allow_slash=False)
            if own_arr:
                break
        if not price and not own_arr and not src.get("item_href"):
            continue
        arr = own_arr or ctx_arr
        if not arr:
            continue
        if not name:
            continue
        frm, to = dt.date.fromisoformat(arr["from"]), dt.date.fromisoformat(arr["to"])
        if to < today - dt.timedelta(days=KEEP_PAST_DAYS) or frm > today + dt.timedelta(days=KEEP_FUTURE_DAYS):
            continue
        items.append({
            "id": src["key"] + "-" + hashlib.sha1(url.encode()).hexdigest()[:10],
            "source": src["key"],
            "maker": src.get("maker", ""),
            "name": name,
            "price": price,
            "arrival": arr,
            "image": img_src(card, base),
            "url": url,
        })
    return items


# ── 取得 ────────────────────────────────────────────────────────

def fetch(url: str, retries: int = 1) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "ja,en;q=0.8",
    })
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
                raw = res.read()
            break
        except Exception:  # noqa: BLE001 - 一時的な拒否はリトライする
            if attempt >= retries:
                raise
            time.sleep(5 * (attempt + 1))
    m = re.search(rb"charset=[\"']?([\w\-]+)", raw[:4096], re.I)
    for enc in filter(None, [m and m.group(1).decode("ascii", "ignore"), "utf-8", "cp932", "euc-jp"]):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def expand_urls(src: dict, today: dt.date) -> list[str]:
    """url_templates の {yyyy} {mm} {m} を months_back か月前〜months_ahead か月先で展開する。"""
    urls = list(src.get("urls", []))
    back = int(src.get("months_back", 0))
    for tpl in src.get("url_templates", []):
        y, m = today.year, today.month - back
        while m < 1:
            y, m = y - 1, m + 12
        for _ in range(back + int(src.get("months_ahead", 3)) + 1):
            urls.append(tpl.format(yyyy=y, mm=f"{m:02d}", m=m))
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    out = []
    for u in urls:
        if u not in out:
            out.append(u)
    return out


def collect_source(src: dict, today: dt.date) -> tuple[list[dict], dict]:
    status = {"key": src["key"], "name": src.get("name", src["key"]), "maker": src.get("maker", ""),
              "url": src.get("link") or (src.get("urls") or [""])[0], "ok": False, "count": 0, "error": ""}
    items: dict[str, dict] = {}
    errors = []
    pages = expand_urls(src, today)
    follow = re.compile(src["follow"]) if src.get("follow") else None
    seen = set()
    while pages and len(seen) < int(src.get("max_pages", 6)):
        url = pages.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            body = fetch(url)
        except Exception as exc:  # noqa: BLE001 - 収集失敗は status に記録して続行
            errors.append(f"{url}: {exc}")
            continue
        if DUMP_DIR:
            os.makedirs(DUMP_DIR, exist_ok=True)
            name = f"{src['key']}-{len(seen):02d}.html"
            with open(os.path.join(DUMP_DIR, name), "w", encoding="utf-8") as f:
                f.write(body)
            with open(os.path.join(DUMP_DIR, "index.txt"), "a", encoding="utf-8") as f:
                f.write(f"{name}\t{url}\n")
        for it in extract_items(body, url, src, today):
            items.setdefault(it["id"], it)
        if follow:
            for href in re.findall(r"href=[\"']([^\"'#]+)[\"']", body):
                nu = urllib.parse.urljoin(url, html.unescape(href))
                if follow.search(nu) and nu not in seen and nu not in pages:
                    pages.append(nu)
        time.sleep(PAUSE)
    status["count"] = len(items)
    status["ok"] = bool(items)
    if not items:
        status["error"] = "; ".join(errors)[:300] or "商品を抽出できませんでした（ページ構造が変わった可能性）"
    elif errors:
        status["error"] = f"一部ページの取得に失敗: {len(errors)}件"
    return list(items.values()), status


def load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def collect(today: dt.date) -> dict:
    cfg = load_json(SOURCES_PATH, {"sources": []})
    prev = load_json(FEED_PATH, {})
    prev_items = {it["id"]: it for it in prev.get("items", [])}
    now = dt.datetime.now(JST).isoformat(timespec="seconds")

    items, statuses = [], []
    for src in cfg.get("sources", []):
        if src.get("enabled") is False:
            continue
        got, st = collect_source(src, today)
        statuses.append(st)
        if not got and st["error"]:
            # 取得に失敗したソースは前回分を残し、カレンダーから消えないようにする
            got = [it for it in prev_items.values() if it.get("source") == src["key"]
                   and it["arrival"]["to"] >= (today - dt.timedelta(days=KEEP_PAST_DAYS)).isoformat()]
            st["kept"] = len(got)
        for it in got:
            old = prev_items.get(it["id"])
            it["first_seen"] = (old or {}).get("first_seen") or now
            if old and old.get("arrival") != it["arrival"]:
                it["arrival_changed"] = now
            elif old and old.get("arrival_changed"):
                it["arrival_changed"] = old["arrival_changed"]
        items.extend(got)

    items.sort(key=lambda x: (x["arrival"]["from"], x["arrival"]["to"], x["maker"], x["name"]))
    return {"generated_at": now, "sources": statuses, "items": items}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--file", help="ネットに出ず、保存済みHTMLから抽出を試す")
    ap.add_argument("--source", help="--file と一緒に使うソースのkey")
    ap.add_argument("--base", default="", help="--file のページURL（相対リンク解決用）")
    ap.add_argument("--today", help="基準日 YYYY-MM-DD（テスト用）")
    ap.add_argument("--arrival", metavar="TEXT",
                    help="入荷めどの文字（例: 10月第2週）を data.json の arrival 形式で表示する（Claude Codeでの更新用）")
    ap.add_argument("--find", metavar="WORD",
                    help="feed.json のラインアップを商品名で検索して表示する（Claude Codeでの更新用）")
    args = ap.parse_args()
    today = dt.date.fromisoformat(args.today) if args.today else dt.datetime.now(JST).date()

    if args.arrival is not None:
        arr = parse_arrival(args.arrival, today)
        if not arr:
            print(f"読み取れません: {args.arrival}", file=sys.stderr)
            return 1
        print(json.dumps(arr, ensure_ascii=False))
        return 0

    if args.find is not None:
        words = norm(args.find).lower().split()
        hits = [it for it in load_json(FEED_PATH, {}).get("items", [])
                if all(w in norm(it["name"]).lower() for w in words)]
        for it in hits:
            print(json.dumps(it, ensure_ascii=False))
        print(f"{len(hits)}件", file=sys.stderr)
        return 0

    if args.file:
        cfg = load_json(SOURCES_PATH, {"sources": []})
        src = next((s for s in cfg["sources"] if s["key"] == args.source), {"key": args.source or "test"})
        with open(args.file, encoding="utf-8") as f:
            body = f.read()
        base = args.base or src.get("link") or (src.get("urls") or ["https://example.com/"])[0]
        items = extract_items(body, base, src, today)
        print(json.dumps(items, ensure_ascii=False, indent=2))
        print(f"{len(items)}件", file=sys.stderr)
        return 0

    feed = collect(today)
    with open(FEED_PATH, "w", encoding="utf-8") as f:
        json.dump(feed, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"収集完了: {len(feed['items'])}件")
    for s in feed["sources"]:
        mark = "OK " if s["ok"] else "NG "
        print(f"  {mark}{s['name']}: {s['count']}件 {s['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
