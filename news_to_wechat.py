#!/usr/bin/env python3
"""
超级日报（彻底修复版）：农历修复+全分类保底，爬虫+聚合+缓存
智谱AI翻译+摘要，6分类每类5条，绝不空推
"""

import os, sys, time, hashlib, logging, requests, feedparser, re, json, subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from typing import List, Dict, Set
from bs4 import BeautifulSoup
from zhdate import ZhDate

# ---------- 配置 ----------
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "")
TZ_BEIJING = timezone(timedelta(hours=8))
TODAY = datetime.now(TZ_BEIJING).strftime("%Y-%m-%d")
MAX_WORKERS = 12
REQUEST_TIMEOUT = 15

CACHE_DIR = "cache"
CACHE_FILE = os.path.join(CACHE_DIR, "pushed_hashes.json")

# 智谱AI
ENABLE_AI = os.environ.get("ENABLE_AI_SUMMARY", "true").lower() == "true"
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
LLM_MODEL = os.environ.get("LLM_MODEL", "glm-4-flash")

logging.basicConfig(level=logging.INFO, stream=sys.stdout, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# ---------- RSS 源（大幅扩充）----------
RSS_FEEDS = [
    # ===== 国内新闻（多重保障）=====
    {"url": "http://www.xinhuanet.com/politics/xhsll.xml", "category": "国内"},
    {"url": "http://www.people.com.cn/rss/politics.xml", "category": "国内"},
    {"url": "https://www.chinanews.com/rss/rss_1.html", "category": "国内"},
    {"url": "https://www.thepaper.cn/rss_news_1.xml", "category": "国内"},
    {"url": "https://news.sina.com.cn/rss/1.xml", "category": "国内"},
    {"url": "https://news.163.com/special/002341KK/rss_news.xml", "category": "国内"},
    # 百度聚合（国内综合）
    {"url": "https://news.baidu.com/ns?word=%E4%B8%AD%E5%9B%BD+%E7%BB%8F%E6%B5%8E+%E6%B0%91%E7%94%9F&tn=newsrss&sr=0&cl=2&rn=50&ct=0", "category": "国内"},
    {"url": "https://news.baidu.com/ns?word=%E6%8A%96%E9%9F%B3+%E7%83%AD%E6%90%9C&tn=newsrss&sr=0&cl=2&rn=50&ct=0", "category": "国内"},
    {"url": "https://news.baidu.com/ns?word=%E5%BE%AE%E5%8D%9A%E7%83%AD%E6%90%9C&tn=newsrss&sr=0&cl=2&rn=50&ct=0", "category": "国内"},

    # ===== 国际新闻 =====
    {"url": "https://feeds.bbci.co.uk/news/world/rss.xml", "category": "国际"},
    {"url": "https://www.chinadaily.com.cn/rss/world_rss.xml", "category": "国际"},
    {"url": "https://news.baidu.com/ns?word=%E5%9B%BD%E9%99%85+%E7%BE%8E%E5%9B%BD+%E6%AC%A7%E6%B4%B2&tn=newsrss&sr=0&cl=2&rn=50&ct=0", "category": "国际"},

    # ===== 湖北武汉本地 =====
    {"url": "http://www.cnhubei.com/rss/whxw.xml", "category": "武汉"},
    {"url": "http://hb.people.com.cn/rss/hubei.xml", "category": "武汉"},
    {"url": "http://www.hb.xinhuanet.com/rss/wh.xml", "category": "武汉"},  # 新增
    {"url": "https://news.baidu.com/ns?word=%E6%AD%A6%E6%B1%89+%E6%B9%96%E5%8C%97+%E5%9C%B0%E9%93%81+%E5%A4%A9%E6%B0%94&tn=newsrss&sr=0&cl=2&rn=50&ct=0", "category": "武汉"},

    # ===== AI 影响 =====
    {"url": "https://www.36kr.com/feed", "category": "AI"},
    {"url": "https://www.huxiu.com/rss/0.html", "category": "AI"},
    {"url": "https://news.baidu.com/ns?word=%E4%BA%BA%E5%B7%A5%E6%99%BA%E8%83%BD+AI+%E5%A4%A7%E6%A8%A1%E5%9E%8B&tn=newsrss&sr=0&cl=2&rn=50&ct=0", "category": "AI"},

    # ===== 便利店行业 =====
    {"url": "https://news.baidu.com/ns?word=%E4%BE%BF%E5%88%A9%E5%BA%97+%E9%9B%B6%E5%94%AE+%E5%BF%AB%E6%B6%88&tn=newsrss&sr=0&cl=2&rn=50&ct=0", "category": "便利店"},
    {"url": "https://news.baidu.com/ns?word=%E7%BD%97%E6%A3%AE+%E5%85%A8%E5%AE%B6+7-11&tn=newsrss&sr=0&cl=2&rn=50&ct=0", "category": "便利店"},

    # ===== 股票行业情报 =====
    {"url": "https://news.baidu.com/ns?word=%E8%82%A1%E5%B8%82+%E9%9B%B6%E5%94%AE+%E6%B6%88%E8%B4%B9&tn=newsrss&sr=0&cl=2&rn=50&ct=0", "category": "股票"},
    # 东方财富RSS（示例，可能需替换为实际可用地址）
    {"url": "https://rss.eastmoney.com/fav/10.xml", "category": "股票"},
    {"url": "https://news.baidu.com/ns?word=A%E8%82%A1+%E4%B8%8A%E8%AF%81%E6%8C%87%E6%95%B0&tn=newsrss&sr=0&cl=2&rn=50&ct=0", "category": "股票"},
]

# ========== 爬虫目标（优化选择器）==========
SCRAPE_TARGETS = [
    # 便利店：联商网（尝试更通用的选择器）
    {"url": "http://www.linkshop.com/news/", "category": "便利店",
     "title_selector": "a[href*='/news/']", "link_attr": "href", "base": "http://www.linkshop.com",
     "text_filter": "新闻"},
    # 武汉政府公告
    {"url": "http://www.wuhan.gov.cn/sy/whyw/", "category": "武汉",
     "title_selector": "a[href*='content']", "link_attr": "href", "base": "http://www.wuhan.gov.cn"},
]

# ========== 关键词评分 ==========
KEYWORD_SCORES = {
    "国内新闻": {"中国":10,"国内":10,"经济":5,"就业":5,"政策":5,"社会":4},
    "国际新闻": {"美国":10,"欧洲":10,"俄罗斯":10,"中东":10,"美联储":5,"汇率":5},
    "湖北武汉本地动态": {"武汉":10,"湖北":10,"地铁":5,"施工":5,"天气":5,"暴雨":5},
    "AI对普通人的影响": {"AI":10,"人工智能":10,"大模型":5,"ChatGPT":10,"职业":5},
    "便利店行业动态": {"便利店":10,"零售":8,"快消":8,"罗森":10,"7-11":10,"加盟":5},
    "股票行业情报": {"A股":10,"上证":10,"指数":8,"券商":5,"零售":5,"消费":5,"研报":5},
}

FALLBACK_KEYWORDS = {
    "国内新闻": ["中国","国内","经济","政策"],
    "国际新闻": ["美国","欧洲","国际","全球"],
    "湖北武汉本地动态": ["武汉","湖北"],
    "AI对普通人的影响": ["人工智能","AI","智能"],
    "便利店行业动态": ["零售","便利店","超市"],
    "股票行业情报": ["股市","A股","股票"],
}

# ========== 缓存 ==========
def load_pushed_hashes() -> Set[str]:
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "r") as f:
                data = json.load(f)
                return {h for h in data if h.endswith(f"@{TODAY}")}
    except: pass
    return set()

def save_pushed_hashes(new_hashes: Set[str]):
    os.makedirs(CACHE_DIR, exist_ok=True)
    old = set()
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r") as f:
            old = set(json.load(f))
    old = {h for h in old if h.endswith(f"@{TODAY}")}
    old.update(new_hashes)
    with open(CACHE_FILE, "w") as f:
        json.dump(list(old), f)

def commit_cache():
    try:
        subprocess.run(["git", "config", "user.email", "actions@github.com"], check=False)
        subprocess.run(["git", "config", "user.name", "GitHub Actions"], check=False)
        subprocess.run(["git", "add", CACHE_FILE], check=False)
        r = subprocess.run(["git", "diff", "--cached", "--quiet"], capture_output=True)
        if r.returncode != 0:
            subprocess.run(["git", "commit", "-m", "Update pushed cache [skip ci]"], check=False)
            subprocess.run(["git", "push"], check=False)
            logger.info("缓存已更新")
    except Exception as e:
        logger.warning(f"缓存提交失败: {e}")

# ========== 爬虫 ==========
def scrape_website(target: Dict) -> List[Dict]:
    news = []
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(target["url"], headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        elements = soup.select(target["title_selector"])[:20]
        for el in elements:
            title = el.get_text().strip()
            if not title or len(title) < 4:
                continue
            if "text_filter" in target and target["text_filter"] not in title:
                continue
            link = el.get(target["link_attr"], "")
            if link and target.get("base") and not link.startswith("http"):
                link = target["base"].rstrip("/") + "/" + link.lstrip("/")
            news.append({
                "title": title,
                "url": link,
                "summary": "",
                "source": target["url"].split("//")[-1].split("/")[0],
                "time": datetime.now(TZ_BEIJING).strftime("%Y-%m-%d %H:%M"),
                "category": target["category"],
            })
        logger.info(f"爬虫 {target['url']} 获取 {len(news)} 条")
    except Exception as e:
        logger.debug(f"爬虫失败 {target['url']}: {e}")
    return news

# ========== RSS抓取 ==========
def fetch_rss(feed_info: Dict) -> List[Dict]:
    news = []
    try:
        url = feed_info["url"]
        sep = "?" if "?" not in url else "&"
        full_url = f"{url}{sep}_t={int(time.time())}"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(full_url, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        # 财联社API特殊处理
        if "cls.cn" in url:
            try:
                data = resp.json()
                articles = data.get("data", {}).get("roll_data", [])[:50]
                for art in articles:
                    title = art.get("title", "")
                    ctime = art.get("ctime", 0)
                    if ctime:
                        pub_time = datetime.fromtimestamp(ctime, tz=TZ_BEIJING)
                        if pub_time.strftime("%Y-%m-%d") != TODAY: continue
                        time_str = pub_time.strftime("%Y-%m-%d %H:%M")
                        summary = re.sub(r"<[^>]+>", "", art.get("brief", ""))[:200]
                        news.append({"title":title,"summary":summary,"source":"cls.cn","time":time_str,
                                     "category":feed_info["category"],"url":art.get("url","")})
            except: pass
        else:
            feed = feedparser.parse(resp.content)
            for entry in feed.entries:
                pub_time = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    pub_time = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).astimezone(TZ_BEIJING)
                elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                    pub_time = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc).astimezone(TZ_BEIJING)
                if not pub_time: continue
                time_str = pub_time.strftime("%Y-%m-%d %H:%M")
                if TODAY not in time_str: continue
                title = entry.get("title","").strip()
                summary = re.sub(r"<[^>]+>", "", entry.get("summary",""))[:300].strip()
                news.append({"title":title,"summary":summary,
                             "source":url.split("//")[-1].split("/")[0],
                             "time":time_str,"category":feed_info["category"],
                             "url":entry.get("link","")})
    except Exception as e:
        logger.debug(f"源 {url[:60]} 失败: {e}")
    return news

def collect_all_news() -> Dict[str, List[Dict]]:
    pool = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(fetch_rss, f): f for f in RSS_FEEDS}
        for future in as_completed(futures):
            feed_info = futures[future]
            try:
                result = future.result()
                pool.setdefault(feed_info["category"], []).extend(result)
            except Exception as e:
                logger.warning(f"任务异常: {e}")
    # 爬虫
    for target in SCRAPE_TARGETS:
        items = scrape_website(target)
        pool.setdefault(target["category"], []).extend(items)
    return pool

# ========== 去重与精选 ==========
def generate_hash(item: Dict) -> str:
    raw = item["title"] + item.get("url", "")
    return hashlib.md5(raw.encode()).hexdigest() + f"@{TODAY}"

def select_with_fallback(category_name: str, all_news: List[Dict], pushed_hashes: Set[str], global_news: List[Dict], target=5) -> List[Dict]:
    sorted_news = sorted(all_news, key=lambda x: x["time"], reverse=True)
    new_news = [item for item in sorted_news if generate_hash(item) not in pushed_hashes]

    if len(new_news) >= target:
        candidate = new_news
    else:
        # 忽略缓存
        candidate = sorted_news
        logger.info(f"{category_name} 新内容不足，忽略缓存")

    if not candidate and global_news:
        # 从全局新闻中借用
        logger.info(f"{category_name} 无本地新闻，从全局借用")
        candidate = sorted(global_news, key=lambda x: x["time"], reverse=True)

    scores = KEYWORD_SCORES.get(category_name, {})
    for item in candidate:
        item["_score"] = sum(v for k, v in scores.items() if k in item["title"])
    ranked = sorted(candidate, key=lambda x: (x["_score"], x["time"]), reverse=True)
    selected = ranked[:target]

    if len(selected) < target and global_news:
        supplement = [item for item in global_news if item not in selected]
        selected += sorted(supplement, key=lambda x: x["time"], reverse=True)[:target-len(selected)]

    return selected[:target]

# ========== 翻译与AI ==========
def is_english(text: str) -> bool:
    if not text: return False
    for ch in text:
        if '\u4e00' <= ch <= '\u9fff': return False
    return True

def translate(text: str) -> str:
    if not LLM_API_KEY: return text
    try:
        prompt = f"将以下英文翻译成中文，只返回翻译结果：\n{text}"
        headers = {"Authorization": f"Bearer {LLM_API_KEY}"}
        data = {"model": LLM_MODEL, "messages": [{"role":"user","content":prompt}], "temperature":0.1}
        resp = requests.post(f"{LLM_BASE_URL}/chat/completions", json=data, headers=headers, timeout=15)
        if resp.status_code != 200: return text
        return resp.json()["choices"][0]["message"]["content"].strip()
    except: return text

def translate_all(sections: Dict[str, List[Dict]]):
    for items in sections.values():
        for item in items:
            if is_english(item["title"]):
                item["title"] = translate(item["title"])
            if is_english(item["summary"]):
                item["summary"] = translate(item["summary"])

def ai_summary(sections: Dict[str, List[Dict]]) -> Dict:
    if not ENABLE_AI or not LLM_API_KEY: return {"intros":{}, "motto":""}
    try:
        all_titles = [f"[{sec}] {item['title']}" for sec, items in sections.items() for item in items]
        prompt = (
            "你是新闻主编。根据以下标题，为六个分类各生成一句15字内概括，并生成一句50字内正能量微语。"
            "分类：国内新闻、国际新闻、湖北武汉本地动态、AI对普通人的影响、便利店行业动态、股票行业情报。"
            "返回JSON：{\"intros\":{\"国内新闻\":\"\",\"国际新闻\":\"\",\"湖北武汉本地动态\":\"\",\"AI对普通人的影响\":\"\",\"便利店行业动态\":\"\",\"股票行业情报\":\"\"},\"motto\":\"\"}"
            "\n标题：\n" + "\n".join(all_titles)
        )
        headers = {"Authorization": f"Bearer {LLM_API_KEY}"}
        data = {"model": LLM_MODEL, "messages": [{"role":"user","content":prompt}], "temperature":0.7,
                "response_format":{"type":"json_object"}}
        resp = requests.post(f"{LLM_BASE_URL}/chat/completions", json=data, headers=headers, timeout=25)
        if resp.status_code == 200:
            return json.loads(resp.json()["choices"][0]["message"]["content"])
    except Exception as e:
        logger.error(f"AI摘要失败: {e}")
    return {"intros":{}, "motto":""}

def get_lunar_str() -> str:
    # 关键修复：使用 naive datetime
    return ZhDate.from_datetime(datetime.now()).chinese()

def format_daily(sections: Dict[str, List[Dict]], ai_extra: Dict) -> str:
    now = datetime.now()
    weekday = ["星期一","星期二","星期三","星期四","星期五","星期六","星期日"][now.weekday()]
    date_str = now.strftime("%Y年%m月%d日")
    header = f"{date_str}日报，{weekday}，农历{get_lunar_str()}，工作愉快，生活喜乐！\n"
    order = ["国内新闻","国际新闻","湖北武汉本地动态","AI对普通人的影响","便利店行业动态","股票行业情报"]
    body = ""
    for sec in order:
        items = sections.get(sec, [])
        if not items:
            body += f"\n【{sec}】今日暂无更新\n"
            continue
        intro = ai_extra.get("intros",{}).get(sec,"")
        body += f"\n━━━━━━ 【{sec}】━━━━━━\n"
        if intro: body += f"📌 {intro}\n"
        for i, item in enumerate(items, 1):
            body += f"{i}. {item['title']}\n"
        body += "\n"
    motto = ai_extra.get("motto","")
    if motto: body += f"【微语】{motto}\n"
    return header + body

def push_wechat(content: str):
    if not PUSHPLUS_TOKEN: raise RuntimeError("缺 PUSHPLUS_TOKEN")
    requests.post("http://www.pushplus.plus/send", json={
        "token": PUSHPLUS_TOKEN, "title": "每日日报", "content": content, "template": "txt"
    }, timeout=15).raise_for_status()

def main():
    pushed_hashes = load_pushed_hashes()
    logger.info(f"已加载今日推送哈希 {len(pushed_hashes)} 条")

    raw = collect_all_news()
    section_map = {
        "国内": "国内新闻", "国际": "国际新闻", "武汉": "湖北武汉本地动态",
        "AI": "AI对普通人的影响", "便利店": "便利店行业动态", "股票": "股票行业情报"
    }
    pool = {v:[] for v in section_map.values()}
    all_global = []
    for cat, news in raw.items():
        sec = section_map.get(cat)
        if sec:
            pool[sec].extend(news)
            all_global.extend(news)

    new_hashes = set()
    final = {}
    for sec_name, sec_news in pool.items():
        selected = select_with_fallback(sec_name, sec_news, pushed_hashes, all_global)
        final[sec_name] = selected
        for item in selected:
            new_hashes.add(generate_hash(item))
        logger.info(f"{sec_name}: 推送 {len(selected)} 条")

    translate_all(final)
    ai_extra = ai_summary(final)
    msg = format_daily(final, ai_extra)
    push_wechat(msg)

    save_pushed_hashes(new_hashes)
    commit_cache()

if __name__ == "__main__":
    main()
