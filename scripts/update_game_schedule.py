#!/usr/bin/env python3
"""アプリゲーム公式情報の自動収集スクリプト。

gamesched/sources.json に定義された各アプリについて
  1. 公式YouTubeチャンネルの最新動画（RSS）と配信予定（上映予定のライブ）
  2. 公式サイトのニュース／お知らせ一覧の見出しと日付
を収集し、gamesched/feed.json に書き出す。

GitHub Actions（.github/workflows/update_game_schedule.yml）から1日2回実行される。
標準ライブラリのみで動作する。取得に失敗したソースは status に理由を残し、
他のソースの収集は続行する。
"""

from __future__ import annotations

import datetime as dt
import email.utils
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES_PATH = os.path.join(ROOT, "gamesched", "sources.json")
FEED_PATH = os.path.join(ROOT, "gamesched", "feed.json")

JST = dt.timezone(dt.timedelta(hours=9))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 25
MAX_ITEMS_PER_SOURCE = 12
NEWS_KEEP_DAYS = 120          # これより古い見出しは捨てる
FUTURE_LIMIT_DAYS = 400       # 明らかに誤抽出の未来日付を弾く
NEWS_FUTURE_DAYS = 30         # 一覧の掲載日としてありえない未来日付を弾く

TAG_RE = re.compile(r"<[^>]+>")
ANCHOR_RE = re.compile(r"<a\b[^>]*?href=[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
RSS_LINK_RE = re.compile(
    r"<link[^>]+type=[\"']application/(?:rss|atom)\+xml[\"'][^>]*>", re.I)
HREF_RE = re.compile(r"href=[\"']([^\"']+)[\"']", re.I)

DATE_PATTERNS = [
    re.compile(r"(20\d{2})\s*[./\-年]\s*(\d{1,2})\s*[./\-月]\s*(\d{1,2})(?!\d)"),
    # 「1.5周年」「2.5次元」を日付と誤読しないよう、区切りは / と 月 のみ
    re.compile(r"(?<!\d)(\d{1,2})\s*[/／]\s*(\d{1,2})(?!\d)|(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日"),
]
TIME_RE = re.compile(r"(\d{1,2})\s*[:時]\s*(\d{2})")


# ── 共通ユーティリティ ────────────────────────────────────────────

def fetch(url: str, accept: str = "text/html,application/xhtml+xml,application/xml") -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": accept,
        "Accept-Language": "ja,en;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
        raw = res.read()
    charset = None
    m = re.search(rb"charset=[\"']?([\w\-]+)", raw[:4096], re.I)
    if m:
        charset = m.group(1).decode("ascii", "ignore")
    for enc in filter(None, [charset, "utf-8", "cp932", "euc-jp"]):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(TAG_RE.sub(" ", s))).strip()


def absolute(base: str, href: str) -> str:
    return urllib.parse.urljoin(base, href)


def _build_date(y: int, mo: int, d: int, today: dt.date) -> str | None:
    try:
        date = dt.date(y, mo, d)
    except ValueError:
        return None
    if not (today - dt.timedelta(days=NEWS_KEEP_DAYS)
            <= date <= today + dt.timedelta(days=FUTURE_LIMIT_DAYS)):
        return None
    return date.isoformat()


def parse_date(text: str, today: dt.date, with_year_only: bool = False) -> str | None:
    """テキストから日付を拾って YYYY-MM-DD で返す。年が無ければ推定する。"""
    m = DATE_PATTERNS[0].search(text)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        return _build_date(y, mo, d, today)
    if with_year_only:
        return None
    m = DATE_PATTERNS[1].search(text)
    if not m:
        return None
    nums = [x for x in m.groups() if x]
    mo, d = int(nums[0]), int(nums[1])
    y = today.year
    # 年をまたいだ直後は前年表記の可能性が高い
    if mo - today.month > 6:
        y -= 1
    elif today.month - mo > 6:
        y += 1
    return _build_date(y, mo, d, today)


def parse_rfc_date(raw: str) -> str | None:
    """RSS/Atom の日付（RFC822 / ISO8601）を YYYY-MM-DD に変換する。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    m = re.search(r"(20\d{2})-(\d{2})-(\d{2})", raw)
    if m:
        return m.group(0)
    try:
        return email.utils.parsedate_to_datetime(raw).astimezone(JST).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def parse_time(text: str) -> str | None:
    m = TIME_RE.search(text)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 29 or mi > 59:
        return None
    return f"{h % 24:02d}:{mi:02d}"


# ── YouTube ──────────────────────────────────────────────────────

def resolve_channel_id(handles: list[str]) -> tuple[str, str]:
    """ハンドル（@xxx）からチャンネルIDを解決する。(channel_id, error)"""
    last_err = ""
    for handle in handles:
        handle = handle.strip()
        if not handle:
            continue
        url = handle if handle.startswith("http") else f"https://www.youtube.com/{handle}"
        try:
            body = fetch(url)
        except Exception as exc:  # noqa: BLE001 - 収集失敗は status に記録して続行
            last_err = f"{handle}: {exc}"
            continue
        m = re.search(r'"(?:channelId|externalId)":"(UC[\w\-]{20,})"', body)
        if m:
            return m.group(1), ""
        last_err = f"{handle}: チャンネルIDを抽出できませんでした"
    return "", last_err


def youtube_feed(channel_id: str, app_key: str) -> list[dict]:
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    body = fetch(url, accept="application/atom+xml")
    root = ET.fromstring(body)
    ns = {"a": "http://www.w3.org/2005/Atom", "m": "http://search.yahoo.com/mrss/"}
    items = []
    for entry in root.findall("a:entry", ns)[:MAX_ITEMS_PER_SOURCE]:
        title = (entry.findtext("a:title", default="", namespaces=ns) or "").strip()
        link_el = entry.find("a:link", ns)
        link = link_el.get("href") if link_el is not None else ""
        published = (entry.findtext("a:published", default="", namespaces=ns) or "")[:19]
        if not title or not link:
            continue
        items.append({
            "app": app_key,
            "kind": "youtube",
            "title": title,
            "url": link,
            "date": published[:10],
            "time": "",
            "source_label": "公式YouTube（最新動画）",
            "upcoming": False,
        })
    return items


def _walk(node, found: list):
    """ytInitialData から配信予定（upcomingEventData付き）を再帰的に拾う。"""
    if isinstance(node, dict):
        if "videoId" in node and "upcomingEventData" in node:
            found.append(node)
        for value in node.values():
            _walk(value, found)
    elif isinstance(node, list):
        for value in node:
            _walk(value, found)


def youtube_upcoming(channel_id: str, app_key: str) -> list[dict]:
    body = fetch(f"https://www.youtube.com/channel/{channel_id}/streams")
    m = re.search(r"ytInitialData\s*=\s*(\{.*?\})\s*;\s*</script>", body, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []
    found: list = []
    _walk(data, found)
    items, seen = [], set()
    for node in found:
        vid = node.get("videoId")
        if not vid or vid in seen:
            continue
        seen.add(vid)
        start = (node.get("upcomingEventData") or {}).get("startTime")
        title = ""
        title_node = node.get("title") or {}
        if isinstance(title_node, dict):
            runs = title_node.get("runs") or []
            if runs:
                title = runs[0].get("text", "")
            title = title or title_node.get("simpleText", "")
        if not start or not title:
            continue
        try:
            when = dt.datetime.fromtimestamp(int(start), JST)
        except (ValueError, OSError):
            continue
        items.append({
            "app": app_key,
            "kind": "youtube",
            "title": title,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "date": when.strftime("%Y-%m-%d"),
            "time": when.strftime("%H:%M"),
            "source_label": "公式YouTube（配信予定）",
            "upcoming": True,
        })
    return items[:MAX_ITEMS_PER_SOURCE]


# ── ニュース一覧 ─────────────────────────────────────────────────

def discover_feed(body: str, base: str) -> str | None:
    m = RSS_LINK_RE.search(body)
    if not m:
        return None
    href = HREF_RE.search(m.group(0))
    return absolute(base, html.unescape(href.group(1))) if href else None


def parse_feed_xml(body: str, app_key: str, label: str, today: dt.date) -> list[dict]:
    root = ET.fromstring(body)
    items = []
    # RSS 2.0
    for item in root.iter():
        tag = item.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        get = lambda name: next(  # noqa: E731
            (c.text for c in item if c.tag.split("}")[-1] == name and c.text), "")
        title = (get("title") or "").strip()
        link = (get("link") or "").strip()
        if not link:
            for c in item:
                if c.tag.split("}")[-1] == "link" and c.get("href"):
                    link = c.get("href")
                    break
        raw_date = get("pubDate") or get("published") or get("updated") or get("date") or ""
        date = parse_rfc_date(raw_date) or parse_date(raw_date, today) or ""
        if not title or not link:
            continue
        items.append({
            "app": app_key,
            "kind": "news",
            "title": title,
            "url": link,
            "date": date or "",
            "time": parse_time(title) or "",
            "source_label": label,
            "upcoming": False,
        })
        if len(items) >= MAX_ITEMS_PER_SOURCE:
            break
    return items


SKIP_TEXT = {"", "TOP", "トップ", "一覧", "もっと見る", "next", "prev", "前へ", "次へ",
             "お知らせ", "ニュース", "ホーム", "詳細", "詳しくはこちら"}
CAT_WORDS = "お知らせ|イベント|アップデート|キャンペーン|ニュース|その他|重要|メンテナンス|ガチャ"
LEAD_RE = re.compile(r"^\s*(CHECK|NEW|新着|PICKUP)\s+", re.I)
LEAD_DATE_RE = re.compile(r"^\s*(20\d{2}\s*[./\-年]\s*)?\d{1,2}\s*[./\-月]\s*\d{1,2}\s*日?"
                          r"\s*(\([月火水木金土日]\))?\s*")
TAIL_RE = re.compile(r"\s*(?:" + CAT_WORDS + r")?\s*20\d{2}\s*[./\-年]\s*\d{1,2}\s*[./\-月]\s*\d{1,2}\s*日?"
                     r"\s*(?:NEW|新着)?\s*$", re.I)


def clean_title(text: str) -> str:
    """一覧のリンク文字列から、日付・カテゴリ・NEWバッジを取り除く。"""
    out = TAIL_RE.sub("", text).strip()
    out = LEAD_DATE_RE.sub("", out).strip()
    out = LEAD_RE.sub("", out).strip()
    return out or text


def parse_news_html(body: str, base: str, app_key: str, label: str,
                    today: dt.date) -> list[dict]:
    """一覧ページから「日付＋見出し」の組を総当たりで拾う汎用パーサ。"""
    items, seen = [], set()
    for m in ANCHOR_RE.finditer(body):
        href, inner = m.group(1), m.group(2)
        text = strip_tags(inner)
        if text in SKIP_TEXT or len(text) < 6 or len(text) > 140:
            continue
        # 直前の見出し（掲載日）だけを見るため、ひとつ前の項目の終了タグで切る
        seg = body[max(0, m.start() - 400):m.start()]
        cut = max(seg.rfind("</li>"), seg.rfind("</a>"), seg.rfind("</article>"))
        if cut >= 0:
            seg = seg[cut:]
        window = strip_tags(seg)[-120:]
        date = (parse_date(window, today, with_year_only=True)
                or parse_date(text, today, with_year_only=True)
                or parse_date(window, today)
                or parse_date(text, today))
        if not date or date > (today + dt.timedelta(days=NEWS_FUTURE_DAYS)).isoformat():
            continue
        url = absolute(base, html.unescape(href))
        if url in seen or url.startswith("javascript"):
            continue
        seen.add(url)
        clean = clean_title(text)
        items.append({
            "app": app_key,
            "kind": "news",
            "title": clean or text,
            "url": url,
            "date": date,
            "time": parse_time(text) or "",
            "source_label": label,
            "upcoming": False,
        })
    items.sort(key=lambda x: x["date"], reverse=True)
    return items[:MAX_ITEMS_PER_SOURCE]


# ── メイン ───────────────────────────────────────────────────────

def collect() -> dict:
    with open(SOURCES_PATH, encoding="utf-8") as f:
        config = json.load(f)
    today = dt.datetime.now(JST).date()
    items: list[dict] = []
    status: list[dict] = []

    for app in config.get("apps", []):
        key, short = app["key"], app.get("short", app["key"])

        # --- YouTube ---
        yt = app.get("youtube") or {}
        channel_id = (yt.get("channel_id") or "").strip()
        resolve_error = ""
        if not channel_id:
            channel_id, resolve_error = resolve_channel_id(yt.get("handles") or [])
        if channel_id:
            for label, fn in (("公式YouTube（最新動画）", youtube_feed),
                              ("公式YouTube（配信予定）", youtube_upcoming)):
                try:
                    got = fn(channel_id, key)
                    items.extend(got)
                    status.append({"app": key, "label": f"{short} / {label}",
                                   "url": f"https://www.youtube.com/channel/{channel_id}",
                                   "ok": True, "count": len(got), "error": ""})
                except Exception as exc:  # noqa: BLE001
                    status.append({"app": key, "label": f"{short} / {label}",
                                   "url": f"https://www.youtube.com/channel/{channel_id}",
                                   "ok": False, "count": 0, "error": str(exc)[:200]})
        else:
            status.append({"app": key, "label": f"{short} / 公式YouTube", "url": "",
                           "ok": False, "count": 0,
                           "error": resolve_error or "channel_id が未設定です"})

        # --- 公式サイトのニュース ---
        for src in app.get("news", []):
            src_label = src.get("label", "ニュース")
            label = f"{short} / {src_label}"
            candidates = src.get("urls") or [src["url"]]
            match = re.compile(src["match"]) if src.get("match") else None
            got: list[dict] = []
            used, errors = "", []
            for url in candidates:
                try:
                    body = fetch(url)
                except Exception as exc:  # noqa: BLE001 - 次の候補URLへ
                    errors.append(f"{url}: {exc}")
                    continue
                feed_url = discover_feed(body, url)
                found: list[dict] = []
                if feed_url:
                    try:
                        found = parse_feed_xml(fetch(feed_url, accept="application/rss+xml"),
                                               key, src_label, today)
                    except Exception:  # noqa: BLE001 - RSSが壊れていればHTMLへ
                        found = []
                if not found:
                    found = parse_news_html(body, url, key, src_label, today)
                if match:
                    found = [x for x in found if match.search(x["title"])]
                used = url
                if found:
                    got = found
                    break
            items.extend(got)
            status.append({"app": key, "label": label, "url": used or (candidates[0] if candidates else ""),
                           "ok": bool(used), "count": len(got),
                           "error": "" if used else "; ".join(errors)[:200]})

    # URL重複を排除（配信予定を優先して残す）
    merged: dict[str, dict] = {}
    for item in items:
        prev = merged.get(item["url"])
        if prev is None or (item["upcoming"] and not prev["upcoming"]):
            merged[item["url"]] = item
    result = sorted(merged.values(),
                    key=lambda x: (x["date"] or "0000-00-00", x["time"]), reverse=True)

    return {
        "generated_at": dt.datetime.now(JST).isoformat(timespec="seconds"),
        "items": result,
        "status": status,
    }


def main() -> int:
    feed = collect()
    with open(FEED_PATH, "w", encoding="utf-8") as f:
        json.dump(feed, f, ensure_ascii=False, indent=2)
        f.write("\n")
    ok = sum(1 for s in feed["status"] if s["ok"])
    print(f"収集完了: {len(feed['items'])}件 / ソース成功 {ok}/{len(feed['status'])}")
    for s in feed["status"]:
        mark = "OK " if s["ok"] else "NG "
        print(f"  {mark}{s['label']}: {s['count']}件 {s['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
