import asyncio
import json
import re
from typing import Optional

import aiohttp

from astrbot.api import logger

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://quote.eastmoney.com/",
}

OTC_FUND_API = "https://fundgz.1234567.com.cn/js/{}.js"
QUOTE_API = "http://push2.eastmoney.com/api/qt/stock/get"
FUND_SEARCH_API = (
    "https://fundsuggest.eastmoney.com/FundSearch/api/FundSearchAPI.ashx"
)
NAV_HISTORY_API = "https://api.fund.eastmoney.com/f10/lsjz"

_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=10)


def _is_otc(code: str) -> bool:
    if not code or len(code) != 6:
        return False
    return code.startswith(("0", "2"))


def _market(code: str) -> str:
    return "1" if code.startswith(("5", "6")) else "0"


class FundAPI:
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=_TIMEOUT,
                connector=aiohttp.TCPConnector(ssl=True, limit=5),
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _request(self, url: str, params: dict = None) -> Optional[dict]:
        session = await self._get_session()
        try:
            async with session.get(url, params=params, headers=HEADERS) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    return json.loads(text)
        except Exception as e:
            logger.debug(f"[FundAPI] 请求失败 {url}: {e}")
        return None

    async def get_realtime(self, fund_code: str) -> Optional[dict]:
        fund_code = str(fund_code).strip()
        if _is_otc(fund_code):
            real = await self._get_otc_real_nav(fund_code)
            if real:
                return real
            return await self._get_otc(fund_code)
        return await self._get_exchange(fund_code)

    async def _get_otc_real_nav(self, code: str) -> Optional[dict]:
        params = {
            "fundCode": code,
            "pageIndex": "1",
            "pageSize": "2",
        }
        try:
            session = await self._get_session()
            async with session.get(
                NAV_HISTORY_API, params=params,
                headers={**HEADERS, "Referer": "https://fund.eastmoney.com/"},
            ) as resp:
                if resp.status != 200:
                    return None
                data = json.loads(await resp.text())
            items = data.get("Data", {}).get("LSJZList", [])
            if not items:
                return None
            latest = items[0]

            def sf(v):
                try:
                    return float(v) if v else 0.0
                except (ValueError, TypeError):
                    return 0.0

            return {
                "code": code,
                "name": "",
                "latest_price": sf(latest.get("DWJZ")),
                "prev_close": sf(items[1].get("DWJZ")) if len(items) > 1 else 0.0,
                "change_rate": sf(latest.get("JZZZL")),
                "update_time": latest.get("FSRQ", ""),
                "is_otc": True,
                "is_actual": True,
            }
        except Exception as e:
            logger.debug(f"[FundAPI] 实际净值查询失败 {code}: {e}")
        return None

    async def _get_otc(self, code: str) -> Optional[dict]:
        url = OTC_FUND_API.format(code)
        session = await self._get_session()
        try:
            async with session.get(url, headers=HEADERS) as resp:
                if resp.status != 200:
                    return None
                text = await resp.text()
                match = re.search(r"jsonpgz\((.*)\)", text)
                if not match:
                    return None
                data = json.loads(match.group(1))

                def sf(v):
                    try:
                        return float(v) if v and v != "" else 0.0
                    except (ValueError, TypeError):
                        return 0.0

                return {
                    "code": data.get("fundcode", code),
                    "name": data.get("name", ""),
                    "latest_price": sf(data.get("gsz")),
                    "prev_close": sf(data.get("dwjz")),
                    "change_rate": sf(data.get("gszzl")),
                    "update_time": data.get("gztime", ""),
                    "is_otc": True,
                }
        except Exception as e:
            logger.debug(f"[FundAPI] 场外基金查询失败 {code}: {e}")
        return None

    async def _get_exchange(self, code: str) -> Optional[dict]:
        m = _market(code)
        params = {
            "secid": f"{m}.{code}",
            "fields": "f43,f44,f45,f46,f57,f58,f60,f152,f169,f170",
        }
        data = await self._request(QUOTE_API, params)
        if not data or data.get("rc") != 0:
            return await self._get_otc(code)
        result = data.get("data", {})
        if not result:
            return None

        dp = result.get("f152", 2)
        try:
            dp = int(dp)
        except (ValueError, TypeError):
            dp = 2
        divisor = 10 ** dp

        def sf(v, d=1):
            if v is None or v == "-":
                return 0.0
            try:
                return float(v) / d
            except (ValueError, TypeError):
                return 0.0

        return {
            "code": str(result.get("f57", code)),
            "name": str(result.get("f58", "")),
            "latest_price": sf(result.get("f43"), divisor),
            "prev_close": sf(result.get("f60"), divisor),
            "change_rate": sf(result.get("f170"), 100),
            "change_amount": sf(result.get("f169"), divisor),
        }

    async def search(self, keyword: str) -> list[dict]:
        if not keyword or not keyword.strip():
            return []
        params = {"m": "1", "key": keyword.strip()}
        data = await self._request(FUND_SEARCH_API, params)
        if not data or data.get("ErrCode") != 0:
            return []
        results = []
        for item in data.get("Datas", []):
            if item.get("CATEGORY") != 700:
                continue
            code = item.get("CODE", "")
            name = item.get("NAME", "")
            info = item.get("FundBaseInfo", {})
            results.append({
                "code": code,
                "name": name,
                "type": info.get("FTYPE", ""),
            })
            if len(results) >= 5:
                break
        return results

    async def get_batch(self, codes: list[str]) -> list[dict]:
        tasks = [self.get_realtime(c) for c in codes]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        output = []
        for code, result in zip(codes, results):
            if isinstance(result, BaseException) or result is None:
                output.append({"code": code, "error": True})
            else:
                result["error"] = False
                output.append(result)
        return output

    def format_summary(
        self, holdings: list[dict], data_list: list[dict],
    ) -> str:
        lines = []
        total_change = 0.0
        for holding, data in zip(holdings, data_list):
            if data.get("error"):
                lines.append(f"  {holding.get('code', '?')} - 查询失败")
                continue
            name = data.get("name", data.get("code", ""))
            rate = data.get("change_rate", 0.0)
            price = data.get("latest_price", 0.0)
            prev = data.get("prev_close", 0.0)
            shares = holding.get("shares", 0)
            emoji = "🔺" if rate > 0 else "🔻" if rate < 0 else "➖"
            change_amount = (price - prev) * shares if prev > 0 else 0
            total_change += change_amount
            line = f"  {emoji} {name}({data.get('code', '')})"
            line += f" 净值 {price:.4f}"
            tag = "实际" if data.get("is_actual") else "估值"
            line += f" {tag}涨跌 {rate:+.2f}%"
            if shares > 0:
                line += f" 持仓变动 ¥{change_amount:+.2f}"
            lines.append(line)
        if total_change != 0:
            lines.append(f"\n  合计持仓变动: ¥{total_change:+.2f}")
        return "\n".join(lines)
