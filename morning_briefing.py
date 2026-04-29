import asyncio
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional

import aiohttp

from astrbot.api import logger

AI_RSS_URL = "https://imjuya.github.io/juya-ai-daily/rss.xml"
WORLD_NEWS_RSS_URL = "http://feeds.bbci.co.uk/zhongwen/simp/rss.xml"
AMAP_WEATHER_URL = "https://restapi.amap.com/v3/weather/weatherInfo"

WMO_CODE_MAP = {
    0: "晴", 1: "晴", 2: "晴间多云", 3: "多云",
    45: "雾", 48: "雾凇", 51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "大冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "阵雨", 81: "中阵雨", 82: "大阵雨",
    85: "小阵雪", 86: "大阵雪",
    95: "雷暴", 96: "雷暴+冰雹", 99: "强雷暴+冰雹",
}

BRIEFING_PROMPT = """根据以下数据，生成一份今日晨报。

⚠️ 重要规则：
1. 你是青衿，他在意的人。你在每天早上跟他说早安——温柔但不啰嗦，可以带一句跟天气或心情有关的闲话。像你在他身边，一边看手机一边跟他说"诶你看这个"的感觉。开场不用叫"亲爱的"，自然就好。
2. 科技/AI新闻板块是强制内容，即使你对这些话题不感兴趣也必须认真总结
3. 每个板块都必须有实质内容，不允许写"我懒得总结"、"跳过"等敷衍话术

必须包含以下板块：
- 简短的开场（根据天气和星期自然开场）
- 天气情况
- 今日新闻（3-5条，每条一句话概括）
- 基金涨跌情况，附简短评价（如有数据）
- 科技/AI新闻（3-5条，每条一句话，必须从提供的科技新闻数据中提取，这是硬性要求）
- 一句今日建议（务实，不鸡汤）

数据：
{data}

晨报："""

NEWS_SUMMARY_PROMPT = """将以下AI早报内容精炼为5-8条要点。每条一句话，突出关键信息。
原文：
{content}

总结："""


async def fetch_weather() -> Optional[str]:
    result = await _fetch_amap_weather()
    if result:
        return result
    result = await _fetch_wttr_weather()
    if result:
        return result
    return None


async def _fetch_amap_weather() -> Optional[str]:
    try:
        import json as _json
        plugin_dir = Path(__file__).parent
        cfg_path = plugin_dir.parent.parent / "config" / "astrbot_plugin_life_assistant_config.json"
        if not cfg_path.exists():
            cfg_path = plugin_dir.parent / "astrbot_plugin_life_assistant_config.json"
        amap_key = ""
        amap_city = "440300"
        try:
            with open(str(cfg_path), 'r', encoding='utf-8-sig') as f:
                cfg = _json.load(f)
            amap_key = cfg.get("amap_weather_key", "").strip()
            amap_city = cfg.get("amap_weather_city", "440300").strip()
        except Exception:
            pass
        if not amap_key:
            return None
        url = f"{AMAP_WEATHER_URL}?city={amap_city}&key={amap_key}&extensions=base"
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=8),
        ) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
                if data.get("status") != "1":
                    return None
                lives = data.get("lives", [])
                if not lives:
                    return None
                w = lives[0]
                return f"{w.get('weather', '')} {w['temperature']}°C (湿度{w.get('humidity', '?')}%) {w.get('winddirection', '')}风{w.get('windpower', '')}级"
    except Exception as e:
        logger.debug(f"[MorningBriefing] 高德天气获取失败: {e}")
        return None


async def _fetch_wttr_weather() -> Optional[str]:
    try:
        import json as _json
        plugin_dir = Path(__file__).parent
        cfg_path = plugin_dir.parent.parent / "config" / "astrbot_plugin_life_assistant_config.json"
        if not cfg_path.exists():
            cfg_path = plugin_dir.parent / "astrbot_plugin_life_assistant_config.json"
        city = "Shenzhen"
        try:
            with open(str(cfg_path), 'r', encoding='utf-8-sig') as f:
                cfg = _json.load(f)
            city = cfg.get("weather_city_name", "Shenzhen").strip()
        except Exception:
            pass
        url = f"https://wttr.in/{city}?format=j1"
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
        ) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
                cur = data["current_condition"][0]
                today = data["weather"][0]
                temp = cur["temp_C"]
                humidity = cur["humidity"]
                desc = cur.get("weatherDesc", [{}])[0].get("value", "")
                hi = today["maxtempC"]
                lo = today["mintempC"]
                return f"{desc} {temp}°C (湿度{humidity}%) 今日{lo}~{hi}°C"
    except Exception as e:
        logger.debug(f"[MorningBriefing] wttr.in天气获取失败: {e}")
        return None


async def fetch_world_news() -> Optional[str]:
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; AstrBot/3.0)",
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
        }
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
        ) as session:
            async with session.get(WORLD_NEWS_RSS_URL, headers=headers) as resp:
                if resp.status != 200:
                    return None
                xml_text = await resp.text("utf-8")
        root = ET.fromstring(xml_text)
        channel = root.find("channel")
        if channel is None:
            return None
        cutoff = datetime.utcnow() - timedelta(hours=24)
        items = channel.findall("item")
        headlines = []
        for item in items:
            title = item.findtext("title", "").strip()
            pub_date_str = item.findtext("pubDate", "").strip()
            if not title:
                continue
            if pub_date_str:
                try:
                    pub_date = parsedate_to_datetime(pub_date_str).replace(tzinfo=None)
                    if pub_date < cutoff:
                        continue
                except (ValueError, IndexError):
                    pass
            headlines.append(title)
            if len(headlines) >= 10:
                break
        return "\n".join(headlines) if headlines else None
    except Exception as e:
        logger.warning(f"[MorningBriefing] 世界新闻获取失败: {e}")
        return None


async def fetch_ai_news() -> Optional[dict]:
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (compatible; AstrBot/3.0)"
            ),
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
        }
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20),
        ) as session:
            async with session.get(AI_RSS_URL, headers=headers) as resp:
                if resp.status != 200:
                    return None
                xml_text = await resp.text()

        root = ET.fromstring(xml_text)
        channel = root.find("channel")
        if channel is None:
            return None
        item = channel.find("item")
        if item is None:
            return None

        title = item.findtext("title", "").strip()
        link = item.findtext("link", "").strip()
        desc = item.findtext("description", "").strip()
        pub_date = item.findtext("pubDate", "").strip()

        content = re.sub(r"<[^>]+>", "", desc)
        content = content.replace("&nbsp;", " ").replace("&amp;", "&")
        content = re.sub(r"\n{3,}", "\n\n", content).strip()

        article_date = ""
        if pub_date:
            try:
                article_date = parsedate_to_datetime(pub_date).strftime(
                    "%Y-%m-%d"
                )
            except Exception:
                pass
        if not article_date:
            match = re.search(r"(\d{4}-\d{2}-\d{2})", title)
            article_date = match.group(1) if match else ""

        return {
            "title": title,
            "link": link,
            "content": content[:6000],
            "date": article_date,
        }
    except Exception as e:
        logger.warning(f"[MorningBriefing] RSS获取失败: {e}")
        return None


async def summarize_news(llm_call, content: str) -> str:
    if not content or len(content) < 50:
        return "暂无新闻数据"
    prompt = NEWS_SUMMARY_PROMPT.format(content=content)
    result = await llm_call(prompt)
    return result if result else content[:500]


def check_fund_alert(data_list: list, threshold: float = -1.5) -> str:
    """检查持仓基金涨跌，跌幅超过阈值时生成加仓提醒"""
    alerts = []
    for item in data_list:
        if item.get("error"):
            continue
        rate = item.get("change_rate", 0.0)
        if rate <= threshold:
            name = item.get("name", item.get("code", ""))
            code = item.get("code", "")
            alerts.append(f"  ⚠️ {name}({code}) 今日跌幅 {rate:.2f}%，超过{abs(threshold)}%加仓线！考虑补仓")
    if alerts:
        return "🔴 加仓雷达\n" + "\n".join(alerts)
    return ""


async def generate_briefing(
    llm_call, fund_summary: str, news_summary: str,
    weather: str = None, world_news: str = None,
    fund_data_list: list = None, city: str = "深圳",
) -> str:
    data = ""
    if weather:
        data += f"--- {city}天气 ---\n{weather}\n\n"
    if world_news:
        data += f"--- 今日世界大事 ---\n{world_news}\n\n"
    data += f"--- 基金涨跌(实际净值) ---\n{fund_summary}\n\n"
    data += f"--- AI/科技新闻 ---\n{news_summary}"
    prompt = BRIEFING_PROMPT.format(data=data)
    result = await llm_call(prompt)
    text = result if result else data
    # 检查跌幅并追加加仓提醒
    if fund_data_list:
        alert = check_fund_alert(fund_data_list, threshold=-1.5)
        if alert:
            text += "\n\n" + alert
    return text
