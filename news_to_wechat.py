import html
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import List, Optional

import feedparser
import requests


@dataclass
class NewsItem:
    title: str
    source: str
    published: str
    summary: str
    link: str
    category: str  # "domestic" or "international"
    published_at: datetime


DOMESTIC_FEEDS = [
    "https://www.people.com.cn/rss/politics.xml",
    "https://www.xinhuanet.com/politics/news_politics.xml",
    "https://www.gov.cn/rss/zcxw.xml",
]

INTERNATIONAL_FEEDS = [
    "http://feeds.bbci.co.uk/news/world/rss.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    "https://www.reuters.com/world/rss",
]

# 只保留时政/外交/政策相关内容
KEYWORDS = [
    "时政",
    "政治",
    "外交",
    "政策",
    "政策性",
    "国事",
    "国际关系",
    "峰会",
    "访问",
    "会晤",
    "会谈",
    "联合国",
    "白宫",
    "国务院",
    "外交部",
    "中美",
    "中俄",
    "中欧",
    "中日",
    "制裁",
    "协议",
    "法案",
    "选举",
    "议会",
    "首相",
    "总统",
    "政府",
]


def normalize_text(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_published(entry) -> str:
    for field in ("published", "updated", "created"):
        value = entry.get(field)
        if value:
            try:
                dt = parsedate_to_datetime(value)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone().strftime("%Y-%m-%d %H:%M")
            except Exception:
                return str(value)
    return "未知时间"


def parse_published_at(entry) -> datetime:
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(field)
        if value:
            try:
                return datetime(*value[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    for field in ("published", "updated", "created"):
        value = entry.get(field)
        if value:
            try:
                dt = parsedate_to_datetime(value)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except Exception:
                pass
    return datetime.min.replace(tzinfo=timezone.utc)


def contains_keyword(text: str) -> bool:
    text_lower = text.lower()
    return any(keyword.lower() in text_lower for keyword in KEYWORDS)


def source_name_from_feed(feed_url: str, feed) -> str:
    if feed.get("title"):
        return normalize_text(feed["title"])
    if "people.com.cn" in feed_url:
        return "人民网"
    if "xinhuanet.com" in feed_url:
        return "新华网"
    if "gov.cn" in feed_url:
        return "中国政府网"
    if "bbc" in feed_url:
        return "BBC"
    if "nytimes" in feed_url:
        return "NYTimes"
    if "reuters" in feed_url:
        return "Reuters"
    return feed_url


def fetch_feed_items(feed_url: str, category: str) -> List[NewsItem]:
    parsed = feedparser.parse(feed_url)
    source = source_name_from_feed(feed_url, parsed.feed)
    items: List[NewsItem] = []

    for entry in parsed.entries:
        title = normalize_text(entry.get("title", ""))
        summary = normalize_text(entry.get("summary", "") or entry.get("description", ""))
        link = entry.get("link", "")
        combined = f"{title} {summary}"
        if not title:
            continue
        if not contains_keyword(combined):
            continue

        items.append(
            NewsItem(
                title=title,
                source=source,
                published=parse_published(entry),
                summary=summary[:120] if summary else "无摘要",
                link=link,
                category=category,
                published_at=parse_published_at(entry),
            )
        )

    return items


def deduplicate(items: List[NewsItem]) -> List[NewsItem]:
    seen = set()
    unique = []
    for item in items:
        key = item.title.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def pick_latest(items: List[NewsItem], limit: int) -> List[NewsItem]:
    return sorted(items, key=lambda x: x.published_at, reverse=True)[:limit]


def collect_news() -> List[NewsItem]:
    domestic: List[NewsItem] = []
    international: List[NewsItem] = []

    for feed_url in DOMESTIC_FEEDS:
        domestic.extend(fetch_feed_items(feed_url, "domestic"))

    for feed_url in INTERNATIONAL_FEEDS:
        international.extend(fetch_feed_items(feed_url, "international"))

    domestic = deduplicate(domestic)
    international = deduplicate(international)

    return pick_latest(domestic, 5) + pick_latest(international, 5)


def format_message(items: List[NewsItem]) -> str:
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"时政新闻精选",
        f"生成时间：{date_str}",
        "",
    ]

    domestic = [item for item in items if item.category == "domestic"]
    international = [item for item in items if item.category == "international"]

    if domestic:
        lines.append("【国内新闻】")
        for idx, item in enumerate(domestic, 1):
            lines.append(
                f"{idx}. {item.title}\n"
                f"来源：{item.source}\n"
                f"发布时间：{item.published}\n"
                f"摘要：{item.summary}\n"
            )

    if international:
        lines.append("【国际新闻】")
        for idx, item in enumerate(international, 1):
            lines.append(
                f"{idx}. {item.title}\n"
                f"来源：{item.source}\n"
                f"发布时间：{item.published}\n"
                f"摘要：{item.summary}\n"
            )

    if not items:
        lines.append("今天没有抓取到符合条件的新闻。")

    return "\n".join(lines).strip()


def send_pushplus(title: str, content: str) -> None:
    token = os.getenv("PUSHPLUS_TOKEN", "").strip()
    if not token:
        raise RuntimeError("缺少环境变量 PUSHPLUS_TOKEN")

    url = "https://www.pushplus.plus/send"
    payload = {
        "token": token,
        "title": title,
        "content": content,
        "template": "html",
        "channel": "wechat",
    }
    resp = requests.post(url, json=payload, timeout=20)
    resp.raise_for_status()

    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text}

    if isinstance(data, dict) and data.get("code") not in (None, 200):
        raise RuntimeError(f"PushPlus 返回异常：{data}")


def main() -> None:
    items = collect_news()
    message = format_message(items)
    send_pushplus("每日时政新闻精选", message)
    print(message)


if __name__ == "__main__":
    main()
