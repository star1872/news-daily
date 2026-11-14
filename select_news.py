#!/usr/bin/env python3
"""从 Google News RSS 选出最多三条财经口播候选。"""

import json
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parent
TRENDS_URL = "https://trends.google.com/trending/rss?geo=US"
STOP_WORDS = set(
    "a an and as at be by for from has in is it of on or the to was were will with says news live".split()
)
TOPICS = {
    "货币政策": r"\b(fed|fomc|interest rates?|rate cut|rate hike|central bank)\b",
    "宏观数据": r"\b(inflation|cpi|gdp|payrolls|jobs report|unemployment)\b",
    "企业与产业": r"\b(earnings|revenue|guidance|acquisition|merger|antitrust|chips?|semiconductors?)\b",
    "市场与投资": r"\b(stocks?|shares?|bonds?|treasury yields?|futures|markets?|investors?|wall street|ipo|bitcoin|funds?)\b",
    "企业经营": r"\b(profits?|sales|bankruptcy|layoffs|factory|investment|debt|dividends?|buybacks?)\b",
    "贸易与能源": r"\b(tariffs?|sanctions?|export controls?|opec|oil prices?)\b",
}
DEVELOPMENT = re.compile(
    r"\b(announces?|approves?|cuts?|raises?|reports?|beats?|misses?|launches?|"
    r"agrees?|falls?|rises?|surges?|blocks?|halts?|files?|wins?)\b", re.I
)
EVENT_TYPES = {
    "财报": r"\b(earnings|revenue|quarterly results|guidance)\b",
    "并购": r"\b(acquisition|acquires?|merger|takeover)\b",
    "监管": r"\b(antitrust|lawsuit|investigation|settlement)\b",
    "利率": r"\b(rate cut|rate hike|rate decision|holds rates)\b",
    "就业": r"\b(payrolls|unemployment|jobs report)\b",
    "通胀": r"\b(cpi|inflation report|consumer prices)\b",
}
MARKDOWN = re.compile(r"([\\\x60*_{}\[\]()<>#+.!|~&-])")


class RSSExcerpt(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def plain_text(markup):
    parser = RSSExcerpt()
    parser.feed(markup or "")
    parser.close()
    return " ".join(" ".join(parser.parts).split())


def words(title):
    return set(re.findall(r"[a-z0-9]+", title.lower())) - STOP_WORDS


def http_url(value):
    url = urlsplit(value or "")
    return url.scheme in {"https", "http"} and bool(url.hostname)


def publisher_url(value):
    host = urlsplit(value or "").hostname or ""
    return http_url(value) and host not in {"news.google.com", "google.com", "www.google.com"}


def load_trends():
    try:
        with urlopen(TRENDS_URL, timeout=15) as response:
            root = ElementTree.fromstring(response.read(2_000_000))
    except (OSError, ElementTree.ParseError):
        print("Google Trends RSS 不可用，按编辑规则排序。")
        return None

    trends = []
    for item in root.findall("./channel/item"):
        fields = {
            node.tag.rsplit("}", 1)[-1]: (node.text or "").strip()
            for node in item
        }
        articles = []
        for node in item:
            if node.tag.rsplit("}", 1)[-1] != "news_item":
                continue
            article = {
                child.tag.rsplit("}", 1)[-1]: (child.text or "").strip()
                for child in node
            }
            if publisher_url(article.get("news_item_url")):
                articles.append((article.get("news_item_title", ""), article["news_item_url"]))

        volume = re.sub(r"[^0-9]", "", fields.get("approx_traffic", ""))
        if fields.get("title"):
            trends.append({
                "query": fields["title"],
                "volume": int(volume or 0),
                "articles": articles,
            })
    return sorted(trends, key=lambda item: -item["volume"])


def parse_time(value):
    try:
        result = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return result.replace(tzinfo=result.tzinfo or timezone.utc)


def articles_from(data, now):
    result = []
    seen = set()
    for source, group in data["sources"].items():
        for item in group["articles"]:
            url = item.get("url", "")
            published = parse_time(item.get("published"))
            if not http_url(url) or url in seen or not published:
                continue
            if not now - timedelta(hours=48) <= published <= now + timedelta(hours=1):
                continue

            title = plain_text(item.get("title"))
            for suffix in (group["name"], group["name_cn"]):
                if title.casefold().endswith(f" - {suffix}".casefold()):
                    title = title[:-(len(suffix) + 3)]
                    break

            tokens = words(title)
            if len(tokens) < 2:
                continue
            seen.add(url)
            excerpt = plain_text(item.get("summary_zh") or item.get("summary"))
            if excerpt.casefold().startswith(title.casefold()):
                remainder = excerpt[len(title):].strip()
                if remainder.casefold() in {"", group["name"].casefold(), group["name_cn"].casefold()}:
                    excerpt = ""

            result.append({
                "source": source,
                "media": group["name_cn"],
                "title": title,
                "title_zh": item.get("title_zh"),
                "published": published.isoformat(),
                "rss_url": url,
                "publisher_url": url if publisher_url(url) else None,
                "rss_excerpt": excerpt[:450],
                "tokens": tokens,
                "types": {label for label, pattern in EVENT_TYPES.items() if re.search(pattern, title, re.I)},
            })
    return sorted(result, key=lambda item: item["published"], reverse=True)


def same_event(left, right):
    if left["types"] and right["types"] and left["types"].isdisjoint(right["types"]):
        return False
    a, b = left["tokens"], right["tokens"]
    if a == b:
        return True
    overlap = len(a & b)
    return overlap >= 3 and overlap / len(a | b) >= 0.48


def group_events(articles):
    groups = []
    for article in articles:
        for group in groups:
            if all(same_event(article, member) for member in group):
                group.append(article)
                break
        else:
            groups.append([article])
    return groups


def normalized_title(title):
    return " ".join(re.findall(r"[a-z0-9]+", title.casefold()))


def rank_event(group, trends, now):
    all_titles = " ".join(article["title"] for article in group)
    topics = [label for label, pattern in TOPICS.items() if re.search(pattern, all_titles, re.I)]
    development = bool(DEVELOPMENT.search(all_titles))
    number = bool(re.search(r"\d+(?:\.\d+)?\s*(?:%|billion|million|bps|basis points)", all_titles, re.I))
    latest = datetime.fromisoformat(group[0]["published"])
    age = (now - latest).total_seconds() / 3600
    coverage = len({article["source"] for article in group})
    scores = {
        "audience_impact": min(35, 12 + 10 * len(topics)),
        "new_development": (15 if age <= 12 else 9 if age <= 24 else 4) + (10 if development else 0),
        "video_potential": 5 + (8 if number else 0) + (7 if development else 0),
        "source_coverage": min(10, 3 * (coverage - 1)),
        "search_trend": 0,
    }
    matched_trend = None
    for trend in trends or []:
        query_words = words(trend["query"])
        related_titles = {normalized_title(title) for title, _ in trend["articles"] if title}
        exact_headline = any(normalized_title(item["title"]) in related_titles for item in group)
        keyword_match = len(query_words) >= 3 and any(
            query_words <= item["tokens"] for item in group
        )
        if exact_headline or keyword_match:
            matched_trend = {"query": trend["query"], "region": "US", "approx_traffic": trend["volume"]}
            scores["search_trend"] = 10 if trend["volume"] >= 50_000 else 5
            break

    articles = []
    for article in group:
        row = {key: value for key, value in article.items() if key not in {"tokens", "types"}}
        if not row["publisher_url"]:
            title = normalized_title(row["title"])
            links = {
                url for trend in trends or [] for headline, url in trend["articles"]
                if normalized_title(headline) == title
            }
            if len(links) == 1:
                row["publisher_url"] = links.pop()
        articles.append(row)

    reasons = topics + [f"{coverage} 家媒体报道"]
    if development:
        reasons.append("标题包含具体进展")
    if number:
        reasons.append("标题含可核对的量化信息")
    if matched_trend:
        reasons.append("匹配美国 Google 热搜")
    return {
        "title": group[0]["title_zh"] or group[0]["title"],
        "score": sum(scores.values()),
        "scores": scores,
        "reasons": reasons,
        "trend": matched_trend,
        "latest_published": latest.isoformat(),
        "video_angle": (
            f"围绕{'、'.join(topics)}说明最新进展、受影响的对象和下一项待观察的数据。"
            if topics else "交代最新进展，再比较各媒体关注的事实和影响。"
        ),
        "articles": articles,
    }


def md(text):
    return MARKDOWN.sub(r"\\\1", " ".join(text.split()))


def link(url):
    return "<" + url.replace(" ", "%20").replace(">", "%3E").replace("<", "%3C") + ">"


def markdown(data):
    lines = [
        "# 财经口播候选", "",
        f"更新时间（UTC） {data['updated']}", "",
        "每次最多保留三条。报道摘录来自 RSS，制作视频前请查阅报道正文。", "",
    ]
    if not data["trends_available"]:
        lines += ["本次未取得 Google Trends 数据。", ""]
    for index, event in enumerate(data["events"], 1):
        lines += [
            f"## {index}. {md(event['title'])}", "",
            f"**{event['score']} 分** · {'、'.join(event['reasons'])}", "",
            event["video_angle"], "",
        ]
        if event["trend"]:
            trend = event["trend"]
            lines += [f"美国热搜 **{md(trend['query'])}**", ""]
        for article in event["articles"]:
            rss = link(article["rss_url"])
            original = article["publisher_url"]
            links = f"[媒体原文]({link(original)}) · " if original and original != article["rss_url"] else ""
            rss_label = "媒体原文" if original == article["rss_url"] else "Google News 入口"
            lines.append(
                f"- **{md(article['media'])}** · {md(article['title_zh'] or article['title'])}"
                f" · {links}[{rss_label}]({rss})"
            )
            if article["rss_excerpt"]:
                lines.append(f"  - RSS 摘录 · {md(article['rss_excerpt'])}")
        lines.append("")
    return "\n".join(lines)


def main():
    now = datetime.now(timezone.utc)
    data = json.loads((ROOT / "news.json").read_text(encoding="utf-8"))
    articles = articles_from(data, now)
    if not articles:
        raise SystemExit("最近 48 小时没有符合条件的报道，保留上次选题。")

    trends = load_trends()
    groups = [
        group for group in group_events(articles)
        if any(
            re.search(pattern, article["title"], re.I)
            for article in group for pattern in TOPICS.values()
        )
    ]
    if not groups:
        raise SystemExit("最近 48 小时没有匹配财经主题的报道，保留上次选题。")

    events = [rank_event(group, trends, now) for group in groups]
    events.sort(key=lambda item: (item["score"], item["latest_published"]), reverse=True)
    output = {
        "updated": now.isoformat(timespec="seconds"),
        "news_updated": data.get("updated"),
        "trends_available": trends is not None,
        "events": events[:3],
    }
    (ROOT / "top_news.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (ROOT / "top_news.md").write_text(markdown(output) + "\n", encoding="utf-8")
    print(f"从 {len(articles)} 条报道归并出 {len(events)} 个事件，发布 {len(output['events'])} 个候选。")


if __name__ == "__main__":
    main()
