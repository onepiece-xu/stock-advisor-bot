# stock-advisor-bot CR 审查报告

审查日期：2026-05-24  
审查对象：`/root/projects/stock-advisor-bot`  
审查范围：程序定位、行情链路、交易建议逻辑、风控、通知、配置安全、测试状态  

## 1. 结论

`stock-advisor-bot` 当前更适合作为“股票监控 + 辅助决策 + 飞书提醒”工具，不建议直接升级为自动交易程序。

没有发现券商实盘下单接口，因此当前最大风险不是“误自动下单”，而是以下几类：

- 行情源不稳定时，部分入口会直接失败。
- 评分/风控逻辑存在被静默跳过的情况。
- 当前测试套件不通过，说明关键行为没有稳定回归保障。
- 配置文件中存在明文 webhook、app secret、LLM API key，存在凭证泄露风险。
- 交易建议中有较强措辞，容易被用户误当成确定性交易指令。

建议先按“辅助投顾工具”治理：先修安全和稳定性，再做信号优化；在测试变绿、凭证外置、行情 fallback 统一之前，不建议接入自动下单。

## 2. 风险分级

| 等级 | 问题 | 影响 |
|---|---|---|
| P0 | 配置文件存在明文密钥 | 凭证泄露后可能导致飞书机器人/LLM API 被滥用 |
| P0 | 测试套件当前失败 | 核心交易建议逻辑缺少可信回归保障 |
| P1 | `monitor-once` 行情异常直接崩溃 | 单次执行不可靠，盘中可能拿不到建议 |
| P1 | 多周期过滤可能被静默跳过 | 周线/日线风控失效，可能放大错误买入信号 |
| P1 | 盘中公告通知调用错误配置字段 | 有重要公告时推送链路可能报错 |
| P2 | 飞书持仓文档解析对格式绑定过死 | 文档格式微调会导致持仓同步失败 |
| P2 | 回测口径偏分钟短周期 | 对真实交易胜率、滑点、成交约束覆盖不足 |

## 3. 关键发现

### P0：明文凭证在 `config.yaml`

位置：

- `config.yaml:67-71`
- `config.yaml:76-84`

问题：

- 飞书 webhook URL、飞书 app secret、DeepSeek API key 写在配置文件中。
- 如果仓库、备份、日志或截图外泄，凭证会直接暴露。

建议：

- 立即轮换当前已暴露凭证。
- `config.yaml` 只保留空值或占位符。
- 统一从环境变量读取，例如 `STOCK_ADVISOR_FEISHU_WEBHOOK_URL`、`STOCK_ADVISOR_APP_SECRET`、`DEEPSEEK_API_KEY`。
- 增加 `config.example.yaml`，真实 `config.yaml` 加入 `.gitignore`。

### P0：测试套件当前失败

验证命令：

```bash
python3 -m unittest discover -s tests -v
```

结果：

- `23` 个测试中，`2 failures, 10 errors`。

主要失败类型：

- `_safe_pnl_pct` 假设持仓对象一定有 `current_price`，但测试中的持仓对象没有该字段。
- `portfolio_doc_sync` 无法解析当前 `data/portfolio_doc_latest.md` 的总资产字段。
- 买入信号相关断言与当前评分逻辑不一致。

建议：

- 先让测试套件变绿，再继续调整策略分数。
- 对交易建议逻辑增加固定行情样本测试，避免每次策略改动引入行为漂移。
- 测试中禁止真实访问外部行情 API，所有网络调用都应 mock。

### P1：`monitor-once` 行情失败会直接退出

位置：

- `stock_advisor/cli.py:292`
- `stock_advisor/providers.py:263`

实测命令：

```bash
python3 -m stock_advisor.cli monitor-once --config config.yaml --mobile
```

结果：

- 东方财富连接被远端关闭后，命令直接抛出 `requests.exceptions.ConnectionError`。

问题：

- daemon 路径里有新浪/腾讯 fallback。
- `monitor-once` 路径没有复用 daemon 的 fallback 策略。

建议：

- 抽出统一的 `load_stock_history_with_fallback`。
- `monitor-once`、daemon、历史建议统一使用同一套行情降级链路。
- 行情失败时输出“数据不可用/使用缓存”状态，而不是直接崩溃。

### P1：多周期过滤可能被静默跳过

位置：

- `stock_advisor/analysis.py:652-667`
- `stock_advisor/analysis.py:684-686`

问题：

- `rationale.append(...)` 在 `rationale` 初始化之前执行。
- 异常被 `except Exception` 吞掉。
- 结果是 `multi_timeframe_filter` 的说明和部分效果可能静默失效。

影响：

- 周线空头禁买、强趋势暂缓卖出等风控可能没有按预期影响最终建议。

建议：

- 将 `score/rationale/risk_flags` 初始化移动到多周期过滤之前。
- 不要吞掉该类逻辑错误，至少打 warning 日志。
- 增加测试覆盖：周线空头时强制 `buy -> avoid`。

### P1：盘中公告通知调用错误配置字段

位置：

- `stock_advisor/runtime.py:340`

问题：

```python
deliver_feishu_message(self.config.feishu, "盘中公告", message)
```

`AppConfig` 中没有 `feishu` 字段，正确路径应是：

```python
self.config.monitor.notification.feishu
```

影响：

- 一旦盘中出现重要公告，推送链路可能报错。

建议：

- 修正配置路径。
- 增加 `_maybe_send_breaking_news` 的单元测试。

### P2：飞书持仓文档解析对格式绑定过死

位置：

- `stock_advisor/portfolio_doc_sync.py:34`
- `stock_advisor/portfolio_doc_sync.py:112`

问题：

- 当前解析依赖固定正则：`- 总资产[：:]...`
- 当前 `data/portfolio_doc_latest.md` 已无法被测试解析。

建议：

- 支持多种格式：Markdown 列表、表格、Lark HTML 节点。
- 解析失败时输出更明确的错误上下文。
- 增加真实文档样例回归测试。

### P2：交易建议文案容易被理解为确定性指令

位置：

- `stock_advisor/analysis.py:1202-1252`
- `stock_advisor/runtime.py:149-155`

问题：

- 文案中存在“买入”“卖出”“清仓”“禁止买入”等强动作词。
- 虽然多处写了“不构成投资建议”，但手机推送场景中用户可能只看动作卡。

建议：

- 飞书推送统一加上“需人工确认”。
- 动作分为 `signal_action` 和 `execution_suggestion`。
- 对 `avoid` 避免默认映射为“清仓”，减少误解。

## 4. 当前可用能力

已具备的有效能力：

- A 股分钟行情监控。
- 东方财富/新浪/腾讯多源行情，daemon 路径有 fallback。
- SQLite 落库。
- 飞书通知与 outbox。
- 持仓快照和复盘报告。
- 交易计划触发单。
- 基础风控：仓位上限、现金线、单票集中度、止损/止盈提醒。

可以继续保留并强化的方向：

- 作为“上班族盘中提醒工具”是合理定位。
- 作为“人工确认前的信号过滤器”是合理定位。
- 不建议作为无人值守自动交易系统。

## 5. 建议修复顺序

### 第一优先级：安全与可运行性

1. 轮换所有已出现在 `config.yaml` 的真实密钥。
2. 密钥改为环境变量读取。
3. 修复 `runtime.py` 盘中公告配置字段。
4. 修复 `analysis.py` 多周期过滤初始化顺序。
5. 让 `python3 -m unittest discover -s tests -v` 变绿。

### 第二优先级：稳定性

1. 统一 `monitor-once` 和 daemon 的行情 fallback。
2. 所有外部行情调用加超时、降级和缓存提示。
3. 回测和单测禁止真实访问外部网络。
4. 持仓文档解析兼容多格式。

### 第三优先级：策略质量

1. 将买入/卖出建议拆成“信号”和“执行建议”。
2. 回测加入滑点、手续费、涨跌停不可成交、T+1、仓位约束。
3. 对每个标的维护独立参数，不要全市场共用一套阈值。
4. 增加事后归因：哪些信号真正贡献收益，哪些只是噪声。

## 6. 验证记录

已执行：

```bash
python3 -m stock_advisor.cli validate-config --config config.yaml
```

结果：通过。

```bash
python3 -m compileall -q stock_advisor scripts tests
```

结果：通过。

```bash
python3 -m stock_advisor.cli monitor-once --config config.yaml --mobile
```

结果：失败，行情请求被远端关闭后直接抛异常。

```bash
python3 -m stock_advisor.cli close-review --config config.yaml
```

结果：成功生成收盘复盘。

```bash
python3 -m unittest discover -s tests -v
```

结果：失败，`2 failures, 10 errors`。

## 7. 最终建议

短期：只作为人工辅助提醒工具使用，不接自动下单。  
中期：完成安全治理、测试修复、行情 fallback 统一后，再用于稳定盘中提醒。  
长期：若要做自动交易，必须新增独立的交易执行层、风控熔断层、模拟盘验证、人工确认机制和完整审计日志。

