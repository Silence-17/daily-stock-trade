# -*- coding: utf-8 -*-
"""
===================================
YfinanceFetcher - 兜底数据源 (Priority 4)
===================================

数据来源：Yahoo Finance（通过 yfinance 库）
特点：国际数据源、可能有延迟或缺失
定位：当所有国内数据源都失败时的最后保障

关键策略：
1. 自动将 A 股代码转换为 yfinance 格式（.SS / .SZ）
2. 处理 Yahoo Finance 的数据格式差异
3. 失败后指数退避重试
"""

import csv
import json
import logging
import math
import threading
from datetime import datetime, timezone
from io import StringIO
from typing import Optional, List, Dict, Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import quote as url_quote
from zoneinfo import ZoneInfo

import pandas as pd
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

from .base import BaseFetcher, DataFetchError, STANDARD_COLUMNS, is_bse_code
from .realtime_types import UnifiedRealtimeQuote, RealtimeSource
from .us_index_mapping import (
    get_us_index_yf_symbol,
    get_us_yfinance_alias,
    is_us_stock_code,
)
from src.services.market_symbol_utils import get_suffix_market, is_suffix_market_symbol

# 可选导入本地股票映射补丁，若缺失则使用空字典兜底
try:
    from src.data.stock_mapping import STOCK_NAME_MAP, is_meaningful_stock_name
except (ImportError, ModuleNotFoundError):
    STOCK_NAME_MAP = {}

    def is_meaningful_stock_name(name: str | None, stock_code: str) -> bool:
        """简单的名称有效性校验兜底"""
        if not name:
            return False
        n = str(name).strip()
        return bool(n and n.upper() != str(stock_code).strip().upper())

import os

logger = logging.getLogger(__name__)
CROSS_MARKET_US_HTTP_TIMEOUT_SECONDS = 5.0
CROSS_MARKET_US_STREAM_IDLE_SECONDS = 90.0


def _safe_number(value: object) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None

GLOBAL_INDEX_REALTIME_MAPPING = {
    "N225": ("^N225", "Nikkei 225", "jp", "JPY"),
    "TOPX": ("^TOPX", "TOPIX", "jp", "JPY"),
    "KS11": ("^KS11", "KOSPI", "kr", "KRW"),
    "KQ11": ("^KQ11", "KOSDAQ", "kr", "KRW"),
}


class YfinanceFetcher(BaseFetcher):
    """
    Yahoo Finance 数据源实现

    优先级：4（最低，作为兜底）
    数据来源：Yahoo Finance

    关键策略：
    - 自动转换股票代码格式
    - 处理时区和数据格式差异
    - 失败后指数退避重试

    注意事项：
    - A 股数据可能有延迟
    - 某些股票可能无数据
    - 数据精度可能与国内源略有差异
    """

    name = "YfinanceFetcher"
    priority = int(os.getenv("YFINANCE_PRIORITY", "4"))
    concurrent_safe_methods = frozenset({
        "get_cross_market_us_premarket_quote",
        "get_cross_market_us_premarket_quotes",
        "get_cross_market_us_realtime_quote",
    })

    def __init__(self):
        """初始化 YfinanceFetcher"""
        self._premarket_stream_lock = threading.RLock()
        self._premarket_stream_start_lock = threading.Lock()
        self._premarket_stream_websocket = None
        self._premarket_stream_thread: Optional[threading.Thread] = None
        self._premarket_stream_quotes: Dict[str, UnifiedRealtimeQuote] = {}
        self._premarket_stream_code_by_symbol: Dict[str, str] = {}
        self._premarket_stream_completed = threading.Event()
        self._premarket_stream_idle_timer: Optional[threading.Timer] = None
        self._premarket_stream_generation = 0
        self._premarket_stream_idle_generation = 0

    def close(self) -> None:
        """Release the persistent cross-market premarket stream, if any."""

        self._stop_cross_market_us_premarket_stream()

    @staticmethod
    def _is_jp_kr_suffix_stock(stock_code: str) -> bool:
        """Return True for supported JP/KR suffix-only Yahoo symbols."""
        return is_suffix_market_symbol(stock_code, "jp") or is_suffix_market_symbol(stock_code, "kr")

    @staticmethod
    def _is_tw_suffix_stock(stock_code: str) -> bool:
        """Return True for supported Taiwan suffix-only Yahoo symbols (TWSE `.TW` / TPEx `.TWO`).

        Taiwan base codes are 4-6 digits (common stocks 4, ETFs/others up to 6,
        e.g. 00878 / 006208), wider than the JP `.T` range.
        """
        return is_suffix_market_symbol(stock_code, "tw")

    @staticmethod
    def _latest_minute_provider_timestamp(ticker) -> Optional[str]:
        """Return Yahoo's latest minute timestamp without inventing freshness."""
        try:
            minute_history = ticker.history(period="1d", interval="1m")
            if minute_history is None or minute_history.empty:
                return None
            latest = minute_history.index[-1]
            if hasattr(latest, "to_pydatetime"):
                latest = latest.to_pydatetime()
            if not isinstance(latest, datetime) or latest.tzinfo is None:
                return None
            return latest.astimezone(timezone.utc).isoformat()
        except Exception as exc:
            logger.debug("[Yfinance] latest minute provider timestamp unavailable: %s", exc)
            return None

    def _convert_stock_code(self, stock_code: str) -> str:
        """
        转换股票代码为 Yahoo Finance 格式

        Yahoo Finance 代码格式：
        - A股沪市：600519.SS (Shanghai Stock Exchange)
        - A股深市：000001.SZ (Shenzhen Stock Exchange)
        - 港股：0700.HK (Hong Kong Stock Exchange)
        - 美股：AAPL, TSLA, GOOGL (无需后缀)

        Args:
            stock_code: 原始代码，如 '600519', 'hk00700', 'AAPL'

        Returns:
            Yahoo Finance 格式代码

        Examples:
            >>> fetcher._convert_stock_code('600519')
            '600519.SS'
            >>> fetcher._convert_stock_code('hk00700')
            '0700.HK'
            >>> fetcher._convert_stock_code('AAPL')
            'AAPL'
        """
        code = stock_code.strip().upper()

        alias_symbol, _ = get_us_yfinance_alias(code)
        if alias_symbol:
            return alias_symbol

        # 美股指数：映射到 Yahoo Finance 符号（如 SPX -> ^GSPC）
        yf_symbol, _ = get_us_index_yf_symbol(code)
        if yf_symbol:
            logger.debug(f"识别为美股指数: {code} -> {yf_symbol}")
            return yf_symbol

        # 美股：1-5 个大写字母（可选 .X 后缀），原样返回
        if is_us_stock_code(code):
            logger.debug(f"识别为美股代码: {code}")
            return code

        # 日股/韩股/台股 MVP：显式 Yahoo Finance suffix-only 代码，原样传给 Yahoo。
        if self._is_jp_kr_suffix_stock(code) or self._is_tw_suffix_stock(code):
            logger.debug(f"识别为日韩台 Yahoo suffix 代码: {code}")
            return code

        # 港股：hk前缀 -> .HK后缀
        if code.startswith('HK'):
            hk_code = code[2:].lstrip('0') or '0'  # 去除前导0，但保留至少一个0
            hk_code = hk_code.zfill(4)  # 补齐到4位
            logger.debug(f"转换港股代码: {stock_code} -> {hk_code}.HK")
            return f"{hk_code}.HK"

        # 已经包含后缀的情况
        if '.SS' in code or '.SZ' in code or '.HK' in code or '.BJ' in code:
            return code

        # 去除可能的 .SH 后缀
        code = code.replace('.SH', '')

        # ETF: Shanghai ETF (51xx, 52xx, 56xx, 58xx) -> .SS; Shenzhen ETF (15xx, 16xx, 18xx) -> .SZ
        if len(code) == 6:
            if code.startswith(('51', '52', '56', '58')):
                return f"{code}.SS"
            if code.startswith(('15', '16', '18')):
                return f"{code}.SZ"

        # BSE (Beijing Stock Exchange): 8xxxxx, 4xxxxx, 920xxx
        if is_bse_code(code):
            base = code.split('.')[0] if '.' in code else code
            return f"{base}.BJ"

        # A股：根据代码前缀判断市场
        if code.startswith(('600', '601', '603', '688')):
            return f"{code}.SS"
        elif code.startswith(('000', '002', '300')):
            return f"{code}.SZ"
        else:
            logger.warning(f"无法确定股票 {code} 的市场，默认使用深市")
            return f"{code}.SZ"

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type((ConnectionError, TimeoutError)),
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )
    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """
        从 Yahoo Finance 获取原始数据

        使用 yfinance.download() 获取历史数据

        流程：
        1. 转换股票代码格式
        2. 调用 yfinance API
        3. 处理返回数据
        """
        import yfinance as yf

        # 转换代码格式
        yf_code = self._convert_stock_code(stock_code)

        logger.debug(f"调用 yfinance.download({yf_code}, {start_date}, {end_date})")

        try:
            # 使用 yfinance 下载数据
            df = yf.download(
                tickers=yf_code,
                start=start_date,
                end=end_date,
                progress=False,  # 禁止进度条
                auto_adjust=True,  # 自动调整价格（复权）
                multi_level_index=True
            )

            # 筛选出 yf_code 的列, 避免多只股票数据混淆
            if isinstance(df.columns, pd.MultiIndex) and len(df.columns) > 1:
                ticker_level = df.columns.get_level_values(1)
                mask = ticker_level == yf_code
                if mask.any():
                    df = df.loc[:, mask].copy()

            if df.empty:
                raise DataFetchError(f"Yahoo Finance 未查询到 {stock_code} 的数据")

            return df

        except Exception as e:
            if isinstance(e, DataFetchError):
                raise
            raise DataFetchError(f"Yahoo Finance 获取数据失败: {e}") from e

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        """
        标准化 Yahoo Finance 数据

        yfinance 返回的列名：
        Open, High, Low, Close, Volume（索引是日期）

        注意：新版 yfinance 返回 MultiIndex 列名，如 ('Close', 'AMD')
        需要先扁平化列名再进行处理

        需要映射到标准列名：
        date, open, high, low, close, volume, amount, pct_chg
        """
        df = df.copy()

        # 处理 MultiIndex 列名（新版 yfinance 返回格式）
        # 例如: ('Close', 'AMD') -> 'Close'
        if isinstance(df.columns, pd.MultiIndex):
            logger.debug("检测到 MultiIndex 列名，进行扁平化处理")
            # 取第一级列名（Price level: Close, High, Low, etc.）
            df.columns = df.columns.get_level_values(0)

        # 重置索引，将日期从索引变为列
        df = df.reset_index()

        # 列名映射（yfinance 使用首字母大写）
        column_mapping = {
            'Date': 'date',
            'Datetime': 'date',
            'datetime': 'date',
            'Open': 'open',
            'High': 'high',
            'Low': 'low',
            'Close': 'close',
            'Volume': 'volume',
        }

        df = df.rename(columns=column_mapping)
        if 'date' not in df.columns:
            index_col = df.columns[0] if len(df.columns) else None
            if index_col is not None:
                candidate = df[index_col]
                if pd.api.types.is_datetime64_any_dtype(candidate):
                    df = df.rename(columns={index_col: 'date'})
                elif not pd.api.types.is_numeric_dtype(candidate):
                    parsed_dates = pd.to_datetime(candidate, errors='coerce')
                    if parsed_dates.notna().any():
                        df = df.rename(columns={index_col: 'date'})
                        df['date'] = parsed_dates

        # 计算涨跌幅（因为 yfinance 不直接提供）
        if 'close' in df.columns:
            df['pct_chg'] = df['close'].pct_change() * 100
            df['pct_chg'] = df['pct_chg'].fillna(0).round(2)

        # 计算成交额（yfinance 不提供，使用估算值）
        # 成交额 ≈ 成交量 * 平均价格
        if 'volume' in df.columns and 'close' in df.columns:
            df['amount'] = df['volume'] * df['close']
        else:
            df['amount'] = 0

        # 添加股票代码列
        df['code'] = stock_code

        # 只保留需要的列
        keep_cols = ['code'] + STANDARD_COLUMNS
        existing_cols = [col for col in keep_cols if col in df.columns]
        df = df[existing_cols]

        return df

    def _fetch_yf_ticker_data(self, yf, yf_code: str, name: str, return_code: str) -> Optional[Dict[str, Any]]:
        """
        通过 yfinance 拉取单个指数/股票的行情数据。

        Args:
            yf: yfinance 模块引用
            yf_code: yfinance 使用的代码（如 '000001.SS'、'^GSPC'）
            name: 指数显示名称
            return_code: 写入结果 dict 的 code 字段（如 'sh000001'、'SPX'）

        Returns:
            行情字典，失败时返回 None
        """
        ticker = yf.Ticker(yf_code)
        # 取近两日数据以计算涨跌幅
        hist = ticker.history(period='2d')
        if hist.empty:
            return None
        today_row = hist.iloc[-1]
        latest_index = hist.index[-1]
        data_date = (
            latest_index.date().isoformat()
            if hasattr(latest_index, 'date')
            else None
        )
        prev_row = hist.iloc[-2] if len(hist) > 1 else today_row
        price = float(today_row['Close'])
        prev_close = float(prev_row['Close'])
        change = price - prev_close
        change_pct = (change / prev_close) * 100 if prev_close else 0
        high = float(today_row['High'])
        low = float(today_row['Low'])
        # 振幅 = (最高 - 最低) / 昨收 * 100
        amplitude = ((high - low) / prev_close * 100) if prev_close else 0
        return {
            'code': return_code,
            'name': name,
            'current': price,
            'change': change,
            'change_pct': change_pct,
            'open': float(today_row['Open']),
            'high': high,
            'low': low,
            'prev_close': prev_close,
            'volume': float(today_row['Volume']),
            'amount': 0.0,  # Yahoo Finance 不提供准确成交额
            'amplitude': amplitude,
            'data_date': data_date,
            'data_granularity': 'session_bar',
        }

    def get_main_indices(self, region: str = "cn") -> Optional[List[Dict[str, Any]]]:
        """
        获取主要指数行情 (Yahoo Finance)，支持 A 股、美股、港股、日股、韩股与台股。
        region=us 时委托给 _get_us_main_indices。
        region=hk 时委托给 _get_hk_main_indices。
        region=jp/kr/tw 时分别委托给对应市场指数方法。
        """
        import yfinance as yf

        if region == "us":
            return self._get_us_main_indices(yf)
        if region == "hk":
            return self._get_hk_main_indices(yf)
        if region == "jp":
            return self._get_jp_main_indices(yf)
        if region == "kr":
            return self._get_kr_main_indices(yf)
        if region == "tw":
            return self._get_tw_main_indices(yf)

        # A 股指数：akshare 代码 -> (yfinance 代码, 显示名称)
        yf_mapping = {
            'sh000001': ('000001.SS', '上证指数'),
            'sz399001': ('399001.SZ', '深证成指'),
            'sz399006': ('399006.SZ', '创业板指'),
            'sh000688': ('000688.SS', '科创50'),
            'sh000016': ('000016.SS', '上证50'),
            'sh000300': ('000300.SS', '沪深300'),
        }

        results = []
        try:
            for ak_code, (yf_code, name) in yf_mapping.items():
                try:
                    item = self._fetch_yf_ticker_data(yf, yf_code, name, ak_code)
                    if item:
                        results.append(item)
                        logger.debug(f"[Yfinance] 获取指数 {name} 成功")
                except Exception as e:
                    logger.warning(f"[Yfinance] 获取指数 {name} 失败: {e}")

            if results:
                logger.info(f"[Yfinance] 成功获取 {len(results)} 个 A 股指数行情")
                return results

        except Exception as e:
            logger.error(f"[Yfinance] 获取 A 股指数行情失败: {e}")

        return None

    def _get_us_main_indices(self, yf) -> Optional[List[Dict[str, Any]]]:
        """获取美股主要指数行情（SPX、IXIC、DJI、VIX），复用 _fetch_yf_ticker_data"""
        # 大盘复盘所需核心美股指数
        us_indices = ['SPX', 'IXIC', 'DJI', 'VIX']
        results = []
        try:
            for code in us_indices:
                yf_symbol, name = get_us_index_yf_symbol(code)
                if not yf_symbol:
                    continue
                try:
                    item = self._fetch_yf_ticker_data(yf, yf_symbol, name, code)
                    if item:
                        results.append(item)
                        logger.debug(f"[Yfinance] 获取美股指数 {name} 成功")
                except Exception as e:
                    logger.warning(f"[Yfinance] 获取美股指数 {name} 失败: {e}")

            if results:
                logger.info(f"[Yfinance] 成功获取 {len(results)} 个美股指数行情")
                return results

        except Exception as e:
            logger.error(f"[Yfinance] 获取美股指数行情失败: {e}")

        return None

    def _get_hk_main_indices(self, yf) -> Optional[List[Dict[str, Any]]]:
        """获取港股主要指数行情（HSI、HSTECH、HSCEI），复用 _fetch_yf_ticker_data"""
        # Yahoo Finance 港股指数符号映射：
        # - HSI -> ^HSI
        # - HSTECH -> HSTECH.HK（不是 ^HSTECH）
        # - HSCEI -> ^HSCE（不是 ^HSCEI）
        # 该映射由离线单测 tests/test_yfinance_hk_indices.py 固化，避免在线依赖导致非确定性失败。
        hk_indices = {
            'HSI': ('^HSI', '恒生指数'),
            'HSTECH': ('HSTECH.HK', '恒生科技指数'),
            'HSCEI': ('^HSCE', '国企指数'),
        }
        results = []
        try:
            for code, (yf_symbol, name) in hk_indices.items():
                try:
                    item = self._fetch_yf_ticker_data(yf, yf_symbol, name, code)
                    if item:
                        results.append(item)
                        logger.debug(f"[Yfinance] 获取港股指数 {name} 成功")
                except Exception as e:
                    logger.warning(f"[Yfinance] 获取港股指数 {name} 失败: {e}")

            if results:
                logger.info(f"[Yfinance] 成功获取 {len(results)} 个港股指数行情")
                return results

        except Exception as e:
            logger.error(f"[Yfinance] 获取港股指数行情失败: {e}")

        return None

    def _get_jp_main_indices(self, yf) -> Optional[List[Dict[str, Any]]]:
        """获取日本主要指数行情（日经225、TOPIX），复用 _fetch_yf_ticker_data。"""
        jp_indices = {
            'N225': ('^N225', '日经225'),
            'TOPX': ('^TOPX', '东证指数'),
        }
        results = []
        try:
            for code, (yf_symbol, name) in jp_indices.items():
                try:
                    item = self._fetch_yf_ticker_data(yf, yf_symbol, name, code)
                    if item:
                        results.append(item)
                        logger.debug(f"[Yfinance] 获取日本指数 {name} 成功")
                except Exception as e:
                    logger.warning(f"[Yfinance] 获取日本指数 {name} 失败: {e}")
            if results:
                logger.info(f"[Yfinance] 成功获取 {len(results)} 个日本指数行情")
                return results
        except Exception as e:
            logger.error(f"[Yfinance] 获取日本指数行情失败: {e}")
        return None

    def _get_kr_main_indices(self, yf) -> Optional[List[Dict[str, Any]]]:
        """获取韩国主要指数行情（KOSPI、KOSDAQ），复用 _fetch_yf_ticker_data。"""
        kr_indices = {
            'KS11': ('^KS11', 'KOSPI'),
            'KQ11': ('^KQ11', 'KOSDAQ'),
        }
        results = []
        try:
            for code, (yf_symbol, name) in kr_indices.items():
                try:
                    item = self._fetch_yf_ticker_data(yf, yf_symbol, name, code)
                    if item:
                        results.append(item)
                        logger.debug(f"[Yfinance] 获取韩国指数 {name} 成功")
                except Exception as e:
                    logger.warning(f"[Yfinance] 获取韩国指数 {name} 失败: {e}")
            if results:
                logger.info(f"[Yfinance] 成功获取 {len(results)} 个韩国指数行情")
                return results
        except Exception as e:
            logger.error(f"[Yfinance] 获取韩国指数行情失败: {e}")
        return None

    def _get_tw_main_indices(self, yf) -> Optional[List[Dict[str, Any]]]:
        """获取台湾主要指数行情（加权指数 ^TWII、柜买指数 ^TWOII），复用 _fetch_yf_ticker_data。"""
        tw_indices = {
            'TWII': ('^TWII', '台湾加权指数'),
            'TWOII': ('^TWOII', '台湾柜买指数'),
        }
        results = []
        try:
            for code, (yf_symbol, name) in tw_indices.items():
                try:
                    item = self._fetch_yf_ticker_data(yf, yf_symbol, name, code)
                    if item:
                        results.append(item)
                        logger.debug(f"[Yfinance] 获取台湾指数 {name} 成功")
                except Exception as e:
                    logger.warning(f"[Yfinance] 获取台湾指数 {name} 失败: {e}")
            if results:
                logger.info(f"[Yfinance] 成功获取 {len(results)} 个台湾指数行情")
                return results
        except Exception as e:
            logger.error(f"[Yfinance] 获取台湾指数行情失败: {e}")
        return None

    def _is_us_stock(self, stock_code: str) -> bool:
        """
        判断代码是否为美股股票（排除美股指数）。

        委托给 us_index_mapping 模块的 is_us_stock_code()。
        """
        return is_us_stock_code(stock_code)

    def _get_us_stock_quote_from_stooq(self, stock_code: str) -> Optional[UnifiedRealtimeQuote]:
        """
        使用 Stooq 为美股实时行情提供免密钥兜底。

        Stooq 提供的是最新交易日行情，精度不如分时实时接口，但在 Yahoo / yfinance
        被限流时，至少能为 Web UI 提供可用价格；若可获取到昨收价，则同时提供涨跌幅等衍生指标。
        """
        symbol = stock_code.strip().upper()
        stooq_symbol = f"{symbol.lower()}.us"
        url = f"https://stooq.com/q/l/?s={stooq_symbol}"
        request = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; DSA/1.0; +https://github.com/ZhuLinsen/daily_stock_analysis)",
                "Accept": "text/plain,text/csv,*/*",
            },
        )

        try:
            with urlopen(request, timeout=15) as response:
                payload = response.read().decode("utf-8", "ignore").strip()
        except (HTTPError, URLError, TimeoutError) as exc:
            logger.warning(f"[Stooq] 获取美股 {symbol} 实时行情失败: {exc}")
            return None

        if not payload or payload.upper().startswith("NO DATA"):
            logger.warning(f"[Stooq] 无法获取 {symbol} 的行情数据")
            return None

        def _fetch_prev_close() -> Optional[float]:
            history_url = f"https://stooq.com/q/d/l/?s={stooq_symbol}&i=d"
            history_request = Request(
                history_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; DSA/1.0; +https://github.com/ZhuLinsen/daily_stock_analysis)",
                    "Accept": "text/plain,text/csv,*/*",
                },
            )
            try:
                with urlopen(history_request, timeout=15) as response:
                    history_payload = response.read().decode("utf-8", "ignore").strip()
            except (HTTPError, URLError, TimeoutError) as exc:
                logger.debug(f"[Stooq] 获取美股 {symbol} 日线历史失败: {exc}")
                return None

            if not history_payload or history_payload.upper().startswith("NO DATA"):
                return None

            try:
                reader = csv.reader(StringIO(history_payload))
                header = next(reader, None)
                if not header:
                    return None

                header_tokens = [cell.strip().lower() for cell in header]
                has_header = "close" in header_tokens and "date" in header_tokens
                if not has_header:
                    return None

                date_index = header_tokens.index("date")
                close_index = header_tokens.index("close")

                daily_rows: list[tuple[datetime, float]] = []
                for row in reader:
                    if not row:
                        continue
                    date_text = row[date_index].strip() if len(row) > date_index else ""
                    close_text = row[close_index].strip() if len(row) > close_index else ""
                    if not date_text or not close_text:
                        continue
                    try:
                        dt = datetime.strptime(date_text, "%Y-%m-%d")
                        close_val = float(close_text)
                    except Exception:
                        continue
                    daily_rows.append((dt, close_val))

                if len(daily_rows) < 2:
                    return None

                daily_rows.sort(key=lambda item: item[0])
                return daily_rows[-2][1]
            except Exception:
                return None

        try:
            reader = csv.reader(StringIO(payload))
            first_row = next(reader, None)
            if first_row is None:
                raise ValueError(f"unexpected Stooq payload: {payload}")

            normalized_first_row = [cell.strip() for cell in first_row]
            header_tokens = {cell.lower() for cell in normalized_first_row if cell}
            has_header = 'open' in header_tokens and 'close' in header_tokens
            row = next(reader, None) if has_header else first_row
            if row is None:
                raise ValueError(f"unexpected Stooq payload: {payload}")

            normalized_row = [cell.strip() for cell in row]
            while normalized_row and normalized_row[-1] == '':
                normalized_row.pop()

            if len(normalized_row) >= 8:
                open_index, high_index, low_index, price_index, volume_index = 3, 4, 5, 6, 7
            elif len(normalized_row) >= 7:
                open_index, high_index, low_index, price_index, volume_index = 2, 3, 4, 5, 6
            else:
                raise ValueError(f"unexpected Stooq payload: {payload}")

            open_price = float(normalized_row[open_index])
            high = float(normalized_row[high_index])
            low = float(normalized_row[low_index])
            price = float(normalized_row[price_index])
            volume = int(float(normalized_row[volume_index]))

            prev_close = _fetch_prev_close()
            change_amount = None
            change_pct = None
            amplitude = None
            if prev_close is not None and prev_close > 0:
                change_amount = price - prev_close
                change_pct = (change_amount / prev_close) * 100
                amplitude = ((high - low) / prev_close) * 100

            quote = UnifiedRealtimeQuote(
                code=symbol,
                name=STOCK_NAME_MAP.get(symbol, ''),
                source=RealtimeSource.STOOQ,
                price=price,
                change_pct=round(change_pct, 2) if change_pct is not None else None,
                change_amount=round(change_amount, 4) if change_amount is not None else None,
                volume=volume,
                amount=None,
                volume_ratio=None,
                turnover_rate=None,
                amplitude=round(amplitude, 2) if amplitude is not None else None,
                open_price=open_price,
                high=high,
                low=low,
                pre_close=prev_close,
                pe_ratio=None,
                pb_ratio=None,
                total_mv=None,
                circ_mv=None,
            )
            logger.info(f"[Stooq] 获取美股 {symbol} 兜底行情成功: 价格={price}")
            return quote
        except Exception as exc:
            logger.warning(f"[Stooq] 解析美股 {symbol} 行情失败: {exc}")
            return None

    def _get_us_stock_quote_from_tencent(
        self,
        stock_code: str,
        *,
        timeout_seconds: float = 15.0,
    ) -> Optional[UnifiedRealtimeQuote]:
        """Fetch one US quote from Tencent before falling back to delayed Stooq data."""

        symbol = stock_code.strip().upper()
        request = Request(
            f"https://qt.gtimg.cn/q=us{symbol}",
            headers={
                "Referer": "https://finance.qq.com",
                "User-Agent": "Mozilla/5.0 (compatible; DSA/1.0; +https://github.com/ZhuLinsen/daily_stock_analysis)",
                "Accept": "text/plain,*/*",
            },
        )
        try:
            with urlopen(request, timeout=max(1.0, float(timeout_seconds))) as response:
                payload = response.read().decode("gbk", "ignore").strip()
        except (HTTPError, URLError, TimeoutError) as exc:
            logger.warning(f"[Tencent] 获取美股 {symbol} 实时行情失败: {exc}")
            return None
        if not payload or '=""' in payload:
            logger.warning(f"[Tencent] 美股 {symbol} 返回空行情")
            return None

        try:
            start = payload.index('"') + 1
            end = payload.rindex('"')
            fields = payload[start:end].split("~")
            if len(fields) < 63:
                raise ValueError(f"field_count={len(fields)}")

            def number(index: int) -> Optional[float]:
                value = fields[index].strip() if len(fields) > index else ""
                if not value:
                    return None
                parsed = float(value)
                return parsed if parsed == parsed else None

            price = number(3)
            volume = number(36) or number(6)
            if price is None or price <= 0 or volume is None or volume <= 0:
                raise ValueError("missing positive price or volume")
            timestamp = None
            timestamp_text = fields[30].strip()
            if timestamp_text:
                timestamp = (
                    datetime.strptime(timestamp_text, "%Y-%m-%d %H:%M:%S")
                    .replace(tzinfo=ZoneInfo("America/New_York"))
                    .astimezone(timezone.utc)
                    .isoformat()
                )
            pb_ratio = number(51)
            missing_fields = ["volume_ratio"]
            if pb_ratio is None:
                missing_fields.append("pb_ratio")
            circ_mv = number(44)
            total_mv = number(45)
            quote = UnifiedRealtimeQuote(
                code=symbol,
                name=fields[46].strip() or fields[1].strip() or symbol,
                source=RealtimeSource.TENCENT,
                provider_timestamp=timestamp,
                market="us",
                currency=fields[35].strip().upper() or "USD",
                data_quality="partial",
                missing_fields=missing_fields,
                price=price,
                change_pct=number(32),
                change_amount=number(31),
                volume=int(volume),
                amount=number(37) or price * volume,
                turnover_rate=number(38),
                amplitude=number(43),
                open_price=number(5),
                high=number(33),
                low=number(34),
                pre_close=number(4),
                pe_ratio=number(39),
                pb_ratio=pb_ratio,
                circ_mv=(circ_mv * 100000000 if circ_mv is not None else None),
                total_mv=(total_mv * 100000000 if total_mv is not None else None),
                high_52w=number(48),
                low_52w=number(49),
            )
            logger.info(f"[Tencent] 获取美股 {symbol} 实时行情成功: 价格={price}")
            return quote
        except Exception as exc:
            logger.warning(f"[Tencent] 解析美股 {symbol} 行情失败: {exc}")
            return None

    @staticmethod
    def _has_usable_provider_timestamp(quote: Optional[UnifiedRealtimeQuote]) -> bool:
        if quote is None or not quote.has_basic_data():
            return False
        raw_timestamp = getattr(quote, "provider_timestamp", None)
        if not raw_timestamp:
            return False
        try:
            parsed = datetime.fromisoformat(str(raw_timestamp).replace("Z", "+00:00"))
        except ValueError:
            return False
        return parsed.tzinfo is not None

    def get_cross_market_us_realtime_quote(
        self,
        stock_code: str,
    ) -> Optional[UnifiedRealtimeQuote]:
        """Return timestamped US evidence with Tencent first and Yahoo as fallback."""

        symbol = stock_code.strip().upper()
        quote = self._get_us_stock_quote_from_tencent(
            symbol,
            timeout_seconds=CROSS_MARKET_US_HTTP_TIMEOUT_SECONDS,
        )
        if self._has_usable_provider_timestamp(quote):
            return quote

        quote = self._get_yahoo_chart_realtime_quote(
            user_code=symbol,
            yf_symbol=self._convert_stock_code(symbol),
            name=STOCK_NAME_MAP.get(symbol, ""),
            market="us",
            currency="USD",
            request_timeout_seconds=CROSS_MARKET_US_HTTP_TIMEOUT_SECONDS,
        )
        return quote if self._has_usable_provider_timestamp(quote) else None

    def get_cross_market_us_premarket_quote(
        self,
        stock_code: str,
    ) -> Optional[UnifiedRealtimeQuote]:
        """Return a timestamped extended-hours minute for premarket evidence."""

        symbol = stock_code.strip().upper()
        quote = self._get_yahoo_chart_realtime_quote(
            user_code=symbol,
            yf_symbol=self._convert_stock_code(symbol),
            name=STOCK_NAME_MAP.get(symbol, ""),
            market="us",
            currency="USD",
            include_pre_post=True,
            request_timeout_seconds=CROSS_MARKET_US_HTTP_TIMEOUT_SECONDS,
        )
        return quote if self._has_usable_provider_timestamp(quote) else None

    def get_cross_market_us_premarket_quotes(
        self,
        stock_codes: List[str],
        *,
        timeout_seconds: float = CROSS_MARKET_US_HTTP_TIMEOUT_SECONDS,
    ) -> Dict[str, UnifiedRealtimeQuote]:
        """Return live Yahoo ticks while retaining the stream between scheduler polls."""

        import yfinance as yf

        code_by_stream_symbol = {
            self._convert_stock_code(str(code or "").strip().upper()): str(code or "").strip().upper()
            for code in stock_codes
            if str(code or "").strip()
        }
        code_by_stream_symbol = {
            symbol: code
            for symbol, code in code_by_stream_symbol.items()
            if symbol
        }
        if not code_by_stream_symbol:
            return {}

        with self._premarket_stream_start_lock:
            with self._premarket_stream_lock:
                listener = self._premarket_stream_thread
                current_mapping = dict(self._premarket_stream_code_by_symbol)
                compatible = bool(
                    listener is not None
                    and listener.is_alive()
                    and all(
                        current_mapping.get(symbol) == code
                        for symbol, code in code_by_stream_symbol.items()
                    )
                )
            if not compatible:
                self._stop_cross_market_us_premarket_stream()
                self._start_cross_market_us_premarket_stream(
                    yf,
                    code_by_stream_symbol,
                )

        self._refresh_cross_market_us_premarket_stream_idle_timer()
        with self._premarket_stream_lock:
            completed = self._premarket_stream_completed
        completed.wait(max(1.0, float(timeout_seconds)))
        with self._premarket_stream_lock:
            return {
                code: self._premarket_stream_quotes[code]
                for code in code_by_stream_symbol.values()
                if code in self._premarket_stream_quotes
            }

    def _start_cross_market_us_premarket_stream(
        self,
        yf,
        code_by_stream_symbol: Dict[str, str],
    ) -> None:
        websocket = yf.WebSocket(verbose=False)
        completed = threading.Event()
        with self._premarket_stream_lock:
            self._premarket_stream_generation += 1
            generation = self._premarket_stream_generation

        def handle_message(message: Dict[str, Any]) -> None:
            stream_symbol = str(message.get("id") or "").strip().upper()
            code = code_by_stream_symbol.get(stream_symbol)
            price = _safe_number(message.get("price"))
            raw_time = _safe_number(message.get("time"))
            if code is None or price is None or price <= 0 or raw_time is None:
                return
            timestamp_seconds = (
                raw_time / 1000.0
                if raw_time >= 10_000_000_000
                else raw_time
            )
            provider_at = datetime.fromtimestamp(timestamp_seconds, tz=timezone.utc)
            change_amount = _safe_number(message.get("change"))
            change_pct = _safe_number(message.get("change_percent"))
            pre_close = (
                price - change_amount
                if change_amount is not None and price - change_amount > 0
                else None
            )
            quote = UnifiedRealtimeQuote(
                code=code,
                name=STOCK_NAME_MAP.get(code, ""),
                source=RealtimeSource.YAHOO_STREAMER,
                fetched_at=datetime.now(timezone.utc).isoformat(),
                provider_timestamp=provider_at.isoformat(),
                market="us",
                currency="USD",
                data_quality="partial",
                missing_fields=["volume", "amount"],
                price=price,
                change_pct=change_pct,
                change_amount=change_amount,
                pre_close=pre_close,
            )
            with self._premarket_stream_lock:
                if generation != self._premarket_stream_generation:
                    return
                self._premarket_stream_quotes[code] = quote
                if len(self._premarket_stream_quotes) >= len(code_by_stream_symbol):
                    completed.set()

        def listen() -> None:
            try:
                websocket.listen(handle_message)
            except Exception as exc:  # noqa: BLE001 - the next poll restarts the stream.
                logger.warning("Yahoo premarket streamer stopped: %s", exc)

        websocket.subscribe(list(code_by_stream_symbol))
        listener = threading.Thread(
            target=listen,
            daemon=True,
            name="dsa-yahoo-premarket-stream",
        )
        with self._premarket_stream_lock:
            self._premarket_stream_websocket = websocket
            self._premarket_stream_thread = listener
            self._premarket_stream_quotes = {}
            self._premarket_stream_code_by_symbol = dict(code_by_stream_symbol)
            self._premarket_stream_completed = completed
        listener.start()

    def _refresh_cross_market_us_premarket_stream_idle_timer(self) -> None:
        with self._premarket_stream_lock:
            previous = self._premarket_stream_idle_timer
            self._premarket_stream_idle_generation += 1
            generation = self._premarket_stream_idle_generation
            timer = threading.Timer(
                CROSS_MARKET_US_STREAM_IDLE_SECONDS,
                self._expire_cross_market_us_premarket_stream,
                args=(generation,),
            )
            timer.daemon = True
            self._premarket_stream_idle_timer = timer
        if previous is not None:
            previous.cancel()
        timer.start()

    def _expire_cross_market_us_premarket_stream(self, generation: int) -> None:
        self._stop_cross_market_us_premarket_stream(
            expected_idle_generation=generation,
        )

    def _stop_cross_market_us_premarket_stream(
        self,
        *,
        expected_idle_generation: Optional[int] = None,
    ) -> None:
        with self._premarket_stream_lock:
            if (
                expected_idle_generation is not None
                and expected_idle_generation
                != self._premarket_stream_idle_generation
            ):
                return
            websocket = self._premarket_stream_websocket
            listener = self._premarket_stream_thread
            idle_timer = self._premarket_stream_idle_timer
            self._premarket_stream_generation += 1
            self._premarket_stream_idle_generation += 1
            self._premarket_stream_websocket = None
            self._premarket_stream_thread = None
            self._premarket_stream_quotes = {}
            self._premarket_stream_code_by_symbol = {}
            self._premarket_stream_completed = threading.Event()
            self._premarket_stream_idle_timer = None
        if idle_timer is not None:
            idle_timer.cancel()
        if websocket is None:
            return
        websocket_logger = getattr(websocket, "logger", None)
        logger_was_disabled = bool(getattr(websocket_logger, "disabled", False))
        try:
            if websocket_logger is not None:
                websocket_logger.disabled = True
            websocket.close()
        except Exception as exc:  # noqa: BLE001 - shutdown is best effort.
            logger.debug("Yahoo premarket streamer close failed: %s", exc)
        finally:
            if (
                listener is not None
                and listener is not threading.current_thread()
                and listener.is_alive()
            ):
                listener.join(timeout=1.0)
            if websocket_logger is not None:
                websocket_logger.disabled = logger_was_disabled

    def _get_us_stock_quote_fallback(self, stock_code: str) -> Optional[UnifiedRealtimeQuote]:
        return (
            self._get_yahoo_chart_realtime_quote(
                user_code=stock_code,
                yf_symbol=self._convert_stock_code(stock_code),
                name=STOCK_NAME_MAP.get(stock_code.strip().upper(), ""),
                market="us",
                currency="USD",
            )
            or self._get_us_stock_quote_from_tencent(stock_code)
            or self._get_us_stock_quote_from_stooq(stock_code)
        )

    def _get_yahoo_chart_realtime_quote(
        self,
        *,
        user_code: str,
        yf_symbol: str,
        name: str,
        market: Optional[str],
        currency: Optional[str],
        include_pre_post: bool = False,
        request_timeout_seconds: float = 15.0,
    ) -> Optional[UnifiedRealtimeQuote]:
        """Fetch the latest real provider minute without yfinance crumb state."""

        encoded_symbol = url_quote(str(yf_symbol or "").strip(), safe="")
        if not encoded_symbol:
            return None
        include_pre_post_text = "true" if include_pre_post else "false"
        urls = [
            (
                f"https://{host}/v8/finance/chart/{encoded_symbol}"
                f"?interval=1m&range=1d&includePrePost={include_pre_post_text}"
            )
            for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
        ]
        try:
            payload = None
            last_error: Optional[Exception] = None
            for url in urls:
                request = Request(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (compatible; DSA/1.0; +https://github.com/ZhuLinsen/daily_stock_analysis)",
                        "Accept": "application/json",
                    },
                )
                try:
                    with urlopen(
                        request,
                        timeout=max(1.0, float(request_timeout_seconds)),
                    ) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                    break
                except (HTTPError, URLError, TimeoutError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    last_error = exc
            if payload is None:
                raise last_error or ValueError("yahoo_chart_payload_unavailable")
            chart = payload.get("chart") if isinstance(payload, dict) else None
            results = chart.get("result") if isinstance(chart, dict) else None
            result = results[0] if isinstance(results, list) and results else None
            if not isinstance(result, dict):
                return None
            timestamps = result.get("timestamp")
            indicators = result.get("indicators")
            quote_rows = indicators.get("quote") if isinstance(indicators, dict) else None
            values = quote_rows[0] if isinstance(quote_rows, list) and quote_rows else None
            closes = values.get("close") if isinstance(values, dict) else None
            if not isinstance(timestamps, list) or not isinstance(closes, list):
                return None
            usable = [
                index
                for index, (timestamp, close) in enumerate(zip(timestamps, closes))
                if _safe_number(timestamp) is not None and _safe_number(close) is not None
            ]
            if not usable:
                return None
            latest_index = usable[-1]
            price = float(closes[latest_index])
            provider_timestamp = datetime.fromtimestamp(
                float(timestamps[latest_index]),
                tz=timezone.utc,
            ).isoformat()
            meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
            previous_close = _safe_number(
                meta.get("chartPreviousClose") or meta.get("previousClose")
            )

            def available_numbers(field: str) -> List[float]:
                raw = values.get(field) if isinstance(values, dict) else None
                if not isinstance(raw, list):
                    return []
                return [
                    float(item)
                    for item in raw[: latest_index + 1]
                    if _safe_number(item) is not None
                ]

            opens = available_numbers("open")
            highs = available_numbers("high")
            lows = available_numbers("low")
            volumes = available_numbers("volume")
            change_amount = price - previous_close if previous_close and previous_close > 0 else None
            change_pct = change_amount / previous_close * 100.0 if change_amount is not None else None
            amplitude = (
                (max(highs) - min(lows)) / previous_close * 100.0
                if highs and lows and previous_close and previous_close > 0
                else None
            )
            missing_fields = [
                field
                for field, value in {
                    "prev_close": previous_close,
                    "volume": sum(volumes) if volumes else None,
                    "amount": None,
                    "pe_ratio": None,
                    "pb_ratio": None,
                }.items()
                if value is None
            ]
            return UnifiedRealtimeQuote(
                code=str(user_code or "").strip().upper(),
                name=str(name or meta.get("shortName") or user_code or "").strip(),
                source=RealtimeSource.YAHOO_CHART,
                provider_timestamp=provider_timestamp,
                market=market,
                currency=str(meta.get("currency") or currency or "").strip().upper() or None,
                data_quality="partial" if missing_fields else "ok",
                missing_fields=missing_fields or None,
                price=price,
                change_pct=round(change_pct, 6) if change_pct is not None else None,
                change_amount=round(change_amount, 6) if change_amount is not None else None,
                volume=int(sum(volumes)) if volumes else None,
                amount=None,
                volume_ratio=None,
                turnover_rate=None,
                amplitude=round(amplitude, 6) if amplitude is not None else None,
                open_price=opens[0] if opens else None,
                high=max(highs) if highs else None,
                low=min(lows) if lows else None,
                pre_close=previous_close,
                pe_ratio=None,
                pb_ratio=None,
                total_mv=None,
                circ_mv=None,
            )
        except (HTTPError, URLError, TimeoutError, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.warning("[Yahoo Chart] 获取 %s 分钟行情失败: %s", yf_symbol, exc)
            return None

    def _get_us_index_realtime_quote(
        self,
        user_code: str,
        yf_symbol: str,
        index_name: str,
        *,
        market: str = "us",
        currency: Optional[str] = None,
    ) -> Optional[UnifiedRealtimeQuote]:
        """
        Get realtime quote for US index (e.g. SPX -> ^GSPC).

        Args:
            user_code: User input code (e.g. SPX)
            yf_symbol: Yahoo Finance symbol (e.g. ^GSPC)
            index_name: Chinese name for the index

        Returns:
            UnifiedRealtimeQuote or None
        """
        direct_quote = self._get_yahoo_chart_realtime_quote(
            user_code=user_code,
            yf_symbol=yf_symbol,
            name=index_name,
            market=market,
            currency=currency,
        )
        if direct_quote is not None:
            return direct_quote

        import yfinance as yf

        try:
            logger.debug(f"[Yfinance] 获取美股指数 {user_code} ({yf_symbol}) 实时行情")
            ticker = yf.Ticker(yf_symbol)

            try:
                info = ticker.fast_info
                if info is None:
                    raise ValueError("fast_info is None")
                price = getattr(info, 'lastPrice', None) or getattr(info, 'last_price', None)
                prev_close = getattr(info, 'previousClose', None) or getattr(info, 'previous_close', None)
                open_price = getattr(info, 'open', None)
                high = getattr(info, 'dayHigh', None) or getattr(info, 'day_high', None)
                low = getattr(info, 'dayLow', None) or getattr(info, 'day_low', None)
                volume = getattr(info, 'lastVolume', None) or getattr(info, 'last_volume', None)
            except Exception:
                logger.debug("[Yfinance] fast_info 失败，尝试 history 方法")
                hist = ticker.history(period='2d')
                if hist.empty:
                    logger.warning(f"[Yfinance] 无法获取 {yf_symbol} 的数据")
                    return self._get_yahoo_chart_realtime_quote(
                        user_code=user_code,
                        yf_symbol=yf_symbol,
                        name=index_name,
                        market=market,
                        currency=currency,
                    )
                today = hist.iloc[-1]
                prev = hist.iloc[-2] if len(hist) > 1 else today
                price = float(today['Close'])
                prev_close = float(prev['Close'])
                open_price = float(today['Open'])
                high = float(today['High'])
                low = float(today['Low'])
                volume = int(today['Volume'])

            change_amount = None
            change_pct = None
            if price is not None and prev_close is not None and prev_close > 0:
                change_amount = price - prev_close
                change_pct = (change_amount / prev_close) * 100

            amplitude = None
            if high is not None and low is not None and prev_close is not None and prev_close > 0:
                amplitude = ((high - low) / prev_close) * 100

            try:
                ticker_info = ticker.info or {}
            except Exception:
                ticker_info = {}
            provider_timestamp = self._latest_minute_provider_timestamp(ticker)
            if provider_timestamp is None:
                direct_quote = self._get_yahoo_chart_realtime_quote(
                    user_code=user_code,
                    yf_symbol=yf_symbol,
                    name=index_name,
                    market=market,
                    currency=currency,
                )
                if direct_quote is not None:
                    return direct_quote
            missing_fields = [
                field
                for field, value in {
                    "price": price,
                    "prev_close": prev_close,
                    "volume": volume,
                    "amount": None,
                    "pe_ratio": None,
                    "pb_ratio": None,
                }.items()
                if value is None
            ]

            quote = UnifiedRealtimeQuote(
                code=user_code,
                name=index_name or user_code,
                source=RealtimeSource.FALLBACK,
                provider_timestamp=provider_timestamp,
                market=market,
                currency=str(ticker_info.get("currency") or currency or "").upper() or None,
                data_quality="partial" if missing_fields else "ok",
                missing_fields=missing_fields or None,
                price=price,
                change_pct=round(change_pct, 2) if change_pct is not None else None,
                change_amount=round(change_amount, 4) if change_amount is not None else None,
                volume=volume,
                amount=None,
                volume_ratio=None,
                turnover_rate=None,
                amplitude=round(amplitude, 2) if amplitude is not None else None,
                open_price=open_price,
                high=high,
                low=low,
                pre_close=prev_close,
                pe_ratio=None,
                pb_ratio=None,
                total_mv=None,
                circ_mv=None,
            )
            logger.info(f"[Yfinance] 获取美股指数 {user_code} 实时行情成功: 价格={price}")
            return quote
        except Exception as e:
            logger.warning(f"[Yfinance] 获取美股指数 {user_code} 实时行情失败: {e}")
            return self._get_yahoo_chart_realtime_quote(
                user_code=user_code,
                yf_symbol=yf_symbol,
                name=index_name,
                market=market,
                currency=currency,
            )

    def get_realtime_quote(self, stock_code: str) -> Optional[UnifiedRealtimeQuote]:
        """
        获取美股/美股指数实时行情数据

        支持美股股票（AAPL、TSLA）和美股指数（SPX、DJI 等）。
        数据来源：yfinance Ticker.info

        Args:
            stock_code: 美股代码或指数代码，如 'AMD', 'AAPL', 'SPX', 'DJI'

        Returns:
            UnifiedRealtimeQuote 对象，获取失败返回 None
        """
        import yfinance as yf

        # 美股指数：使用映射（SPX -> ^GSPC）
        normalized_code = str(stock_code or "").strip().upper()
        alias_symbol, alias_name = get_us_yfinance_alias(normalized_code)
        if alias_symbol:
            return self._get_us_index_realtime_quote(
                user_code=normalized_code,
                yf_symbol=alias_symbol,
                index_name=alias_name,
                market="us",
                currency="USD",
            )
        global_index = GLOBAL_INDEX_REALTIME_MAPPING.get(normalized_code)
        if global_index:
            yf_symbol, index_name, market, currency = global_index
            return self._get_us_index_realtime_quote(
                user_code=normalized_code,
                yf_symbol=yf_symbol,
                index_name=index_name,
                market=market,
                currency=currency,
            )

        yf_symbol, index_name = get_us_index_yf_symbol(stock_code)
        if yf_symbol:
            return self._get_us_index_realtime_quote(
                user_code=stock_code.strip().upper(),
                yf_symbol=yf_symbol,
                index_name=index_name,
            )

        # 仅处理美股股票或 JP/KR/TW suffix-only 股票
        if not (
            self._is_us_stock(stock_code)
            or self._is_jp_kr_suffix_stock(stock_code)
            or self._is_tw_suffix_stock(stock_code)
        ):
            logger.debug(f"[Yfinance] {stock_code} 不是美股或日韩 suffix 代码，跳过")
            return None

        try:
            symbol = self._convert_stock_code(stock_code)
            is_us_symbol = self._is_us_stock(symbol)
            suffix_market = get_suffix_market(symbol)
            if suffix_market in {"jp", "kr", "tw"}:
                direct_quote = self._get_yahoo_chart_realtime_quote(
                    user_code=stock_code,
                    yf_symbol=symbol,
                    name=STOCK_NAME_MAP.get(symbol, ""),
                    market=suffix_market,
                    currency=None,
                )
                if direct_quote is not None:
                    return direct_quote
            logger.debug(f"[Yfinance] 获取 {symbol} 实时行情")

            ticker = yf.Ticker(symbol)

            # 尝试获取 fast_info（更快，但字段较少）
            try:
                info = ticker.fast_info
                if info is None:
                    raise ValueError("fast_info is None")

                price = getattr(info, 'lastPrice', None) or getattr(info, 'last_price', None)
                prev_close = getattr(info, 'previousClose', None) or getattr(info, 'previous_close', None)
                open_price = getattr(info, 'open', None)
                high = getattr(info, 'dayHigh', None) or getattr(info, 'day_high', None)
                low = getattr(info, 'dayLow', None) or getattr(info, 'day_low', None)
                volume = getattr(info, 'lastVolume', None) or getattr(info, 'last_volume', None)
                market_cap = getattr(info, 'marketCap', None) or getattr(info, 'market_cap', None)

            except Exception:
                # 回退到 history 方法获取最新数据
                logger.debug("[Yfinance] fast_info 失败，尝试 history 方法")
                hist = ticker.history(period='2d')
                if hist.empty:
                    if is_us_symbol:
                        logger.warning(f"[Yfinance] 无法获取 {symbol} 的数据，尝试 Tencent/Stooq 兜底")
                        return self._get_us_stock_quote_fallback(symbol)
                    logger.warning(f"[Yfinance] 无法获取 {symbol} 的数据")
                    return self._get_yahoo_chart_realtime_quote(
                        user_code=stock_code,
                        yf_symbol=symbol,
                        name=STOCK_NAME_MAP.get(symbol, ""),
                        market=suffix_market,
                        currency=None,
                    )

                today = hist.iloc[-1]
                prev = hist.iloc[-2] if len(hist) > 1 else today

                price = float(today['Close'])
                prev_close = float(prev['Close'])
                open_price = float(today['Open'])
                high = float(today['High'])
                low = float(today['Low'])
                volume = int(today['Volume'])
                market_cap = None

            # 计算涨跌幅
            change_amount = None
            change_pct = None
            if price is not None and prev_close is not None and prev_close > 0:
                change_amount = price - prev_close
                change_pct = (change_amount / prev_close) * 100

            # 计算振幅
            amplitude = None
            if high is not None and low is not None and prev_close is not None and prev_close > 0:
                amplitude = ((high - low) / prev_close) * 100

            # 获取股票名称与 provider 元数据
            try:
                ticker_info = ticker.info or {}
            except Exception:
                ticker_info = {}
            try:
                info_name = ticker_info.get('shortName', '') or ticker_info.get('longName', '') or ''
                name = info_name if is_meaningful_stock_name(info_name, symbol) else STOCK_NAME_MAP.get(symbol, '')
            except Exception:
                name = STOCK_NAME_MAP.get(symbol, '')

            missing_fields = [
                field
                for field, value in {
                    "price": price,
                    "prev_close": prev_close,
                    "volume": volume,
                    "amount": None,
                    "pe_ratio": None,
                    "pb_ratio": None,
                }.items()
                if value is None
            ]
            provider_timestamp = self._latest_minute_provider_timestamp(ticker)
            if provider_timestamp is None:
                direct_quote = self._get_yahoo_chart_realtime_quote(
                    user_code=stock_code,
                    yf_symbol=symbol,
                    name=name,
                    market=suffix_market or ("us" if is_us_symbol else None),
                    currency=str(ticker_info.get("currency") or "").upper() or None,
                )
                if direct_quote is not None:
                    return direct_quote
            quote = UnifiedRealtimeQuote(
                code=symbol,
                name=name,
                source=RealtimeSource.FALLBACK,
                provider_timestamp=provider_timestamp,
                market=suffix_market or ("us" if is_us_symbol else None),
                currency=str(ticker_info.get("currency") or "").upper() or None,
                data_quality="partial" if missing_fields else "ok",
                missing_fields=missing_fields or None,
                price=price,
                change_pct=round(change_pct, 2) if change_pct is not None else None,
                change_amount=round(change_amount, 4) if change_amount is not None else None,
                volume=volume,
                amount=None,  # yfinance 不直接提供成交额
                volume_ratio=None,
                turnover_rate=None,
                amplitude=round(amplitude, 2) if amplitude is not None else None,
                open_price=open_price,
                high=high,
                low=low,
                pre_close=prev_close,
                pe_ratio=None,
                pb_ratio=None,
                total_mv=market_cap,
                circ_mv=None,
            )

            logger.info(f"[Yfinance] 获取 {symbol} 实时行情成功: 价格={price}")
            return quote

        except Exception as e:
            if self._is_us_stock(stock_code):
                logger.warning(
                    f"[Yfinance] 获取美股 {stock_code} 实时行情失败: {e}，尝试 Tencent/Stooq 兜底"
                )
                return self._get_us_stock_quote_fallback(stock_code)
            logger.warning(f"[Yfinance] 获取 {stock_code} 实时行情失败: {e}")
            symbol = self._convert_stock_code(stock_code)
            return self._get_yahoo_chart_realtime_quote(
                user_code=stock_code,
                yf_symbol=symbol,
                name=STOCK_NAME_MAP.get(symbol, ""),
                market=get_suffix_market(symbol),
                currency=None,
            )


if __name__ == "__main__":
    # 测试代码
    logging.basicConfig(level=logging.DEBUG)

    fetcher = YfinanceFetcher()

    try:
        df = fetcher.get_daily_data('600519')  # 茅台
        print(f"获取成功，共 {len(df)} 条数据")
        print(df.tail())
    except Exception as e:
        print(f"获取失败: {e}")
