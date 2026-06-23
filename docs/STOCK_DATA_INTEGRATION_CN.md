# stock_data 数据源接入方案（A 股视角）

> 本文档对应一次**部分替换 + 增量增强**的数据源迁移评估。
> **范围**：只评估 A 股（沪深京 CSI）的接入方式，**不讨论**美股 / 港股 / 加密 / 期货 / 外汇 / MOEX。
> **目标**：把 QuantDinger 的 A 股数据获取路径部分切换到外部 `stock_data` 服务，同时保留所有现有链路作为兜底。

---

## 一、背景

- `stock_data` 是一个基于 FastAPI 的本地数据聚合服务，封装了 Tushare / Baostock / AkShare / Yfinance / Zhitu / Tencent / EastMoney / THS / Cninfo / Myquant / **Baidu** 共 **11 个**上游数据源（v4 README），对外暴露 30+ 个 GET endpoint（含 `/healthz`、`/explorer/`、`/control/*`）。
- QuantDinger 现有 `app/data_sources/`（10 个 `BaseDataSource` 子类）+ `app/data_providers/`（10 个复合视图）+ 多条 REST 路由，覆盖 Crypto / Forex / Futures / USStock / CNStock / HKStock / MOEX 七个市场。
- 评估目的是回答两件事：**A 股哪些能切？切的话怎么动？**

---

## 二、QuantDinger 现有 A 股数据能力

### 2.1 抽象接口（`app/data_sources/base.py`）

`BaseDataSource` 规定两个核心方法：

| 方法 | 签名 | 返回 |
|---|---|---|
| `get_kline` | `(symbol, timeframe, limit, before_time=None, after_time=None)` | `[{"time": unix_s, "open", "high", "low", "close", "volume"}]` |
| `get_ticker` | `(symbol)` | CCXT 风格 ticker：`{last, change, changePercent, high, low, open, previousClose}` |

支持的 timeframe：`1m / 3m / 5m / 15m / 30m / 1H / 4H / 1D / 1W`。

### 2.2 A 股相关数据源（`app/data_sources/`）

| 文件 | 主数据源 | 回退链 | 备注 |
|---|---|---|---|
| `cn_stock.py` | **Twelve Data** → **Tencent** → yfinance → **AkShare (Eastmoney)** | 4 级 | 分钟线只走 yfinance/AkShare；Tencent 仅支持日/周/月 |
| `hk_stock.py` | Twelve Data → Tencent → yfinance → AkShare | 4 级 | 符号归一为 `HK00700`（与 A 股共用 AkShare 兜底） |
| `us_stock.py` | **yfinance** 主，**Finnhub** 仅做 daily 兜底 | 2 级 | minute 限 7 天窗口；ticker Finnhub → yfinance fast_info → info → 1m bar |
| `cn_hk_fundamentals.py` | **基本面**：PE/PB/PS/PEG/market_cap/ROE/EPS/income_statement/balance_sheet/cash_flow/earnings/profile | — | A+H 全部走 Twelve Data + AkShare |

辅助模块（不直接走 `BaseDataSource`）：

- `app/data_sources/tencent.py` — 腾讯财经 HTTP 工具
- `app/data_sources/asia_stock_kline.py` — TwelveData/yfinance/AkShare 分钟/周线 helper

### 2.3 A 股相关的复合视图层（`app/data_providers/`）

| Provider | 输出 | 数据源 |
|---|---|---|
| `indices.py` | 10 个全球指数（^GSPC/^DJI/^IXIC/^GDAXI/^FTSE/^FCHI/^N225/^KS11/^AXJO/^BSESN） — **含 A 股相关指数** | yfinance batch |
| `news.py` | 中英文财经新闻 | 搜索服务（Google/Bing/Tavily/SerpAPI） |
| `sentiment.py` | 恐惧贪婪、VIX、DXY、收益率曲线、VXN、GVZ、PCR | alternative.me + yfinance + AkShare |
| `heatmap.py` | 7 类资产热力图 | ThreadPool 并行 |
| `opportunities.py` | 异动扫描（±5%/±15% 阈值） | 各数据源 |
| `economic_calendar.py` | 财经日历（lookback 3 天 / 展望 14 天） | **Finnhub** |

### 2.4 A 股相关路由

| 路由 | 入口 |
|---|---|
| `/api/kline/kline` | `kline.py` → `KlineService.get_kline` |
| `/api/market/symbols/search` `symbols/hot` `watchlist/get` `watchlist/prices` `price` `types` `config` | `market.py` |
| `/api/global-market/overview` `heatmap` `news` `calendar` `sentiment` `opportunities` `refresh` | `global_market.py` |
| `/api/agent/v1/markets` `/symbols` `/klines` `/price` | `agent_v1/markets.py` |

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

## 三、stock_data 现有能力（按 v4 README + 实测 routes.py）

### 3.1 endpoint 总览（实测路由表）

| Path | Method | Tags | 说明 |
|---|---|---|---|
| `/api/v1/stocks/{code}/quote` | GET | stocks | **A 增强版**报价：价/涨跌幅/OHLC/量额 + **PE_TTM/PB/总市值/流通市值/换手率/振幅/量比**（Tencent 字段 39/43–49/52） |
| `/api/v1/stocks/{code}/history` | GET | stocks | 日/周/月 K 线 + **`?indicators=` 服务端直出 14 个技术指标**（`ma,macd,boll,kdj,rsi,wr,bias,cci,atr,obv,roc,dmi,sar,kc`），auto-lookback |
| `/api/v1/stocks/{code}/intraday` | GET | stocks | 分钟线（1/5/15/30/60），**仅当日** |
| `/api/v1/stocks/{code}/info` | GET | stocks | **A 股公司画像**（行业/上市日/注册资本/高管/经营范围） — Zhitu → Myquant 兜底 |
| `/api/v1/indices` | GET | indices | 沪深300/上证50/科创50 等 14 个 CSI 指数 + 2 个 HK 指数 + 6 个 US 指数（SPX/DJI/IXIC/NDX/VIX/SPY） |
| `/api/v1/indices/{code}/quote` | GET | indices | 指数实时报价 |
| `/api/v1/indices/{code}/history` | GET | indices | 指数日/周/月 K 线 + `?indicators=` |
| `/api/v1/indices/{code}/intraday` | GET | indices | 指数分钟线 |
| `/api/v1/stocks?market=csi\|hk\|us` | GET | stocks | 股票清单（**US 仅有标普 500 成分股**；HK 仅 Akshare 兜底） |
| `/api/v1/calendar` | GET | stocks | A 股交易日历（SQLite 持久化，启动时 warm-up） |
| `/api/v1/boards` `boards/{code}/stocks` | GET | boards | 概念/行业板块 + 成分股（`type=concept\|industry`, `include_quote=true`） |
| `/api/v1/zt-pools?type=zt\|dt\|zbgc&date=&refresh=` | GET | zt-pools | 涨停/跌停/炸板池（**当前交易日不入 SQLite**，直透上游） |
| `/api/v1/stocks/{code}/dragon-tiger` `/dragon-tiger/daily` | GET | stocks | 龙虎榜（个股 + 全市场） |
| `/api/v1/stocks/{code}/margin` | GET | stocks | 融资融券 8 字段 |
| `/api/v1/stocks/{code}/block-trade` | GET | stocks | 大宗交易 |
| `/api/v1/stocks/{code}/holder-num` | GET | stocks | 股东户数（季度） |
| `/api/v1/stocks/{code}/dividend` | GET | stocks | 分红送转 |
| `/api/v1/stocks/{code}/fund-flow` `/fund-flow/daily` | GET | stocks | 资金流（分钟级 / 120 日） |
| `/api/v1/hot/topics?date=` | GET | stocks | 同花顺热门话题 |
| `/api/v1/north-flow/realtime` | GET | stocks | 沪深股通实时资金流 |
| `/api/v1/stocks/{code}/reports` | GET | stocks | 研报列表 |
| `/api/v1/stocks/{code}/reports/{report_id}/pdf` | GET | stocks | 研报 PDF 下载 |
| `/api/v1/stocks/{code}/announcements` | GET | stocks | 巨潮公告 |
| `/api/v1/indicators/catalog` | GET | indicators | 14 个技术指标字典（**给 AI Agent 提示用**） |
| `/api/v1/news/search?q=&from=&to=&limit=` | GET | news | 关键词/股票代码/主题新闻搜索（**EastMoney 主，Baidu 备份**） |
| `/api/v1/news/flash?limit=` | GET | news | 7×24 快讯（**EastMoney 主，THS 备份**） |
| `/api/v1/news/content?url=` | GET | news | URL → 正文提取（拒绝内网 URL） |
| `/healthz` | GET | — | 健康检查（k8s/lb 约定，**挂根目录**；`?details=true` 暴露各 fetcher 熔断状态） |
| `/explorer/` | — | — | **交互式 API Explorer** UI（`GET /control/api-manifest` 驱动） |
| `/control/config` `/control/server/status` `/control/api-manifest` `POST /control/fetcher-test` | GET/POST | — | 管理 API（**127.0.0.1 only**，需 `SERVER_HOST=0.0.0.0` 显式开启远程访问） |

> ⚠️ OpenAPI 关闭（`docs_url=None / openapi_url=None`），**无认证**（CORS 锁 localhost 默认；`SERVER_HOST=127.0.0.1`）。

### 3.2 上游适配器（**11 个** fetcher — v4 已新增 Baidu）

| Provider | Markets | 能力 | 需要 token |
|---|---|---|---|
| **Tushare Pro** | csi | 日/周/月 + 实时（需 tick 权限） | `TUSHARE_TOKEN` |
| **Baostock** | csi | 日/周/月 + **1/5/15/30/60 分钟（仅个股）** + 交易日历 + 指数 | 无 |
| **Akshare** | csi, hk | 全量 DWM + 分钟 + 实时 + 股票池 + 板块 + 公告 | 无 |
| **Yfinance** | csi, hk, us | DWM + 分钟（≤60 天窗口） + 实时（Stooq US 兜底） | 无 |
| **Zhitu** | csi | 实时 + 涨跌停池；5/15/30/60 分钟（**无 1 分钟**）+ 公司画像 | `ZHITU_TOKEN` |
| **Tencent** | csi, hk | 实时（带 PE/PB/换手率等增强字段） | 无 |
| **EastMoney 数据中心** | csi | 龙虎榜、融资融券、大宗交易、股东户数、分红、资金流、研报、快讯、新闻 | 无 |
| **THS 同花顺** | csi | 热门话题、沪深股通、快讯（备份） | 无 |
| **Cninfo 巨潮** | csi | 公告 | 无 |
| **Myquant 掘金** | csi | DWM + 5/15/30/60 分钟（仅当日） + 实时（仅价） + 公司画像（备份） | `MYQUANT_TOKEN` |
| **Baidu 千帆** | csi | 新闻搜索（EastMoney 主，**Baidu 备份**） | `BAIDU_API_KEY` |

Failover：`DataFetcherManager._with_failover` 按 capability + market + 优先级挑；优先级可通过 `*_PRIORITY` 环境变量覆盖。

熔断：`CB_FAILURE_THRESHOLD=3, CB_COOLDOWN_SECONDS=300, CB_HALF_OPEN_MAX_CALLS=1`。

### 3.3 存储 & 缓存

- **SQLite**（`stock_data/stock_cache.db`，可由 `STOCK_CACHE_DB_PATH` 覆盖），5 张持久化表：`stock_list / trade_calendar / stock_board / stock_board_stock / pool_daily`
- 启动行为：`STOCK_DB_INIT=true` → DROP + 全量重建；`false` → 幂等 `CREATE IF NOT EXISTS`
- 启动 warm-up：trade_calendar 表为空时一次性从上游拉取（失败不致命）
- **K 线不持久化**，每次请求穿透上游；TTLCache 内存缓存（60s–7200s，按 endpoint 类型分级）
- **无 Redis、无 cron、无 worker**
- 响应中 `source` 字段：fetcher 名（tushare / akshare / eastmoney 等）或 `"persistence"`（来自 SQLite）

### 3.4 频率覆盖矩阵（A 股）

| 频率 | Baostock | Tushare | Akshare | Yfinance | Zhitu | Myquant |
|---|:-:|:-:|:-:|:-:|:-:|:-:|
| 日/周/月 | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ (d only 指数) |
| 1 分钟 | ✅ 仅个股 | ❌ | ✅ A 股 + 指数 | ✅ | ❌ | ❌ |
| 5/15/30/60 分钟 | ✅ 仅个股 | ❌ | ✅ | ✅ | ✅ | ✅ |

> ⚠️ **关键限制**：分钟线全部**只返回当天一根交易日**，没有跨日历史分钟归档。

### 3.5 A 股指标体系（`/indicators/catalog`）

| Key | Type | Inputs | Output columns | 默认 Lookback |
|-----|------|--------|----------------|--------------|
| `ma` | SMA/EMA/WMA | closes | `ma5, ma10, ma20, ma30, ma60` | 60 |
| `macd` | 12/26/9 EMA diff | closes | `macd_dif, macd_dea, macd_hist` | 87 |
| `boll` | Bollinger Bands | closes | `boll_mid, boll_upper, boll_lower, boll_bandwidth` | 20 |
| `kdj` | Stochastic | ohlcv | `kdj_k, kdj_d, kdj_j` | 18 |
| `rsi` | Wilder's RSI | closes | `rsi_6, rsi_12, rsi_24` | 48 |
| `wr` | Williams %R | ohlcv | `wr_6, wr_10` | 10 |
| `bias` | 乖离率 | closes | `bias_6, bias_12, bias_24` | 24 |
| `cci` | Commodity Channel | ohlcv | `cci` | 28 |
| `atr` | Average True Range | ohlcv | `atr, tr` | 28 |
| `obv` | On-Balance Volume | ohlcv | `obv, obv_ma` | 1 |
| `roc` | Rate of Change | closes | `roc, roc_signal` | 12 |
| `dmi` | Directional Movement | ohlcv | `dmi_pdi, dmi_mdi, dmi_adx, dmi_adxr` | 56 |
| `sar` | Parabolic SAR | ohlcv | `sar, sar_trend, sar_ep, sar_af` | 5 |
| `kc` | Keltner Channel | ohlcv | `kc_mid, kc_upper, kc_lower, kc_width` | 60 |

**自动 lookback 扩展**：服务端自动多取 `max(days, indicator_lookback)` 根 K 线计算指标，再截取回 `days` 根；调用方无需预计算。

### 3.6 关键环境变量

| 变量 | 说明 | 默认 |
|---|---|---|
| `TUSHARE_TOKEN` | Tushare Pro token | — |
| `ZHITU_TOKEN` | Zhitu token | — |
| `MYQUANT_TOKEN` | Myquant token | — |
| `BAIDU_API_KEY` | Baidu 千帆 API key | — |
| `BAIDU_NEWS_DOMAINS` | Baidu 新闻搜索白名单（逗号分隔） | canonical news subdomains |
| `*_PRIORITY` | 各 fetcher 优先级覆写 | 见表 3.2 |
| `ENABLE_API_CACHE` | TTLCache 总开关 | `true` |
| `CACHE_TTL_QUOTE` | 实时报价 TTL | 60s |
| `CACHE_TTL_HISTORY_DAILY/WEEKLY/MONTHLY` | K 线 TTL | 300/3600/7200s |
| `CACHE_TTL_INDEX_INTRADAY` `CACHE_TTL_STOCK_INTRADAY` | 分钟线 TTL | 30s |
| `CACHE_TTL_STOCK_INFO` | 公司画像 TTL | 3600s |
| `STOCK_CACHE_DB_PATH` | SQLite 路径 | `<repo>/stock_data/stock_cache.db` |
| `STOCK_DB_INIT` | `true` DROP 重建（⚠️ 丢缓存） | `false` |
| `SERVER_PORT` / `SERVER_HOST` | 服务端口 / 主机 | `8888` / **`127.0.0.1`** |
| `CB_FAILURE_THRESHOLD` / `CB_COOLDOWN_SECONDS` / `CB_HALF_OPEN_MAX_CALLS` | 熔断参数 | 3 / 300 / 1 |

---

## 四、A 股对比矩阵：QuantDinger 需要 vs stock_data 能给

### ✅ 完全支持（可直接切换，零妥协）

| QuantDinger 需求 | stock_data 对应能力 | 切换路径 |
|---|---|---|
| CNStock 日/周/月 K 线 + 复权 | `/stocks/{code}/history?period=daily&adjust=qfq\|hfq` | 1:1 替换 |
| CNStock 实时报价 | `/stocks/{code}/quote`（**还多 PE/PB/换手率/振幅/量比**） | 字段映射即可 |
| CNStock 股票清单 | `/stocks?market=csi` | 1:1 替换 |
| CNStock 交易日历 | `/calendar` | 1:1 替换 |
| CNStock **公司画像**（行业/注册资本/高管/经营范围） | **`/stocks/{code}/info`** | 纯增量，QuantDinger 现无此模块 |
| CNStock 14 个技术指标 | `/history?indicators=ma,macd,boll,kdj,rsi,wr,bias,cci,atr,obv,roc,dmi,sar,kc` | 服务端直出，含自动 lookback |
| CNStock 当日分钟 K（1/5/15/30/60m） | `/stocks/{code}/intraday` | 仅当日 |
| CSI 指数日 K + 实时 + 板块 | `/indices` `/indices/{code}/quote` `/indices/{code}/history` `/boards` `/boards/{code}/stocks` | 1:1 |
| A 股特色数据（龙虎榜/融资融券/大宗交易/股东户数/分红/资金流/热门话题/北向资金/研报 + PDF/公告/涨跌停池） | 全部 endpoint | **纯增量**，QuantDinger 现无此模块 |
| **财经新闻搜索 + 快讯 + 正文提取** | **`/news/search` `/news/flash` `/news/content`** | **纯增量**，EastMoney 主 + Baidu/THS 备份；可替换 QuantDinger 现有 Google/Bing 搜索（中文场景更优） |
| 14 个技术指标目录 | `/indicators/catalog` | 给 AI Agent 提示用 |

### ⚠️ 部分支持（有取舍）

| QuantDinger 需求 | 限制 | 缓解方案 |
|---|---|---|
| CNStock 分钟 K 线（**多日历史**） | stock_data 单次只返回**当日**；Akshare 也是单日 | (a) 接受只做当日策略 (b) 在 QuantDinger 侧加分钟级 K 线 SQLite 缓存，按需聚合 (c) 仍走 yfinance/AkShare 分钟路径 |
| 基本面（income/balance/cashflow/ratios） | stock_data **没有三大表 fetcher**；Myquant SDK 支持但没接；东财只有 PE/分红/股东数 | 保留 Twelve Data + AkShare fallback；可新增 Myquant fetcher 补齐 |

### ❌ 不支持（必须保留 QuantDinger 现有数据源）

> 本节列出的是 **stock_data 完全不覆盖** 的能力，与本文档 "只关心 A 股" 的范围一致 —— 这些能力要么在 QuantDinger 内其他模块（非 A 股）使用，要么 stock_data 没有对应 fetcher。

| QuantDinger 需求 | stock_data 状态 | 后果 |
|---|---|---|
| **Crypto 全部**（CCXT 任意交易所，spot/swap 永续） | ❌ 完全无 | **必须保留** CCXT（但与本文档 A 股范围无关） |
| **Forex**（EURUSD/GBPUSD/USDJPY…） | ❌ 完全无 | **必须保留** Twelve Data / Tiingo / yfinance |
| **传统 & 加密 Futures**（GC/SI/CL/BZ/HG/NG/PL/PA/ALI/ZW/ZC/SB + Binance 永续） | ❌ 完全无 | **必须保留** Twelve Data / Tiingo / yfinance / CCXT binanceusdm |
| **MOEX** | ❌ 完全无 | **必须保留** ISS API |
| **全球指数**（^GSPC/^DJI/^IXIC/^GDAXI/^FTSE/^FCHI/^N225/^KS11/^AXJO/^BSESN） | stock_data 仅有 6 个 US 指数（SPX/DJI/IXIC/NDX/VIX/SPY），**无德/英/法/日/韩/澳/印** | 仍走 yfinance |
| **Fear & Greed Index** | ❌ | 保留 alternative.me |
| **经济日历**（Finnhub） | ❌ | 保留 Finnhub |
| **VIX / DXY / 收益率曲线 / PCR** 等情绪指标 | stock_data `/indices/{code}/quote` 能给 VIX 价，但 sentiment 整套没有 | 保留 yfinance + alternative.me |
| **Adanos 情绪**（Reddit/X/Polymarket） | ❌ | 保留 |
| **CoinGecko / CoinCap 热力图** | ❌ | 保留 |
| **Tick / Level-2 / 订单簿** | ❌ | 全部保留 |
| **期权 / 期货链** | ❌ | 保留 |
| **按关键字搜股票** | ❌（仅全清单分页） | 保留 yfinance/CCXT 搜索（**注**：stock_data 新增了 `/news/search`，但只搜新闻不搜股票） |

---

## 五、三种接入方案对比

> 用户问题：**A 股数据由 stock_data 支持，其他数据由源代码支持**（方案 A）vs **完全放弃其他数据，只计算 A 股数据**（方案 B）vs **A 股数据由 stock_data 支持，不支持的部分回退到 QuantDinger**（方案 C）。三个方案复杂度分别多高？推荐哪个？

### 5.1 方案 A — 增量替换 + A 股新增（"推荐"）

**做法**：A 股链路在 `cn_stock.py` 4 级回退链**前面**插入 StockDataSource 作为**第 0 级优先路径**；非 A 股链路（Crypto / Forex / Futures / MOEX / 全球指数 / 情绪 / 日历）**完全不动**；A 股特色数据（龙虎榜/资金流/北向/研报/公告/涨跌停池 + 公司画像 + 新闻搜索）走**全新** `cn_extras` 路由暴露。

#### 改动面（粗略估时）

| 范畴 | 内容 | 估时 |
|---|---|---|
| 新增 `app/data_sources/stock_data.py` | `BaseDataSource` 子类，httpx 客户端，重试/限流/熔断/超时 | 1–2 天 |
| `app/data_sources/cn_stock.py` | 在 4 级回退前插 StockDataSource，timeframe 映射，symbol 归一复用 `tencent.normalize_cn_code` | 0.5–1 天 |
| 新增 `app/data_providers/cn_market_data.py` | 龙虎榜/融资融券/资金流/北向/研报 + PDF/公告/涨跌停池/公司画像/新闻 search+flash+content | 1.5–2 天 |
| 新增 `app/routes/cn_extras.py` + `app/routes/agent_v1/cn_extras.py` | 13+ 路由 + Agent Gateway R scope | 1 天 |
| `env.example` 新增 `STOCK_DATA_*` | `BASE_URL` `ENABLED` `TIMEOUT` `SHADOW_MODE` `MARKETS=csi` | 0.5 天 |
| 单测 + 集成测 | `test_stock_data_adapter.py`（mock httpx）+ `test_stock_data_integration.py`（marker integration） | 1.5 天 |
| OpenAPI / agent-openapi / docs | `scripts/export_openapi.py` + 手编 agent spec + 本文件 | 0.5 天 |
| **总计** | | **6.5–8.5 人天** |

#### 优点

- **风险面最小**：A 股链路所有现有 4 级回退完整保留，stock_data 故障 = 回到旧链路；非 A 股用户**完全无感知**
- **影子模式可落地**：env `STOCK_DATA_SHADOW_MODE=true` 同时取 stock_data 与旧链路 diff，不返回客户端，先观察 1–2 周
- **纯增量**：13 个新路由全部为 ADD，不动现有路径，符合 `docs/MODULE_BOUNDARIES.md → 路由层不允许改既有路径`
- **可灰度**：`STOCK_DATA_MARKETS=csi` 白名单，默认只切 A 股

#### 缺点

- **两条代码路径同时维护**：stock_data adapter 和原 4 级回退链要并行跑一段时间
- **测试覆盖翻倍**：mock 一次 stock_data + 一次原链路 ≈ 2× 当前 cn_stock 测试体量
- **文档需维护两套**：用户问"我用的是哪条链路"需在 README 解释

#### 复杂度评级：**中**（约 1.5 个 sprint）

---

### 5.2 方案 B — 砍掉所有非 A 股数据源

**做法**：直接删除 `crypto.py / forex.py / futures.py / moex.py` 等非 A 股 `BaseDataSource`；删除 `data_providers/crypto.py forex.py commodities.py indices.py`（**注**：A 股相关指数保留）的非 A 股部分；相关路由（`/api/global-market/*` 中非 A 股分支、quick-trade 的非 A 股交易对）报错或返回 404；IBKR / MT5 / Alpaca 适配器从 Dockerfile 移除。

#### 改动面（粗略估时）

| 范畴 | 内容 | 估时 |
|---|---|---|
| 删除 `app/data_sources/crypto.py forex.py futures.py moex.py` | 4 个文件 + 所有引用 | 0.5 天 |
| 删除 `data_providers/crypto.py forex.py commodities.py` | 3 个文件 | 0.5 天 |
| 删除 `app/services/live_trading/binance.py okx.py ...` 7 个文件 + `app/services/pending_orders/live_order_phases.py` 中加密分支 | 7+ 文件 | 1 天 |
| 删除 `ibkr_trading / mt5_trading / alpaca_trading` 整个子包 + Dockerfile 内引用 | 3 个子包 | 1 天 |
| 删除 `routes/quick_trade.py` 中所有非 A 股交易对支持 | 路由 + 服务 | 1 天 |
| 清理 `app/markets/registry.py`：保留 CNStock，删 6 个市场 | registry + UI 可见性 | 1 天 |
| 清理 `app/data_providers/sentiment.py heatmap.py economic_calendar.py adanos_sentiment.py` | 移除 VIX/DXY/F&G/Adanos/CoinGecko/CoinCap | 1 天 |
| 更新所有 OpenAPI / agent spec / 安装脚本 / docker-compose / install.sh/.ps1 | 多文件 | 1 天 |
| **迁移破坏性变更**：现有用户策略里凡是引用非 A 股 symbol / market 的全部 422 | 数据迁移 + 通知 | 2–3 天 |
| 端到端回归测试 | 全市场回归（含被砍的）确认无残留引用 | 1 天 |
| **总计** | | **9–11 人天 + 1 个 release note 通知窗口** |

#### 优点

- **代码最简**：单一市场单一栈，未来维护成本最低
- **依赖最少**：CCXT / IBKR / MT5 / Alpaca / Tiingo / Finnhub / Adanos / CoinGecko / CoinCap 全可卸，docker 镜像缩小 ~30%
- **测试体量减少**：单一链路，回归覆盖收窄

#### 缺点

- **BREAKING 变更**：所有非 A 股用户（加密货币 / 外汇 / 美港股 / 商品期货 / MOEX 用户）**全部失去功能**，必须通知 + 引导迁移
- **战略定位转向**：从"多市场量化平台"变成"A 股专用量化平台"，与 `README.md` 当前定位冲突
- **下游用户策略作废**：现网用户策略里 `market=crypto` / `forex` / `futures` / `moex` 的全部报 422，需要提供导出脚本
- **违反 `CLAUDE.md → Never commit secrets / weakening safety`** 红线之外，但触碰"重大战略变更需明确要求"的红线
- **失去 IBKR / 量化券商的差异化能力**：QuantDinger 当前通过 `services/broker_*` 提供券商适配是差异化卖点之一

#### 复杂度评级：**高**（约 2–2.5 个 sprint + 1 个 release note 周期）

---

### 5.3 方案 C — A 股走 stock_data + QuantDinger 自动 fallback

**做法**：方案 A 的基础上，把 fallback 从"4 级链兜底"升级为"StockDataSource → QuantDinger 原 4 级链"，并加自动重试与**字段级**回退（stock_data 字段缺失时即时回退到原链路取该字段）。

#### 改动面（粗略估时）

| 范畴 | 内容 | 估时 |
|---|---|---|
| 方案 A 全部内容 | 见 5.1 | 6.5–8.5 天 |
| 字段级 fallback 框架 | `BaseDataSource.get_kline/get_ticker` 拦截异常 → 在 `cn_stock.py` 决策路由 → 单字段请求 → 合并 | 2 天 |
| 分钟线历史回退 | 检测 `before_time != None` → 跳过 stock_data 直接走 yfinance/AkShare（QuantDinger 现有路径） | 0.5 天 |
| `cn_hk_fundamentals.py` fallback 编排 | PE/PB/PS/ROE → stock_data 优先；income/balance/cashflow → 现有 Twelve Data | 0.5 天 |
| 三级缓存层 | 内存 TTLCache → Redis（CacheManager 复用）→ 落 SQLite（历史 K 线，**新增** `cn_kline_archive` 表） | 2–3 天 |
| A/B 与回滚面板 | `STOCK_DATA_TRAFFIC_PCT=0→100` 渐进切换，按用户/策略灰度 | 1–1.5 天 |
| 增强测试矩阵 | stock_data 主路径 + fallback 路径 + 字段缺失 + 熔断 + 超时 + 部分失败合并 | 2 天 |
| **总计** | | **14.5–17.5 人天** |

#### 优点

- **可用性最高**：任何 stock_data 不可用场景自动透明回退到原链路；任何 stock_data 字段缺失也能从原链路补齐
- **数据质量最高**：多源 cross-validation（Tencent vs Tushare vs Baostock 数值偏差检测）
- **分钟线历史**：通过 SQLite 归档层弥补 stock_data 单日限制
- **灰度最细**：可按用户/策略/股票三个维度切流量

#### 缺点

- **实现复杂度最高**：字段级 fallback + 三级缓存 + A/B 灰度都是非平凡工程
- **调试困难**：一个失败请求可能跨 stock_data + 4 级原链路，链路追踪复杂
- **过度设计风险**：如果 stock_data 自身 11 fetcher failover 已足够好，方案 C 的大部分回退代码实际不会触发
- **测试矩阵爆炸**：每个字段 × 每个源 × 各种失败组合 ≈ 指数级

#### 复杂度评级：**高**（约 3–3.5 个 sprint）

---

### 5.4 三方案对比表

| 维度 | 方案 A 增量替换 | 方案 B 砍非 A 股 | 方案 C 自动 fallback |
|---|---|---|---|
| **改动人天** | 6.5–8.5 | 9–11 + 通知 | 14.5–17.5 |
| **是否破坏性变更** | 否（纯增量） | **是**（所有非 A 股用户失去功能） | 否 |
| **A 股数据质量** | 高（多源 failover + 11 fetcher） | 高 | **最高**（双栈 cross-validation） |
| **回滚成本** | 一键 env 关闭 | 需重装 + 数据迁移 | 一键 env 关闭 |
| **灰度能力** | 完整（白名单 + shadow mode） | N/A（全切或全不切） | 最细（流量百分比 + 用户级） |
| **维护成本** | 中（双栈并行一段时间） | 低（单栈） | 高（三级缓存 + 多源合并） |
| **战略契合度** | 高（与"多市场平台"定位一致） | 低（变 A 股专用） | 高 |
| **测试体量** | 中（×2 当前 cn_stock） | 低（单栈） | 高（多源矩阵） |
| **推荐度** | ⭐⭐⭐ **推荐** | ⭐ 不推荐（破坏性） | ⭐⭐ 备选（若 A 阶段后量化数据偏差大） |

### 5.5 推荐：方案 A（增量替换）

**核心理由**：

1. **CLAUDE.md 红线保护** — "Agent tokens are paper-only by default; live execution requires `paper_only=false`" 等安全护栏在方案 B 下全部失效（非 A 股 live-trading 链路被砍），触发重大战略变更需明确 ask。
2. **零破坏性** — 方案 A 不动任何现有用户路径；非 A 股用户（加密 / 外汇 / 期货 / 美港股）**完全无感**。
3. **影子模式可观测** — A 提供 `STOCK_DATA_SHADOW_MODE=true` 灰度路径，可在不返给客户端的前提下对比 stock_data vs 旧链路的延迟 / 错误率 / 数值偏差，1–2 周后再决策是否进一步升级到方案 C。
4. **复杂度与收益比最佳** — 6.5–8.5 人天换来 A 股数据质量提升 + 9 类全新特色数据 + 服务端 14 个技术指标，ROI 最高。
5. **可演进** — 若 A 阶段跑 1–2 个季度后发现 stock_data 在某些字段（如 PE 静态值 / 三大表）口径不一致，再升级到方案 C 做字段级 fallback 即可；不必一次性把复杂度拉满。

**方案 C 何时升级**：

- stock_data 出现已知数据偏差（如 PE 静态值与 TwelveData 差异 > 5%）且影响策略信号
- 分钟线历史成为核心需求（当前 backtest / IndicatorStrategy 已有 `before_time` 用法）
- 多 worker Gunicorn 部署下需要分布式缓存（当前 TTLCache 进程内不共享）

**方案 B 何时考虑**（仅当用户明确要求）：

- 产品战略转向"A 股专用"
- 用户量中非 A 股占比 < 5% 且已主动通知
- 维护成本成为首要约束（团队 < 3 人）

---

## 六、推荐分阶段实施（基于方案 A）

### 6.1 整体架构

```
QuantDinger 调用 ──┬──► StockDataSource (新增，A 股第 0 级优先) ──► stock_data REST API ──► 上游 11 fetcher
                   │                                                                    (Tushare/Baostock/Akshare/Yfinance/Zhitu/Tencent/EastMoney/THS/Cninfo/Myquant/Baidu)
                   └──► 现有数据源链（CNStock 4 级 / HK / US / Crypto / Forex / Futures / MOEX）保持不动
```

新增 `BaseDataSource` 子类 `StockDataSource`，**仅作为 CNStock 的回退或优先路径**，由工厂方法路由。非 A 股链路完全不动。

### 6.2 阶段 1：适配器骨架（隔离改动）

**新建** `backend_api_python/app/data_sources/stock_data.py`：
- 类 `StockDataSource(BaseDataSource)`，实现 `name='stock_data'`
- 内部 `httpx.AsyncClient`（已有 `httpx` 依赖）→ `STOCK_DATA_BASE_URL` 默认 `http://localhost:8888`
- 构造时支持 `timeout`、`priority`、`enabled`、`shadow_mode` 开关
- 方法 `_call(path, params)` 统一处理重试 / 熔断 / 限流（参考 `app/data_sources/rate_limiter.py` 与 `circuit_breaker.py`）
- 启动时 `GET /healthz` 健康探测 + `/control/server/status` 状态读取（仅 shadow 模式下生效）

**修改** `app/data_sources/__init__.py`：导出 `StockDataSource`。
**修改** `env.example`：增加 `STOCK_DATA_BASE_URL`、`STOCK_DATA_ENABLED`、`STOCK_DATA_TIMEOUT`、`STOCK_DATA_SHADOW_MODE`、`STOCK_DATA_MARKETS=csi`。

### 6.3 阶段 2：CNStock 路径

**修改** `app/data_sources/cn_stock.py`：
- 在 4 级回退前插入 **第 0 级**：如果 `STOCK_DATA_ENABLED=true` 且服务可达，**优先**走 `StockDataSource`
- `get_kline` 映射：
  - `timeframe='1D'/'1W'/'1M'` → `/stocks/{code}/history?period=daily|weekly|monthly&days=limit&adjust=qfq`
  - `timeframe='1m'/'5m'/'15m'/'30m'/'1H'` → 当日 `/stocks/{code}/intraday?period=N`（**注意：仅当日，需在 caller 端处理"超过当日即回退 yfinance/AkShare"**）
  - `timeframe='4H'` → 聚合两小时或回退 AkShare
  - `before_time` 历史分钟线 → **必须回退**现有 yfinance/AkShare 链路
- `get_ticker` 映射 stock_data `StockQuote`（带 PE/PB/换手率/振幅/量比）→ CCXT-style `{last, change, changePercent, high, low, open, previousClose}`
- 符号归一：`600519.SS / SH600519 / 600519` → stock_data `600519`（复用 `tencent.normalize_cn_code`）

### 6.4 阶段 3：公司画像（纯增量）

**新建** `app/data_providers/cn_company_info.py`：
- `fetch_company_profile(code)` → `/stocks/{code}/info`
- 在 `app/routes/market.py` 加 `/api/market/company-profile/{code}` 路由
- 在 `app/routes/agent_v1/markets.py` 加 `GET /companies/{code}/profile`（scope R）

### 6.5 阶段 4：A 股特色数据 + 新闻（纯增量）

**新建** `app/data_providers/cn_market_data.py`：

```python
# A 股特色数据（10 个 endpoint）
fetch_dragon_tiger(code=None, trade_date, look_back=30)   → /dragon-tiger/...  /dragon-tiger/daily
fetch_margin(code)                                        → /margin
fetch_block_trade(code)                                   → /block-trade
fetch_holder_num(code)                                    → /holder-num
fetch_dividend(code)                                      → /dividend
fetch_fund_flow(code, daily=False)                        → /fund-flow  /fund-flow/daily
fetch_north_flow()                                        → /north-flow/realtime
fetch_hot_topics(date)                                    → /hot/topics
fetch_reports(code) / fetch_report_pdf(code, id)          → /reports  /reports/{id}/pdf
fetch_announcements(code)                                 → /announcements
fetch_zt_pool(date, type='zt')                            → /zt-pools?type=zt&date=

# 新闻（3 个 endpoint — EastMoney 主，Baidu/THS 备份）
search_news(q, from_date, to_date, limit)                 → /news/search?q=&from=&to=&limit=
fetch_flash_news(limit)                                   → /news/flash?limit=
fetch_news_content(url)                                   → /news/content?url=
```

**新建** `app/routes/cn_extras.py` + `app/routes/agent_v1/cn_extras.py`（scope R）让 AI Agent 也能用。
**更新** `docs/api/openapi.yaml` 与 `docs/agent/agent-openapi.json`。

### 6.6 阶段 5：缓存与可观测

**修改** `app/services/kline.py::KlineService`：
- 新增 `STOCK_DATA_*` 的 TTL 配置（参考 stock_data 默认：quote=60s, daily=300s, weekly=3600s, monthly=7200s, intraday=30s, info=3600s）
**修改** `app/data_sources/rate_limiter.py`：
- 新增 `_stock_data_limiter`（建议 min=0.3s, jitter=0.1–0.5，因为 stock_data 自己做了 failover/熔断）
**新建** `app/data_sources/stock_data_metrics.py`：
- 上游命中率、平均延迟、failover 次数（与 stock_data `/healthz?details=true` 对接）
- 暴露 Prometheus 指标 `/metrics/stock_data`

### 6.7 阶段 6：基本面补丁（可选）

stock_data 缺三大报表。可选方案：

- **方案 1**：在 stock_data 侧新增 Myquant fetcher 暴露 `stock_fundamentals` / `stock_balance_sheet` / `stock_income_statement`（Myquant SDK 已有，免费接口）
- **方案 2**：QuantDinger 侧保留现有 `cn_hk_fundamentals.py`（Twelve Data + AkShare）
- **建议**：先 2 后 1（统一数据源）

### 6.8 阶段 7：测试

**新建** `backend_api_python/tests/test_stock_data_adapter.py`：
- Mock httpx 响应，验证 schema 映射正确（`StockQuote` ↔ ticker，`KLineData` ↔ kline 列表）
- 验证符号归一（4 种输入 → stock_data 形式）
- 验证 timeframe 回退（1m/5m/15m/30m/1H 当日、4H 回退、before_time 回退）
- 验证 shadow mode 双取 + diff 不返回客户端

**新建** `backend_api_python/tests/test_stock_data_integration.py`（pytest marker `integration`）：
- 启本地 stock_data 实例，端到端跑 CNStock DWM + 当日分钟 + 特色数据

**新建** `backend_api_python/tests/test_cn_extras.py`：
- 覆盖 13+ 新路由的输入校验 + 字段映射 + 错误降级

### 6.9 阶段 8：可配置开关 & 渐进发布

- `STOCK_DATA_ENABLED=false` 默认关闭，避免初次部署就全部切流量
- `STOCK_DATA_MARKETS=csi` 白名单（默认只 A 股）
- `STOCK_DATA_SHADOW_MODE=false` 默认关闭，运维手动开启观察
- 通过 dashboard / 配置中心动态开关，方便 A/B 对比
- `STOCK_DATA_TRAFFIC_PCT`（方案 C 时启用）— 渐进流量切换

---

## 七、改动文件清单（汇总）

| 操作 | 路径 | 用途 |
|---|---|---|
| 新建 | `app/data_sources/stock_data.py` | 主适配器，实现 `BaseDataSource` |
| 新建 | `app/data_providers/cn_company_info.py` | 公司画像（A 股增量） |
| 新建 | `app/data_providers/cn_market_data.py` | 龙虎榜/融资融券/资金流/北向/研报/公告/涨跌停/新闻 |
| 新建 | `app/data_sources/stock_data_metrics.py` | Prometheus 指标 |
| 新建 | `app/routes/cn_extras.py` | 暴露 CN 特色数据 + 公司画像 + 新闻 |
| 新建 | `app/routes/agent_v1/cn_extras.py` | Agent Gateway 入口 |
| 新建 | `tests/test_stock_data_adapter.py` | 单测 |
| 新建 | `tests/test_stock_data_integration.py` | 集成测（marker integration） |
| 新建 | `tests/test_cn_extras.py` | 新路由单测 |
| 新建 | `docs/data_sources/STOCK_DATA_INTEGRATION.md` | 集成文档（即本文） |
| 修改 | `app/data_sources/factory.py` | 注册新源 / 选择策略 |
| 修改 | `app/data_sources/cn_stock.py` | 插入第 0 级优先回退 |
| 修改 | `app/data_sources/__init__.py` | 导出 |
| 修改 | `app/routes/market.py` | 加 `/market/company-profile/{code}` |
| 修改 | `app/routes/agent_v1/markets.py` | 加 `/companies/{code}/profile` |
| 修改 | `app/services/kline.py` | TTL 配置 |
| 修改 | `app/data_sources/rate_limiter.py` | 新增限流器 |
| 修改 | `env.example` | 新增 env 变量 |
| 修改 | `docs/api/openapi.yaml` | 同步 OpenAPI |
| 修改 | `docs/agent/agent-openapi.json` | 同步 agent OpenAPI |
| 修改 | `requirements.txt` | （如未引入 httpx，需加） |

---

## 八、关键风险与决策点

| 风险 | 影响 | 缓解 |
|---|---|---|
| stock_data 单服务单点 | 故障时 A 股数据全断 | 加 health-check 短路 + 现有 4 级回退链兜底（方案 A 已覆盖） |
| 分钟线仅当日 | 现有 backtest / IndicatorStrategy 用 `before_time` 取长历史分钟线时失败 | 在 `get_kline` 内检测 `before_time` 时**自动回退** yfinance/AkShare |
| stock_data 不持久化 K 线 | 每次穿透，依赖上游速率 | TTLCache 命中可缓解，但 backtest 仍重；建议在 QuantDinger 侧加分钟级 SQLite 缓存层（一次性工作） |
| 多 worker 部署下 TTLCache 不共享 | gunicorn 多 worker 时缓存命中率低 | 复用现有 Redis（`CacheManager`）做 stock_data 路径二级缓存 |
| stock_data **无 auth** | 暴露到公网有风险 | 与 stock_data 部署在同一内网，nginx 加 basic auth 或 IP 白名单；`SERVER_HOST=127.0.0.1` 默认锁 localhost |
| 数值口径差异（PE/PB/总市值） | 与 TwelveData / AkShare 对比有偏差 | shadow mode 跑 1–2 周 diff；超过阈值（如 5%）时上报数据团队 |
| **US/HK 股票清单受限**（HK 小、US 仅 SPX） | 不影响本文档（A 股范围），但若用户用 `cn_stock.py` 接口传非 A 股 symbol 会失败 | symbol 归一时先校验 market（`SH/SZ/BJ` 前缀），非 A 股直接走原 4 级链 |
| stock_data **`/explorer/` 与 `/control/*`** 暴露 | 默认锁 127.0.0.1，远程需显式 `SERVER_HOST=0.0.0.0` | 文档提示运维风险，禁止在公网开启 |

---

## 九、验证策略

1. **影子模式**：先让 `STOCK_DATA_ENABLED=true` 但 `STOCK_DATA_SHADOW_MODE=true`，把 stock_data 与现有数据源的响应**同时取**并 diff，不返回给客户端，先观察 1–2 周。
2. **指标对比**：平均延迟、错误率、字段完整度、与 TwelveData/AkShare 数值偏差（PE/PB 可能有口径差异）。
3. **A/B**：先对 A 股启用，观察 `KlineService` 命中率、backtest 准确率。
4. **回滚**：env 开关一键回到旧数据源。

---

## 十、一句话总结

| 维度 | 结论 |
|---|---|
| **能完全覆盖** | A 股日/周/月 K + 实时报价（含 7 个增强字段） + A 股特色数据（龙虎榜/融资融券/资金流/北向/研报/公告/涨跌停/板块/股东数/分红） + 14 个服务端技术指标 + A 股交易日历 + **公司画像** + **中文新闻搜索/快讯/正文** |
| **能部分覆盖** | A 股分钟线（仅当日）；基本面缺三大表 |
| **完全无法覆盖** | Crypto / Forex / 传统 & 加密 Futures / MOEX / 全球指数（除 US 6 个）/ 情绪指标 / 经济日历 / Tick-Level-2 / 期权 — **与 A 股范围无关，本文档不展开** |
| **迁移性质（推荐方案 A）** | **增量 + 部分替换**，非 wholesale；新增 `StockDataSource` 适配器 + 新增 `cn_market_data` / `cn_company_info` provider；现有 4 级回退链**全部保留** |
| **预估工作量** | **6.5–8.5 人天**（方案 A 增量替换） |

切换后净收益（仅 A 股）：

- ✅ A 股数据**质量提升**（多 7 个报价字段 + 14 个服务端技术指标 + 9 类原本没有的特色数据 + 公司画像 + 中文新闻搜索）
- ✅ 减少对 TwelveData / AkShare 海外稳定性 / TwelveData 800 次/天免费额度的依赖（核心 DWM 走 Tushare/Baostock/Akshare 更稳）
- ✅ 中文新闻搜索场景**优于** Google/Bing 搜索（EastMoney 主源 + Baidu/THS 备份）
- ⚠️ 损失：分钟线历史回溯能力、多 worker 缓存（仍可 Redis 二级缓存缓解）

---

## 十一、落地建议

如果批准本次迁移，**推荐采用方案 A**，从以下同时起步：

- **阶段 1（适配器骨架）**：隔离改动，所有后续阶段的基础
- **阶段 4（特色数据 + 新闻）**：纯新增，零风险，立刻给业务带来 13+ 新数据维度

阶段 2（CNStock 主路径切换）等阶段 1 稳定 + shadow mode 验证后再启动。

落地前应先确认以下信息：

- stock_data 服务的部署地址与版本（决定 `STOCK_DATA_BASE_URL` 默认值）
- TUSHARE_TOKEN / ZHITU_TOKEN / MYQUANT_TOKEN / BAIDU_API_KEY 是否准备（影响 stock_data 端的高优先级 fetcher 是否启用）
- 是否接受"分钟线历史回溯"退化（决定是否在 QuantDinger 侧加分钟 K 线 SQLite 缓存）
- 是否一并补基本面三大表（决定阶段 6 走方案 1 还是 2）

---

## 十二、修订记录

| 日期 | 修订 | 来源 |
|---|---|---|
| 2026-06-23 | 对齐 stock_data v4 实测：11 fetcher（Baidu 新增）、`/healthz` 根挂载、`/explorer/` + `/control/*` 管理 API、`/stocks/{code}/info` 公司画像、`/news/{search,flash,content}` 三个新闻 endpoint、`/zt-pools` 路径纠正（而非 `/pools`）、响应 `source` 字段、SQLite 持久化（`STOCK_DB_INIT` + 启动 warm-up）、`SERVER_HOST=127.0.0.1` 默认；范围收紧至 A 股；新增方案 A/B/C 三方对比与推荐（推荐方案 A） | stock_data v4 README + `api/routes.py` + `server.py` + `explorer/routes.py` 实测 |