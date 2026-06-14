# stock_data 数据源接入方案

> 本文档对应一次**部分替换 + 增量增强**的数据源迁移评估。
> 目标：把 QuantDinger 的 A 股 / 港股 / 美股数据获取路径部分切换到外部 `stock_data` 服务，同时保留所有现有链路作为兜底。

---

## 一、背景

- `stock_data` 是一个基于 FastAPI 的本地数据聚合服务，封装了 Tushare / Baostock / AkShare / Yfinance / Zhitu / Tencent / EastMoney / THS / Cninfo / Myquant 共 10 个上游数据源，对外暴露 27 个 GET endpoint。
- QuantDinger 现有 `app/data_sources/`（7 个 `BaseDataSource` 子类）+ `app/data_providers/`（10 个复合视图）+ 多条 REST 路由，覆盖 Crypto / Forex / Futures / USStock / CNStock / HKStock / MOEX 七个市场。
- 评估目的是回答两件事：**哪些能切？哪些不能切？切的话怎么动？**

---

## 二、QuantDinger 现有数据能力

### 2.1 抽象接口（`app/data_sources/base.py`）

`BaseDataSource` 规定两个核心方法：

| 方法 | 签名 | 返回 |
|---|---|---|
| `get_kline` | `(symbol, timeframe, limit, before_time=None, after_time=None)` | `[{"time": unix_s, "open", "high", "low", "close", "volume"}]` |
| `get_ticker` | `(symbol)` | CCXT 风格 ticker：`{last, change, changePercent, high, low, open, previousClose}` |

支持的 timeframe：`1m / 3m / 5m / 15m / 30m / 1H / 4H / 1D / 1W`。

### 2.2 各市场数据源（`app/data_sources/`）

| 市场 | 主数据源 | 回退链 | 备注 |
|---|---|---|---|
| **Crypto** | CCXT（默认 binance，可换 okx/bybit/bitget/gate/kraken/htx/coinbase） | 无（CCXT 自带） | 多交易所、多 `market_type`（spot/swap），`get_kline` 分页 300 条一批 |
| **CNStock** | Twelve Data → **Tencent** → yfinance → **AkShare (Eastmoney)** | 4 级 | 分钟线只走 yfinance/AkShare，Tencent 仅支持日/周/月 |
| **HKStock** | Twelve Data → Tencent → yfinance → AkShare | 4 级 | 符号归一为 `HK00700` |
| **USStock** | **yfinance** 主，**Finnhub** 仅做 daily 兜底 | 2 级 | minute 限 7 天窗口；ticker Finnhub → yfinance fast_info → info → 1m bar |
| **Forex** | Twelve Data → **Tiingo** → yfinance | 3 级 | 12 个货币对硬编码；1m 需要 Tiingo 付费层 |
| **Futures** | 传统（GC/SI/CL…）：Twelve Data → yfinance → Tiingo；**加密合约**：CCXT binanceusdm | 2 条路径 | Gold/Silver 走 Tiingo spot |
| **MOEX** | Moscow Exchange ISS API（`iss.moex.com`） | 无 | 时区按 Europe/Moscow 处理 |

辅助模块（不直接走 `BaseDataSource`）：

- `app/data_sources/tencent.py` — 腾讯财经 HTTP 工具
- `app/data_sources/asia_stock_kline.py` — TwelveData/yfinance/AkShare 分钟/周线 helper
- `app/data_sources/cn_hk_fundamentals.py` — **基本面**：PE/PB/PS/PEG/market_cap/ROE/EPS/income_statement/balance_sheet/cash_flow/earnings/profile（A+H，全部走 Twelve Data + AkShare）

### 2.3 复合视图层（`app/data_providers/`，全部走 SWR `cached_or_compute`）

| Provider | 输出 | 数据源 |
|---|---|---|
| `crypto.py` | 15 个主流币 + 30 个热力图币 | CCXT + yfinance + **CoinGecko** + **CoinCap** |
| `forex.py` | 12 个货币对报价 | TD + Tiingo + yfinance |
| `commodities.py` | 12 个商品（GC/SI/CL/BZ/HG/NG/PL/PA/ALI/ZW/ZC/SB） | TD + yfinance + Tiingo |
| `indices.py` | 10 个全球指数（^GSPC/^DJI/^IXIC/^GDAXI/^FTSE/^FCHI/^N225/^KS11/^AXJO/^BSESN） | yfinance batch |
| `news.py` | 中英文财经新闻 | 搜索服务（Google/Bing/Tavily/SerpAPI） |
| `sentiment.py` | 恐惧贪婪、VIX、DXY、收益率曲线、VXN、GVZ、PCR | alternative.me + yfinance + AkShare |
| `heatmap.py` | 7 类资产热力图 | ThreadPool 并行 |
| `opportunities.py` | 异动扫描（±5%/±15% 阈值） | 各数据源 |
| `economic_calendar.py` | 财经日历（lookback 3 天 / 展望 14 天） | **Finnhub** |
| `adanos_sentiment.py` | Reddit/X/News/Polymarket 情绪对比 | Adanos API |

### 2.4 路由暴露

| 路由 | 入口 |
|---|---|
| `/api/kline/kline` | `kline.py` → `KlineService.get_kline` |
| `/api/market/symbols/search` `symbols/hot` `watchlist/get` `watchlist/prices` `price` `types` `config` | `market.py` |
| `/api/global-market/overview` `heatmap` `news` `calendar` `sentiment` `adanos-sentiment` `opportunities` `refresh` | `global_market.py` |
| `/api/agent/v1/markets` `/symbols` `/klines` `/price` | `agent_v1/markets.py` |
| `/api/agent/v1/quick-trade/orders` | `agent_v1/quick_trade.py`（取最新价 → `KlineService`） |

### 2.5 缓存 / 限流 / 熔断（实际接线状态）

| 层 | 状态 |
|---|---|
| `KlineService` 内存 + Redis 缓存（per-key） | ✅ 在用 |
| `data_providers/__init__.py` 的 SWR `cached_or_compute` | ✅ 在用（线程池 4 worker） |
| `cache_manager.py`（DataCache 三个全局） | ❌ 已废弃 |
| `circuit_breaker.py` | ❌ 定义了但未启用 |
| `rate_limiter.py`（_tencent / _eastmoney / _akshare） | 仅 `_tencent_limiter` 在用 |
| `retry_with_backoff` 装饰器 | 用在 `tencent.fetch_quote/kline`、`asia_stock_kline` 重试循环 |

---

## 三、stock_data 现有能力

### 3.1 27 个 REST endpoint（全部 GET，挂在 `/api/v1/`）

| Endpoint | 参数 | 说明 |
|---|---|---|
| `/stocks/{code}/quote` | `stock_code` | **A 增强版**报价：价/涨跌幅/OHLC/量额 + **PE_TTM/PB/总市值/流通市值/换手率/振幅/量比**（来自 Tencent 字段 39/43–49/52） |
| `/stocks/{code}/history` | `period=daily\|weekly\|monthly`, `days=1–365`, `start_date`, `end_date`, `adjust=qfq\|hfq`, `indicators=ma,macd,...` | 日/周/月 K 线 + **14 个技术指标自动附加** |
| `/stocks/{code}/intraday` | `period=1\|5\|15\|30\|60`, `adjust` | 分钟线，**仅当日一根交易日** |
| `/indices` `/indices/{code}/quote` `/indices/{code}/history` `/indices/{code}/intraday` | `period` / `days` | 沪深300/上证50/科创50 等 14 个 CSI 指数 + 2 个 HK 指数 + 6 个 US 指数（SPX/DJI/IXIC/NDX/VIX/SPY） |
| `/stocks?market=csi\|hk\|us` | `refresh`, `offset`, `limit=100` | 股票清单（**US 仅有标普 500 成分股**） |
| `/calendar` | `refresh` | A 股交易日历 |
| `/boards` `/boards/{code}/stocks` | `type=concept\|industry`, `include_quote` | 概念/行业板块 + 成分股 |
| `/pools?type=zt\|dt\|zbgc&date=` | | 涨停/跌停/炸板池（**当天不缓存**，直透上游） |
| `/stocks/{code}/dragon-tiger` `/dragon-tiger/daily` | `stock_code`, `trade_date`, `look_back=30` | 龙虎榜 |
| `/stocks/{code}/margin` | `page_size` | 融资融券 8 字段 |
| `/stocks/{code}/block-trade` | `page_size` | 大宗交易 |
| `/stocks/{code}/holder-num` | `page_size` | 股东户数（季度） |
| `/stocks/{code}/dividend` | `page_size` | 分红送转 |
| `/stocks/{code}/fund-flow` `/fund-flow/daily` | | 资金流（分钟级 / 120 日） |
| `/hot/topics` | `date` | 同花顺热门话题 |
| `/north-flow/realtime` | | 沪深股通实时资金流 |
| `/stocks/{code}/reports` `/reports/{id}/pdf` | `max_pages` | 研报列表 + PDF 下载 |
| `/stocks/{code}/announcements` | `page_size` | 巨潮公告 |
| `/indicators/catalog` | | 14 个技术指标字典 |
| `/health` | `details` | 服务健康 |

> ⚠️ 未启用 OpenAPI（`docs_url=None`），**无认证**（默认 CORS 锁 localhost）。

### 3.2 上游适配器（10 个 fetcher）

| Provider | Markets | 能力 | 需要 token |
|---|---|---|---|
| **Tushare Pro** | csi | 日/周/月 + 实时（需 tick 权限） | TUSHARE_TOKEN |
| **Baostock** | csi | 日/周/月 + **1/5/15/30/60 分钟（仅个股）** + 交易日历 + 指数 | 无 |
| **Akshare** | csi, hk | 全量 DWM + 分钟 + 实时 + 股票池 + 板块 + 公告 | 无 |
| **Yfinance** | csi, hk, us | DWM + 分钟（≤60 天窗口） + 实时（Stooq US 兜底） | 无 |
| **Zhitu** | csi | 实时 + 涨跌停池；5/15/30/60 分钟（**无 1 分钟**） | ZHITU_TOKEN |
| **Tencent** | csi, hk | 实时（带 PE/PB/换手率等增强字段） | 无 |
| **EastMoney 数据中心** | csi | 龙虎榜、融资融券、大宗交易、股东户数、分红、资金流、研报 | 无 |
| **THS 同花顺** | csi | 热门话题、沪深股通 | 无 |
| **Cninfo 巨潮** | csi | 公告 | 无 |
| **Myquant 掘金** | csi | DWM + 5/15/30/60 分钟（仅当日） + 实时（仅价） | MYQUANT_TOKEN |

Failover：`DataFetcherManager._with_failover` 按 capability + market + 优先级挑；优先级可通过 `*_PRIORITY` 环境变量覆盖。

熔断：`REALTIME_CIRCUIT_BREAKER`（threshold=3, cooldown=300s, half_open=1）。

### 3.3 存储 & 缓存

- **SQLite**（`stock_cache.db`），5 张表：`stock_list / trade_calendar / stock_board / stock_board_stock / pool_daily`
- **K 线不持久化**，每次请求穿透上游；TTLCache 内存缓存（60s–7200s）
- **无 Redis、无 cron、无 worker**

### 3.4 频率覆盖矩阵

| 频率 | Baostock | Tushare | Akshare | Yfinance | Zhitu | Myquant |
|---|:-:|:-:|:-:|:-:|:-:|:-:|
| 日/周/月 | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ (d only 指数) |
| 1 分钟 | ✅ 仅个股 | ❌ | ✅ A 股 + 指数 | ✅ | ❌ | ❌ |
| 5/15/30/60 分钟 | ✅ 仅个股 | ❌ | ✅ | ✅ | ✅ | ✅ |

> ⚠️ **关键限制**：分钟线全部**只返回当天一根交易日**，没有跨日历史分钟归档。

---

## 四、对比矩阵：QuantDinger 需要 vs stock_data 能给

### ✅ 完全支持（可直接切换，零妥协）

| QuantDinger 需求 | stock_data 对应能力 | 切换路径 |
|---|---|---|
| CNStock 日/周/月 K 线 + 复权 | `/stocks/{code}/history?period=daily&adjust=qfq\|hfq` | 1:1 替换 |
| CNStock 实时报价 | `/stocks/{code}/quote`（**还多 PE/PB/换手率/振幅/量比**） | 字段映射即可 |
| CNStock 股票清单 | `/stocks?market=csi` | 1:1 替换 |
| CNStock 交易日历 | `/calendar` | 1:1 替换 |
| CNStock 14 个技术指标 | `/history?indicators=ma,macd,boll,kdj,rsi,wr,bias,cci,atr,obv,roc,dmi,sar,kc` | 服务端直出 |
| CNStock 当日分钟 K（1/5/15/30/60m） | `/stocks/{code}/intraday` | 仅当日 |
| CSI 指数日 K + 实时 + 板块 | `/indices` `/indices/{code}/quote` `/indices/{code}/history` `/boards` `/boards/{code}/stocks` | 1:1 |
| A 股特色数据（龙虎榜/融资融券/大宗交易/股东户数/分红/资金流/热门话题/北向资金/研报/公告/涨跌停池） | 全部 endpoint | **纯增量**，QuantDinger 现无此模块 |
| 14 个技术指标目录 | `/indicators/catalog` | 给 AI Agent 提示用 |

### ⚠️ 部分支持（有取舍）

| QuantDinger 需求 | 限制 | 缓解方案 |
|---|---|---|
| CNStock 分钟 K 线（多日历史） | stock_data 单次只返回**当日**；Akshare 也是单日 | (a) 接受只做当日策略 (b) 在 QuantDinger 侧加分钟级 K 线 SQLite 缓存，按需聚合 (c) 仍走 yfinance/AkShare 分钟路径 |
| HKStock 日/周/月 K 线 + 实时 | 全部支持 ✅，**但 HK 股票清单仅 Akshare `stock_hk_spot_em`（很有限）** | 用 `/stocks?market=hk` 取清单，或接受小清单 |
| USStock 日/周/月 K 线 + 实时 | 全部支持 ✅ | OK |
| USStock 股票清单 | **只 S&P 500 成分股**，无完整美股 | 仍走 yfinance/Finnhub 兜底 |
| HKStock 分钟 K | **不支持**（README 明说） | 仍走 yfinance / AkShare |
| USStock 分钟 K | Yfinance 60 天窗口；stock_data 也只当日 | QuantDinger 自有缓存可缓解 |
| 基本面（income/balance/cashflow/ratios） | stock_data **没有三大表 fetcher**；Myquant SDK 支持但没接；东财只有 PE/分红/股东数 | 保留 Twelve Data + AkShare fallback；可新增 Myquant fetcher 补齐 |
| VIX / DXY / 收益率曲线 / PCR 等情绪指标 | stock_data **没有**（虽然 `/indices/{code}/quote` 能给 VIX 价，但 sentiment 整套没有） | 保留 yfinance + alternative.me |
| 全球指数（^GSPC/^DJI/^IXIC/^GDAXI/^FTSE/^FCHI/^N225/^KS11/^AXJO/^BSESN） | stock_data 仅有 6 个 US 指数（SPX/DJI/IXIC/NDX/VIX/SPY），**无德/英/法/日/韩/澳/印** | 仍走 yfinance |

### ❌ 不支持（必须保留 QuantDinger 现有数据源）

| QuantDinger 需求 | stock_data 状态 | 后果 |
|---|---|---|
| **Crypto 全部**（CCXT 任意交易所，spot/swap 永续） | ❌ 完全无 | **必须保留** CCXT，不可替换 |
| **Forex**（EURUSD/GBPUSD/USDJPY…） | ❌ 完全无 | **必须保留** Twelve Data / Tiingo / yfinance |
| **Futures 传统合约**（GC/SI/CL/BZ/HG/NG/PL/PA/ALI/ZW/ZC/SB） | ❌ 完全无 | **必须保留** Twelve Data / Tiingo / yfinance |
| **Futures 加密合约**（Binance 永续等） | ❌ 完全无 | **必须保留** CCXT binanceusdm |
| **MOEX** | ❌ 完全无 | **必须保留** ISS API |
| **新闻**（中英文搜索） | ❌ 完全无（cninfo 仅公告） | **必须保留** 搜索服务 |
| **Fear & Greed Index** | ❌ | 保留 alternative.me |
| **经济日历**（Finnhub） | ❌ | 保留 Finnhub |
| **CoinGecko / CoinCap 热力图** | ❌ | 保留 |
| **Adanos 情绪**（Reddit/X/Polymarket） | ❌ | 保留 |
| **Tick / Level-2 / 订单簿** | ❌ | 全部保留 |
| **期权 / 期货链** | ❌ | 保留 |
| **按关键字搜股票** | ❌（仅全清单分页） | 保留 yfinance/CCXT 搜索 |

---

## 五、迁移方案

> **核心结论**：这是一次 **部分替换 + 增量增强** 的迁移，不是 wholesale 替换。Crypto / Forex / Futures / MOEX / 新闻 / 情绪 / 经济日历 这些**必须保留现有数据源**。

### 5.1 整体架构

```
QuantDinger 调用 ──┬──► StockDataAdapter (新增) ──► stock_data REST API ──► 上游 10 fetcher
                   │                                                          (Tushare/Baostock/Akshare/Yfinance/Zhitu/Tencent/EastMoney/THS/Cninfo/Myquant)
                   └──► 现有数据源链（CCXT / TwelveData / Tiingo / yfinance / Finnhub / ISS / SearchService / Adanos / CoinGecko）保持不动
```

新增一个 `BaseDataSource` 子类 `StockDataSource`，**仅作为 CNStock / HKStock / USStock 的回退或优先路径**，由工厂方法路由。

### 5.2 推荐分阶段实施

#### 阶段 1：适配器骨架（隔离改动）

**新建** `backend_api_python/app/data_sources/stock_data.py`：
- 类 `StockDataSource(BaseDataSource)`，实现 `name='stock_data'`
- 内部 `httpx.AsyncClient`（已有 `httpx` 依赖）→ `STOCK_DATA_BASE_URL` 默认 `http://localhost:8888`
- 构造时支持 `timeout`、`priority`、`enabled` 开关
- 方法 `_call(path, params)` 统一处理重试 / 熔断 / 限流（参考 `app/data_sources/rate_limiter.py` 与 `circuit_breaker.py`）

**修改** `app/data_sources/__init__.py`：导出 `StockDataSource`。
**修改** `env.example`：增加 `STOCK_DATA_BASE_URL`、`STOCK_DATA_ENABLED`、`STOCK_DATA_TIMEOUT`。

#### 阶段 2：CNStock 路径

**修改** `app/data_sources/cn_stock.py`：
- 在 4 级回退前插入 **第 0 级**：如果 `STOCK_DATA_ENABLED=true` 且服务可达，**优先**走 `StockDataSource`
- `get_kline` 映射：
  - `timeframe='1D'/'1W'` → `/stocks/{code}/history?period=daily|weekly&days=limit&adjust=qfq`
  - `timeframe='1m'/'5m'/'15m'/'30m'/'1H'` → 当日 `/stocks/{code}/intraday?period=N`（**注意：仅当日，需在 caller 端处理"超过当日即回退 yfinance/AkShare"**）
  - `timeframe='4H'` → 聚合两小时或回退 AkShare
  - `before_time` 历史分钟线 → **必须回退**现有 yfinance/AkShare 链路
- `get_ticker` 映射 stock_data `StockQuote`（带 PE/PB/换手率）→ CCXT-style `{last, change, changePercent, high, low, open, previousClose}`
- 符号归一：`600519.SS / SH600519 / 600519` → stock_data `600519`（复用 `tencent.normalize_cn_code`）

#### 阶段 3：HKStock 路径

**修改** `app/data_sources/hk_stock.py`：
- 同上插入 StockDataSource 优先
- 但 `timeframe='1m'/'5m'/...` → stock_data **无 HK 分钟线**，自动回退 AkShare/yfinance
- 符号归一：`0700.HK / HK00700 / 00700` → `HK00700`

#### 阶段 4：USStock 路径

**修改** `app/data_sources/us_stock.py`：
- 插入 StockDataSource 优先
- 实时报价：`/stocks/AAPL/quote` 替代 Finnhub（**多 PE/PB 等增强字段**）
- 日 K：`/stocks/AAPL/history?period=daily`
- 分钟 K：限制**当日**，历史回退 yfinance
- **股票清单**：`/stocks?market=us` 仍只 S&P 500，watchlist 仍走 Finnhub/yfinance
- 符号归一：`AAPL` 直传

#### 阶段 5：Symbol 搜索路由

**修改** `app/routes/market.py::symbols_search` 与 `agent_v1/markets.py::symbols`：
- csi/hk/us 三市场搜索时，**优先**调 `/stocks?market=...&limit=` 然后 in-memory 关键字过滤
- 兜底仍走 yfinance/CCXT

#### 阶段 6：增量数据接入

将 stock_data 独有的 A 股特色数据接到 QuantDinger（**纯新增**，无需替换）：

**新建** `app/data_providers/cn_market_data.py`：
- `fetch_dragon_tiger(code=None, trade_date, look_back=30)` → `/dragon-tiger/...`
- `fetch_margin(code)` → `/margin`
- `fetch_block_trade(code)` → `/block-trade`
- `fetch_holder_num(code)` → `/holder-num`
- `fetch_dividend(code)` → `/dividend`
- `fetch_fund_flow(code, daily=False)` → `/fund-flow` `/fund-flow/daily`
- `fetch_north_flow()` → `/north-flow/realtime`
- `fetch_hot_topics(date)` → `/hot/topics`
- `fetch_reports(code)` `/fetch_report_pdf(code, id)` → `/reports` `/reports/{id}/pdf`
- `fetch_announcements(code)` → `/announcements`
- `fetch_zt_pool(date)` → `/pools?type=zt`

**新建** `app/routes/cn_extras.py` 暴露上述 endpoint。
**新建** `app/routes/agent_v1/cn_extras.py`（scope R）让 AI Agent 也能用。
**更新** `docs/api/openapi.yaml` 与 `docs/agent/agent-openapi.json`。

#### 阶段 7：基本面补丁（可选）

stock_data 缺三大报表。可选方案：

- **方案 A**：在 stock_data 侧新增 Myquant fetcher 暴露 `stock_fundamentals` / `stock_balance_sheet` / `stock_income_statement`（Myquant SDK 已有，免费接口）
- **方案 B**：QuantDinger 侧保留现有 `cn_hk_fundamentals.py`（Twelve Data + AkShare）
- **建议**：先 A 后 B（统一数据源）

#### 阶段 8：缓存与可观测

**修改** `app/services/kline.py::KlineService`：
- 新增 `STOCK_DATA_*` 的 TTL 配置（参考 stock_data 默认：quote=60s, daily=300s, weekly=3600s, monthly=7200s）
**修改** `app/data_sources/rate_limiter.py`：
- 新增 `_stock_data_limiter`（建议 min=0.3s, jitter=0.1–0.5，因为 stock_data 自己做了 failover/熔断）
**新建** `app/data_sources/stock_data_metrics.py`：
- 上游命中率、平均延迟、failover 次数（与 stock_data `/health?details` 对接）

#### 阶段 9：测试

**新建** `backend_api_python/tests/test_stock_data_adapter.py`：
- Mock httpx 响应，验证 schema 映射正确（`StockQuote` ↔ ticker，`KLineData` ↔ kline 列表）
- 验证符号归一（4 种输入 → stock_data 形式）
- 验证 timeframe 回退（1m/5m/15m/30m/1H 当日、4H 回退、before_time 回退）

**新建** `backend_api_python/tests/test_stock_data_integration.py`（pytest marker `integration`）：
- 启本地 stock_data 实例，端到端跑 CNStock/HKStock/USStock

#### 阶段 10：可配置开关 & 渐进发布

- `STOCK_DATA_ENABLED=false` 默认关闭，避免初次部署就全部切流量
- `STOCK_DATA_MARKETS=csi,hk` 白名单（默认全开）
- 通过 dashboard / 配置中心动态开关，方便 A/B 对比

### 5.3 改动文件清单（汇总）

| 操作 | 路径 | 用途 |
|---|---|---|
| 新建 | `app/data_sources/stock_data.py` | 主适配器，实现 `BaseDataSource` |
| 新建 | `app/data_providers/cn_market_data.py` | 龙虎榜/融资融券/资金流/北向/研报/公告 等 |
| 新建 | `app/routes/cn_extras.py` | 暴露 CN 特色数据 |
| 新建 | `app/routes/agent_v1/cn_extras.py` | Agent Gateway 入口 |
| 新建 | `tests/test_stock_data_adapter.py` | 单测 |
| 新建 | `tests/test_stock_data_integration.py` | 集成测（marker integration） |
| 新建 | `docs/data_sources/STOCK_DATA_INTEGRATION.md` | 集成文档（即本文） |
| 修改 | `app/data_sources/factory.py` | 注册新源 / 选择策略 |
| 修改 | `app/data_sources/cn_stock.py` | 插入优先回退 |
| 修改 | `app/data_sources/hk_stock.py` | 插入优先回退 |
| 修改 | `app/data_sources/us_stock.py` | 插入优先回退 |
| 修改 | `app/data_sources/__init__.py` | 导出 |
| 修改 | `app/routes/market.py` | 符号搜索用 `/stocks` |
| 修改 | `app/routes/agent_v1/markets.py` | 同上 |
| 修改 | `app/services/kline.py` | TTL 配置 |
| 修改 | `app/data_sources/rate_limiter.py` | 新增限流器 |
| 修改 | `env.example` | 新增 env 变量 |
| 修改 | `docs/api/openapi.yaml` | 同步 OpenAPI |
| 修改 | `docs/agent/agent-openapi.json` | 同步 agent OpenAPI |
| 修改 | `requirements.txt` | （如未引入 httpx，需加） |

### 5.4 关键风险与决策点

| 风险 | 影响 | 缓解 |
|---|---|---|
| stock_data 单服务单点 | 故障时 CN/HK/US 数据全断 | 加 health-check 短路 + 现有 4 级回退链兜底 |
| 分钟线仅当日 | 现有 backtest / IndicatorStrategy 用 `before_time` 取长历史分钟线时失败 | 在 `get_kline` 内检测 `before_time` 时**自动回退** yfinance/AkShare |
| stock_data 不持久化 K 线 | 每次穿透，依赖上游速率 | TTLCache 命中可缓解，但 backtest 仍重；建议在 QuantDinger 侧加分钟级 SQLite 缓存层（一次性工作） |
| 多 worker 部署下 TTLCache 不共享 | gunicorn 多 worker 时缓存命中率低 | 复用现有 Redis（`CacheManager`）做 stock_data 路径二级缓存 |
| US 股票清单仅 SPX | watchlist 创建 SPX 外的股票时报错 | watchlist 校验时调 `/stocks?market=us` 看是否在 500 之内，否则拒绝 |
| stock_data 无 auth | 暴露到公网有风险 | 与 stock_data 部署在同一内网，nginx 加 basic auth 或 IP 白名单 |

### 5.5 验证策略

1. **影子模式**：先让 `STOCK_DATA_ENABLED=true` 但 `STOCK_DATA_SHADOW_MODE=true`，把 stock_data 与现有数据源的响应**同时取**并 diff，不返回给客户端，先观察 1–2 周。
2. **指标对比**：平均延迟、错误率、字段完整度、与 TwelveData/AkShare 数值偏差（PE/PB 可能有口径差异）。
3. **A/B**：先对 CNStock A 股启用，观察 `KlineService` 命中率、backtest 准确率。
4. **回滚**：env 开关一键回到旧数据源。

---

## 六、一句话总结

| 维度 | 结论 |
|---|---|
| **能完全覆盖** | A 股日/周/月 K + 实时报价（含增强字段） + A 股特色数据（龙虎榜/融资融券/资金流/北向/研报/公告/涨跌停/板块/股东数/分红） + 14 个技术指标 + A 股交易日历 |
| **能部分覆盖** | HK/US 日 K + 实时；分钟线仅当日；HK/US 股票清单受限（HK 小、US 仅 SPX）；基本面缺三大表 |
| **完全无法覆盖** | **Crypto / Forex / 传统 & 加密 Futures / MOEX / 新闻 / 情绪指标 / 经济日历 / Tick-Level-2 / 期权** |
| **迁移性质** | **增量 + 部分替换**，非 wholesale；新增 `StockDataSource` 适配器 + 新增 `cn_market_data` provider；Crypto/Forex/Futures/MOEX 链路**全部保留** |

切换后净收益：

- ✅ A 股数据**质量提升**（多 7 个报价字段 + 14 个服务端技术指标 + 9 类原本没有的特色数据）
- ✅ 减少对 TwelveData / AkShare 海外稳定性 / TwelveData 800 次/天免费额度的依赖（核心 DWM 走 Tushare/Baostock，更稳）
- ⚠️ 损失：分钟线历史回溯能力、多 worker 缓存、符号搜索（仍兜底现有链路）

---

## 七、落地建议

如果批准本次迁移，建议从 **阶段 1（适配器骨架）+ 阶段 6（CN 特色数据接入）** 同时起步，阶段 2/3/4 等阶段 1 稳定后逐个市场切换。

落地前应先确认以下信息：

- stock_data 服务的部署地址与版本（决定 `STOCK_DATA_BASE_URL` 默认值）
- TUSHARE_TOKEN / ZHITU_TOKEN / MYQUANT_TOKEN 是否准备（影响 stock_data 端的高优先级 fetcher 是否启用）
- 是否接受"分钟线历史回溯"退化（决定是否在 QuantDinger 侧加分钟 K 线 SQLite 缓存）
- 是否一并补基本面三大表（决定阶段 7 走方案 A 还是 B）