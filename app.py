from __future__ import annotations

import json
import math
import os
import random
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from statistics import mean, pstdev


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"


PERIODS = {1, 5, 10, 30}
SOURCE_NAMES = {
    "eastmoney": "东方财富",
    "sina": "新浪财经",
    "demo": "演示行情",
}


class DataSourceError(RuntimeError):
    pass


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def http_get_text(url: str, timeout: float = 4.0) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 futures-intraday-tool/1.0",
            "Referer": "https://quote.10jqka.com.cn/",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("gbk", errors="ignore")


def normalize_symbol(raw: str) -> str:
    symbol = "".join(ch for ch in raw.strip() if ch.isalnum()).upper()
    return symbol or "M2609"


def continuous_symbol(symbol: str) -> str:
    letters = "".join(ch for ch in symbol if ch.isalpha())
    digits = "".join(ch for ch in symbol if ch.isdigit())
    if not letters:
        return symbol
    return f"{letters}主连" if digits else symbol


def try_tencent_quote(symbol: str) -> dict:
    candidates = [
        symbol.lower(),
        f"hf_{symbol.lower()}",
        f"nf_{symbol.lower()}",
        f"sf_{symbol.lower()}",
    ]
    errors: list[str] = []
    for code in candidates:
        url = f"https://qt.gtimg.cn/q={urllib.parse.quote(code)}"
        try:
            text = http_get_text(url)
        except Exception as exc:
            errors.append(f"{code}: {exc}")
            continue
        if "~" not in text or "none_match" in text:
            errors.append(f"{code}: 未匹配")
            continue
        fields = text.split('"')
        payload = fields[1] if len(fields) > 1 else text
        parts = payload.split("~")
        prices = [to_float(x) for x in parts if to_float(x) is not None]
        last = next((x for x in prices if x and x > 0), None)
        if not last:
            errors.append(f"{code}: 无价格")
            continue
        return {
            "last": last,
            "name": parts[1] if len(parts) > 1 and parts[1] else symbol,
            "rawCode": code,
            "raw": payload[:240],
        }
    raise DataSourceError("腾讯接口未返回可用期货报价：" + "；".join(errors[-3:]))


def try_ths_quote(symbol: str) -> dict:
    # 同花顺的正式 iFinD 行情接口需要终端/账号授权，免费网页端经常变更
    # 鉴权路径。保留按钮入口，但不再请求会 404 的旧 URL。
    raise DataSourceError("同花顺公开网页接口当前无法稳定直连国内期货分钟线")


def try_sina_bars(symbol: str, period: int) -> tuple[list[dict], dict]:
    var_name = f"_{symbol}_{period}"
    url = (
        "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/"
        f"var%20{urllib.parse.quote(var_name)}=/InnerFuturesNewService.getFewMinLine"
        f"?symbol={urllib.parse.quote(symbol)}&type={period}"
    )
    text = http_get_text(url, timeout=6)
    match = re.search(r"=\((\[.*\])\);?\s*$", text, re.S)
    if not match:
        raise DataSourceError("新浪财经分钟线接口无可解析数据")
    try:
        rows = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise DataSourceError(f"新浪财经分钟线解析失败：{exc}") from exc
    bars = []
    for row in rows[-240:]:
        try:
            dt = datetime.strptime(row["d"], "%Y-%m-%d %H:%M:%S")
            bars.append(
                {
                    "time": dt.strftime("%m-%d %H:%M"),
                    "open": round(float(row["o"]), 2),
                    "high": round(float(row["h"]), 2),
                    "low": round(float(row["l"]), 2),
                    "close": round(float(row["c"]), 2),
                    "volume": int(float(row.get("v") or 0)),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    if len(bars) < 30:
        raise DataSourceError("新浪财经分钟线数量不足")
    quote = {
        "last": bars[-1]["close"],
        "name": symbol,
        "rawCode": symbol,
        "raw": f"sina:{symbol}:{period}",
    }
    return bars, quote


def try_eastmoney_quote_id(symbol: str) -> str:
    url = f"https://searchapi.eastmoney.com/api/suggest/get?input={urllib.parse.quote(symbol)}&type=14"
    try:
        text = http_get_text(url, timeout=6)
    except Exception as exc:
        raise DataSourceError(f"东方财富搜索接口连接失败：{exc}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DataSourceError(f"东方财富搜索接口解析失败：{exc}") from exc
    rows = payload.get("QuotationCodeTable", {}).get("Data") or []
    normalized = symbol.lower()
    for row in rows:
        quote_id = row.get("QuoteID")
        code = str(row.get("Code") or row.get("UnifiedCode") or "").lower()
        classify = str(row.get("Classify") or "")
        if quote_id and code == normalized and classify.lower() == "futures":
            return quote_id
    for row in rows:
        quote_id = row.get("QuoteID")
        if quote_id and str(row.get("Classify") or "").lower() == "futures":
            return quote_id
    raise DataSourceError("东方财富未找到该期货合约代码")


def try_eastmoney_bars(symbol: str, period: int) -> tuple[list[dict], dict]:
    quote_id = try_eastmoney_quote_id(symbol)
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get?"
        f"secid={urllib.parse.quote(quote_id)}"
        "&fields1=f1,f2,f3,f4,f5,f6"
        "&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
        f"&klt={period}&fqt=0&end=20500101&lmt=240"
    )
    try:
        text = http_get_text(url, timeout=8)
    except Exception as exc:
        raise DataSourceError(f"东方财富K线接口连接失败：{exc}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DataSourceError(f"东方财富K线接口解析失败：{exc}") from exc
    klines = payload.get("data", {}).get("klines") or []
    bars = []
    for item in klines[-240:]:
        parts = item.split(",")
        if len(parts) < 6:
            continue
        try:
            dt = datetime.strptime(parts[0], "%Y-%m-%d %H:%M")
            bars.append(
                {
                    "time": dt.strftime("%m-%d %H:%M"),
                    "open": round(float(parts[1]), 2),
                    "close": round(float(parts[2]), 2),
                    "high": round(float(parts[3]), 2),
                    "low": round(float(parts[4]), 2),
                    "volume": int(float(parts[5])),
                }
            )
        except ValueError:
            continue
    if len(bars) < 30:
        raise DataSourceError("东方财富K线数量不足")
    quote = {"last": bars[-1]["close"], "name": symbol, "rawCode": quote_id, "raw": f"eastmoney:{quote_id}:{period}"}
    return bars, quote


def to_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def extract_numbers(text: str) -> list[float]:
    buff = []
    current = []
    for ch in text:
        if ch.isdigit() or ch in ".-":
            current.append(ch)
        elif current:
            value = to_float("".join(current))
            if value is not None and math.isfinite(value):
                buff.append(value)
            current = []
    if current:
        value = to_float("".join(current))
        if value is not None and math.isfinite(value):
            buff.append(value)
    return buff


def quote_from_source(symbol: str, source: str) -> tuple[dict, str | None]:
    if source == "eastmoney":
        bars, quote = try_eastmoney_bars(symbol, 5)
        return quote, None
    if source == "sina":
        bars, quote = try_sina_bars(symbol, 5)
        return quote, None
    if source == "demo":
        base = 1000 + (sum(ord(ch) for ch in symbol) % 5000)
        return {"last": float(base), "name": symbol, "rawCode": "demo", "raw": ""}, None
    raise DataSourceError("未知接口源")


def synthetic_bars(symbol: str, period: int, source: str, last: float) -> list[dict]:
    seed = f"{symbol}-{period}-{source}-{datetime.now().strftime('%Y%m%d%H')}"
    rng = random.Random(seed)
    count = 160
    step = timedelta(minutes=period)
    now = datetime.now().replace(second=0, microsecond=0)
    start = now - step * (count - 1)
    volatility = max(last * (0.0007 + period * 0.00008), 0.8)
    drift = rng.uniform(-0.08, 0.08) * volatility
    price = last - drift * count * 0.25
    bars = []
    for idx in range(count):
        shock = rng.gauss(drift, volatility)
        open_price = price
        close = max(0.01, open_price + shock)
        high = max(open_price, close) + abs(rng.gauss(0, volatility * 0.55))
        low = min(open_price, close) - abs(rng.gauss(0, volatility * 0.55))
        volume = int(800 + abs(rng.gauss(0, 520)) + idx * 2)
        bars.append(
            {
                "time": (start + step * idx).strftime("%m-%d %H:%M"),
                "open": round(open_price, 2),
                "high": round(high, 2),
                "low": round(max(0.01, low), 2),
                "close": round(close, 2),
                "volume": volume,
            }
        )
        price = close

    anchor = bars[-1]["close"]
    if anchor:
        ratio = last / anchor
        for bar in bars:
            for key in ("open", "high", "low", "close"):
                bar[key] = round(bar[key] * ratio, 2)
    return bars


def build_bars(symbol: str, period: int, source: str, continuous: bool) -> tuple[list[dict], dict, str | None]:
    request_symbol = continuous_symbol(symbol) if continuous else symbol
    warning = None
    provider = source
    is_synthetic = False
    try:
        if source == "demo":
            raise DataSourceError("使用演示行情")
        if source == "eastmoney":
            try:
                bars, quote = try_eastmoney_bars(symbol, period)
            except DataSourceError as exc:
                bars, quote = try_sina_bars(symbol, period)
                provider = "sina"
                warning = f"{exc}；已自动改用新浪财经真实分钟线"
        elif source == "sina":
            bars, quote = try_sina_bars(symbol, period)
            provider = "sina"
        else:
            raise DataSourceError("未知或不可用行情源")
    except DataSourceError as exc:
        quote, _ = quote_from_source(symbol, "demo")
        last = float(quote["last"])
        bars = synthetic_bars(symbol, period, "demo", last)
        provider = "demo"
        is_synthetic = True
        warning = f"{exc}；全部联网行情不可用，当前使用本地演示K线"

    meta = {
        "symbol": symbol,
        "requestSymbol": request_symbol,
        "source": source,
        "sourceName": SOURCE_NAMES.get(source, source),
        "provider": provider,
        "providerName": SOURCE_NAMES.get(provider, provider),
        "period": period,
        "continuous": continuous,
        "quoteName": quote.get("name", symbol),
        "quoteCode": quote.get("rawCode", symbol),
        "updatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "isSyntheticBars": is_synthetic,
    }
    return bars, meta, warning


def sma(values: list[float], window: int) -> float:
    if len(values) < window:
        return mean(values)
    return mean(values[-window:])


def ema_series(values: list[float], span: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (span + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(value * alpha + result[-1] * (1 - alpha))
    return result


def calc_rsi(values: list[float], window: int = 14) -> float:
    if len(values) <= window:
        return 50.0
    gains = []
    losses = []
    for left, right in zip(values[-window - 1 : -1], values[-window:]):
        delta = right - left
        gains.append(max(delta, 0))
        losses.append(abs(min(delta, 0)))
    avg_gain = mean(gains) or 0.0001
    avg_loss = mean(losses) or 0.0001
    return 100 - 100 / (1 + avg_gain / avg_loss)


def analyze_bars(bars: list[dict]) -> dict:
    closes = [bar["close"] for bar in bars]
    highs = [bar["high"] for bar in bars]
    lows = [bar["low"] for bar in bars]
    last = closes[-1]
    ma5 = sma(closes, 5)
    ma10 = sma(closes, 10)
    ma20 = sma(closes, 20)
    ma60 = sma(closes, 60)
    ema12 = ema_series(closes, 12)
    ema26 = ema_series(closes, 26)
    dif = ema12[-1] - ema26[-1]
    dea = ema_series([a - b for a, b in zip(ema12[-len(ema26) :], ema26)], 9)[-1]
    macd = (dif - dea) * 2
    rsi = calc_rsi(closes)
    changes = [b - a for a, b in zip(closes[-31:-1], closes[-30:])]
    atr = mean([h - l for h, l in zip(highs[-14:], lows[-14:])])
    sigma = pstdev(changes) if len(changes) > 1 else atr
    trend_score = 0
    trend_score += 1 if last > ma20 else -1
    trend_score += 1 if ma5 > ma10 > ma20 else -1 if ma5 < ma10 < ma20 else 0
    trend_score += 1 if macd > 0 else -1
    trend_score += 1 if closes[-1] > closes[-6] else -1
    trend_score += 1 if last > ma60 else -1
    heat = clamp((rsi - 50) / 25, -1, 1)
    score = clamp((trend_score / 5) * 70 + heat * 30, -100, 100)

    if score >= 45:
        action = "偏多试多"
        bias = "多头"
        entry = max(ma5, last - atr * 0.35)
        stop = entry - max(atr * 0.9, sigma * 1.2)
        target = entry + max(atr * 1.5, sigma * 1.8)
        plan = "回踩短均线不破可轻仓试多，冲高放量后分批止盈。"
    elif score <= -45:
        action = "偏空试空"
        bias = "空头"
        entry = min(ma5, last + atr * 0.35)
        stop = entry + max(atr * 0.9, sigma * 1.2)
        target = entry - max(atr * 1.5, sigma * 1.8)
        plan = "反抽短均线不过可轻仓试空，急跌后避免追空。"
    else:
        action = "震荡观望"
        bias = "震荡"
        entry = last
        stop = last - atr
        target = last + atr
        plan = "趋势强度不足，等待突破区间或量能确认后再介入。"

    risk = "高" if atr / max(last, 1) > 0.012 else "中" if atr / max(last, 1) > 0.006 else "低"
    return {
        "last": round(last, 2),
        "action": action,
        "bias": bias,
        "score": round(score, 1),
        "plan": plan,
        "risk": risk,
        "entry": round(entry, 2),
        "stop": round(stop, 2),
        "target": round(target, 2),
        "ma5": round(ma5, 2),
        "ma10": round(ma10, 2),
        "ma20": round(ma20, 2),
        "ma60": round(ma60, 2),
        "rsi": round(rsi, 1),
        "macd": round(macd, 3),
        "atr": round(atr, 2),
    }


class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path: str) -> str:
        parsed = urllib.parse.urlparse(path)
        clean = parsed.path.lstrip("/")
        if not clean:
            clean = "index.html"
        return str(STATIC / clean)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/analyze":
            self.handle_analyze(parsed)
            return
        if parsed.path == "/api/health":
            self.send_json({"ok": True, "time": time.time()})
            return
        super().do_GET()

    def handle_analyze(self, parsed: urllib.parse.ParseResult) -> None:
        params = urllib.parse.parse_qs(parsed.query)
        symbol = normalize_symbol(params.get("symbol", ["M2609"])[0])
        source = params.get("source", ["ths"])[0]
        continuous = params.get("continuous", ["1"])[0] in {"1", "true", "yes"}
        try:
            period = int(params.get("period", ["5"])[0])
        except ValueError:
            period = 5
        if period not in PERIODS:
            period = 5
        try:
            bars, meta, warning = build_bars(symbol, period, source, continuous)
            analysis = analyze_bars(bars)
            self.send_json({"ok": True, "bars": bars, "meta": meta, "analysis": analysis, "warning": warning})
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=500)

    def send_json(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    STATIC.mkdir(exist_ok=True)
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8765"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"期货日内分析工具已启动: http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
