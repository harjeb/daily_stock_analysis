# Wolf 日K盘后分析

Wolf 日K盘后分析是一个基于蒸馏规则的每日附加报告。它只使用 DSA 已有日 K 加载链路、均线、BOLL、量价和大盘摘要，不读取 NGA 原始回帖，也不依赖 15 分钟 K。

## 配置

```env
WOLF_DAILY_REPORT_ENABLED=true
WOLF_DAILY_STOCK_LIST_ENABLED=true
WOLF_DAILY_WHITELIST_ENABLED=true
WOLF_DAILY_WHITELIST_FILE=data/pools/wolf_whitelist.csv
WOLF_DAILY_MAX_CODES=30
WOLF_DAILY_HISTORY_DAYS=120
WOLF_DAILY_HOT_SECTOR_FILTER_ENABLED=true
WOLF_DAILY_HOT_SECTOR_TOP_N=12
WOLF_DAILY_HOT_SECTOR_MIN_CHANGE_PCT=0
```

### LLM 驱动模式

默认使用硬编码确定性护栏。启用 LLM 模式后，每只股票的技术数据会发送给 LLM，由 LLM 根据狼大规则框架做判断并输出结构化决策：

```env
WOLF_DAILY_USE_LLM=true
WOLF_LLM_MAX_TOKENS=2048
WOLF_LLM_TEMPERATURE=0.3
WOLF_USER_CAN_MONITOR_INTRADAY=false
```

LLM 模式下，`strategies/wolf_postmarket.yaml` 中的规则指令会作为 system prompt 注入，LLM 输出 JSON 格式的决策结果（`wolf_action`、`entry_type`、`position_cap`、`next_day_paths` 等字段）。如果 LLM 调用失败或返回不可解析的结果，会自动降级为 `watch` 安全默认值。

LLM 模式会额外获取以下数据传入 prompt：
- **市场广度**：上涨/下跌家数、涨停/跌停家数、两市成交额（通过 `get_market_stats` API）
- **板块趋势**：命中板块的 60 日涨幅、全市场强势板块前 5（来自热点板块筛选）、当日领涨/领跌板块排名（通过 `get_sector_rankings` API）
- **个股实时行情**：量比、换手率、市盈率、市净率、总市值、流通市值、振幅、60日涨跌幅、52周高低（通过 `get_realtime_quote` API）
- **计划入场区间**：候选支撑位（MA5/10/20/60、BOLL 中下轨、近 20 日低点、前红 K 低点）和阻力位（BOLL 上轨、近 20 日高点等），含距离收盘价百分比（从日 K 数据计算）
- **个股基本面**：所属行业、ROE、净利率、市盈率(动)、市净率、所属板块（通过 `get_base_info` + `get_belong_board` API）
- **用户盯盘能力**：`WOLF_USER_CAN_MONITOR_INTRADAY=true` 时告知 LLM 用户可以盘中盯盘，影响 `cannot_watch_no_intraday` 规则的判断

LLM 模式与确定性护栏模式可以随时切换，不影响报告格式和推送流程。

可选的 GitHub Actions / 容器注入方式：

```env
WOLF_DAILY_WHITELIST_CONTENT=600519,300750,002594
WOLF_DAILY_WHITELIST_CONTENT_B64=
```

GitHub Actions 每日分析 workflow 会读取同名 Repository Variables 或 Secrets。普通开关和数量上限建议放在 Variables；只有不想提交到仓库的白名单内容才需要放到 `WOLF_DAILY_WHITELIST_CONTENT` 或 `WOLF_DAILY_WHITELIST_CONTENT_B64`。

`WOLF_DAILY_MAX_CODES` 不是强势板块命中数量上限。默认会先按近 60 日板块涨幅选强势板块，再把白名单里命中这些板块的股票全部分析；如果 150 只白名单里有 60 只属于强势板块，本轮会分析这 60 只。只有拿不到板块数据、关闭强势板块筛选，或 `STOCK_LIST` 过大且缺少强势板块命中时，才使用 `WOLF_DAILY_MAX_CODES` 做兜底截断。

强势板块默认取近 60 日涨幅前 12 个，且涨幅不低于 0%。可以用 `WOLF_DAILY_HOT_SECTOR_TOP_N=0` 表示不限制板块个数，只按 `WOLF_DAILY_HOT_SECTOR_MIN_CHANGE_PCT` 过滤。

如果报告显示“白名单 0 只”，通常表示 `WOLF_DAILY_WHITELIST_ENABLED=true` 但没有可读取的白名单：`WOLF_DAILY_WHITELIST_FILE` 指向的文件不存在，且没有配置 `WOLF_DAILY_WHITELIST_CONTENT` / `WOLF_DAILY_WHITELIST_CONTENT_B64`。

## 报告范围

- 白名单：输出“观察 / 可试探 / 可入场”候选，适合维护一个更大的观察池。
- `STOCK_LIST`：输出已有自选股的操作分析，强调不加仓、等待确认、低吸试探或仓位上限。

只支持 6 位 A 股代码。港股、美股或其它格式会在 Wolf 报告中跳过，不影响 DSA 原有分析。

## 数据边界

当前版本不使用 15 分钟 K，因此不会判断：

- 3 个 15 分钟是否守住缺口
- 尾盘确认
- 分时底分型
- 盘中承接强弱

这些场景会降级为“等待确认”。报告输出的是次日计划，不是盘中即时买卖指令。

## 使用的日K规则

- 大盘优先，个股不能突破大盘高风险门禁。
- MA5 乖离过大不追高。
- 黑 K 放量跌破 MA5 按短期见顶 / 减仓信号处理。
- 放量跌破前一根红 K 低点，视为上涨段失效风险。
- 跌破 MA20 不给主动入场。
- BOLL 上轨偏离过大先等回轨；未站上中轨时降低右侧确认。

揉搓线只需要日 K 的开高低收即可量化：

```text
range = high - low
body = abs(close - open)
upper_shadow = high - max(open, close)
lower_shadow = min(open, close) - low
upper_shadow_ratio = upper_shadow / range
lower_shadow_ratio = lower_shadow / range
```

长上影可按 `upper_shadow_ratio >= 0.35 且 upper_shadow >= body * 1.2` 判断；长下影同理。`下影接上影` 指前一根日 K 为长下影、当前日 K 为长上影；`上影接下影` 指前一根日 K 为长上影、当前日 K 为长下影。

报告不会保存或展示原始论坛文本，只保留这些蒸馏后的规则和机器可执行判断。
