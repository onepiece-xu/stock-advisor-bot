# stock-advisor-bot CR 审查报告 v2

审查日期：2026-05-24  
审查对象：`/root/projects/stock-advisor-bot`  
审查基线：当前工作区未提交状态  
审查重点：安全、交易风控、通知桥接、测试质量、运行可靠性  

## 1. 当前结论

当前项目仍应定位为“股票监控 + 辅助决策 + 飞书提醒”工具，不建议直接接自动下单。

相比上一轮检查，当前代码已有明显修复进展：

- `python3 -m unittest discover -s tests -v` 当前通过，`23 tests OK`。
- `analysis.py` 中多周期过滤使用 `rationale` 前未初始化的问题已修复。
- `runtime.py` 盘中公告通知调用不存在 `config.feishu` 的问题已修复。
- `portfolio_doc_sync` 对当前 `data/portfolio_doc_latest.md` 的解析测试已恢复通过。

但仍有几个不应忽略的问题：

- 本地真实配置仍存放敏感凭证，虽然 `config.yaml` 已被 `.gitignore` 忽略，但本机明文仍有泄露面。
- 测试通过不代表策略安全，部分测试已经从“校验深套禁补”退化为“允许买入”。
- 测试执行过程中仍真实访问东方财富网络接口，说明测试不完全 hermetic。
- 通知桥接脚本绕过统一配置系统，直接正则读取配置并提取密钥。
- outbox 与 bridge 消费语义存在误标记风险。
- 飞书 bot 在未配置 verification token 时默认放行，适合本地调试，不适合公网暴露。

## 2. 验证记录

执行命令：

```bash
python3 -m stock_advisor.cli validate-config --config config.yaml
```

结果：通过。

执行命令：

```bash
python3 -m compileall -q stock_advisor scripts tests
```

结果：通过。

执行命令：

```bash
python3 -m unittest discover -s tests -v
```

结果：通过，`Ran 23 tests in 8.108s OK`。

注意：测试通过期间仍出现外部网络解析失败 warning：

- `push2.eastmoney.com` DNS 解析失败。
- `push2his.eastmoney.com` DNS 解析失败。

这说明测试中仍有未 mock 的真实行情访问。当前失败被代码吞掉或降级，因此没有导致测试失败，但这会让 CI 结果受网络环境影响。

## 3. 风险总览

| 等级 | 问题 | 状态 | 建议 |
|---|---|---|---|
| P0 | 本地真实凭证明文存放 | 未解决 | 轮换并改环境变量/密钥管理 |
| P0 | 深套禁补测试退化 | 未解决 | 恢复强约束测试 |
| P1 | 测试真实访问外部行情源 | 未解决 | mock 所有网络调用 |
| P1 | bridge 脚本绕过统一配置加载 | 未解决 | 使用 `load_config()` |
| P1 | outbox 消费可能误标记已发送 | 未解决 | 引入 delivered/failed/attempts 状态机 |
| P1 | 飞书 bot 空 token 默认放行 | 未解决 | 生产模式强制 token |
| P2 | LLM/Feishu env fallback 不一致 | 未解决 | 统一配置优先级 |
| P2 | 信号状态抑制规则过粗 | 未解决 | 按交易日/信号强度建模 |
| P2 | signals 表无幂等约束 | 未解决 | 增加唯一键或去重策略 |

## 4. P0：本地真实凭证明文存放

证据：

- `config.yaml` 已被 `.gitignore` 忽略，未被 Git 跟踪。
- 但当前本地文件仍包含真实 webhook、飞书 app secret、DeepSeek API key。
- 脚本中还存在硬编码飞书文档 token：`scripts/rebuild_doc.py:5`、`scripts/sync_doc_now.py:5`、`stock_advisor/cli.py:1073`。

影响：

- 即使不提交 Git，凭证也可能通过备份、日志、终端录屏、文档粘贴、机器权限泄露。
- bridge 脚本会正则读取配置文件中的 app secret 与 webhook，扩大明文凭证使用面。

建议：

1. 立即轮换当前已出现在本机配置中的 webhook、app secret、LLM key。
2. `config.yaml` 中只保留占位符。
3. webhook、app secret、DeepSeek key、飞书文档 token 全部改为环境变量或单独 secret 文件。
4. 启动脚本统一从环境变量注入，不允许业务脚本正则读取密钥。

## 5. P0：深套禁补测试已经失真

证据：

- 测试名：`test_deep_losing_position_does_not_average_down_before_reclaiming_ma60`
- 位置：`tests/test_regressions.py:193`
- 当前断言：`self.assertEqual(decision.action, "buy")`

问题：

- 测试名称表达的是“深套股不要补仓”。
- 当前断言却要求结果为 `buy`。
- 测试构造的 holding 只有 `quantity` 和 `cost_price`，没有 `current_price`。
- `_safe_pnl_pct()` 在缺少 `current_price` 时返回 `None`。
- `_apply_account_risk_guards()` 只有在 `pnl_pct <= -20` 时才禁止买入，因此该测试没有真正覆盖深套禁补逻辑。

相关代码：

- `stock_advisor/analysis.py:570`
- `stock_advisor/analysis.py:572`
- `stock_advisor/analysis.py:1339`

影响：

- 这是炒股程序里最关键的安全规则之一。
- 测试虽然通过，但没有保护“深套不摊平”这条纪律。
- 后续改策略分数时，可能在深套场景继续产生买入建议。

建议：

1. 测试 holding 必须带 `current_price=Decimal("30")`。
2. 该测试应断言 `action != "buy"`，优先断言 `avoid` 或 `reduce`。
3. 增加边界测试：`-19.9%`、`-20.0%`、`-20.1%`。
4. 对缺少 `current_price` 的 holding 应明确降级为“无法计算持仓风险，禁止加仓”，而不是跳过深套 guard。

## 6. P1：测试仍真实访问外部行情 API

证据：

单元测试通过期间出现：

- `push2.eastmoney.com` DNS 解析失败 warning。
- `push2his.eastmoney.com` DNS 解析失败 warning。

相关代码：

- `stock_advisor/analysis.py:1069` 会调用 `sector_strength.fetch_sector_boards()`。
- `stock_advisor/atr_risk.py` 会拉取东方财富 OHLC。

问题：

- 单元测试不应依赖网络。
- 当前测试结果会受网络、DNS、行情源限流影响。
- 失败被吞掉后，测试也无法验证“行情增强失败时是否有正确降级提示”。

建议：

1. 单元测试中 patch `sector_strength.fetch_sector_boards`。
2. 单元测试中 patch ATR/OHLC 拉取函数。
3. 将网络增强项作为可注入依赖，不要在核心评分函数内部直接发请求。
4. 增加 `--offline-test` 或配置开关，CI 默认禁用所有外部网络。

## 7. P1：bridge 脚本绕过统一配置系统

证据：

- `scripts/bridge_validator.py:543`
- `scripts/bridge_validator.py:551`
- `scripts/bridge_validator.py:552`
- `scripts/bridge_validator.py:554`

问题：

`bridge_validator.py` 通过正则从 `config.yaml` 里解析：

- `app_id`
- `app_secret`
- `receive_open_id`
- `webhook_url`
- `delivery_mode`

这绕过了：

- `stock_advisor.config.load_config()`
- 环境变量覆盖逻辑
- 配置校验
- 后续字段重命名和结构变更

影响：

- 主程序和 bridge 可能读取到不一致配置。
- 配置改成环境变量后，bridge 仍可能失败。
- 正则误匹配时会静默使用错误凭证。

建议：

1. bridge 直接调用 `load_config(REPO / "config.yaml")`。
2. delivery 统一复用 `notify.deliver_feishu_message()`。
3. 移除 bridge 内部对 app secret/webhook 的正则提取。
4. 给 bridge 增加配置加载单元测试。

## 8. P1：outbox 消费语义存在误标记风险

证据：

- `stock_advisor/outbox.py:101`
- `stock_advisor/outbox.py:153`
- `scripts/bridge_validator.py:610`
- `scripts/bridge_validator.py:647`

问题：

- `pull_outbox(mark_sent=True)` 是“读取即标记 sent”。
- bridge 中如果所有消息都被校验拦截，`_mark_sent(limit=10)` 会消费消息。
- 当前状态只有 `sent: true/false`，没有 `delivered`、`blocked`、`failed`、`attempts`。

影响：

- 被风控拦截的消息和真正发送成功的消息都可能被标记为 sent。
- 事后难以区分“已送达”“已拦截”“发送失败”“待重试”。
- 这对交易提醒审计不够安全。

建议：

1. outbox 状态改为枚举：`queued`、`delivering`、`delivered`、`blocked`、`failed`。
2. 消费时写入 `delivered_at`、`blocked_reason`、`attempts`。
3. bridge 只在飞书 API 成功后标记 `delivered`。
4. blocked 消息单独落到 `bridge_blocked.jsonl`，不要混同 sent。

## 9. P1：飞书 bot 空 verification token 默认放行

证据：

- `stock_advisor/feishu_bot_server.py:362`
- `stock_advisor/feishu_bot_server.py:364`
- `stock_advisor/feishu_bot_server.py:365`

问题：

```python
if not expected:
    return True
```

当 `verification_token` 为空时，任何请求都会通过 token 校验。

影响：

- 如果服务绑定 `0.0.0.0` 且端口暴露，外部可直接调用机器人命令。
- 机器人命令可查询持仓、行情、复盘等敏感信息。

建议：

1. 本地开发可允许空 token，但生产模式必须禁止。
2. 当 `feishu_bot.enabled=true` 且 `listen_host=0.0.0.0` 时，强制要求 verification token。
3. 对请求增加来源限制或反向代理鉴权。
4. 对命令响应增加最小必要信息原则，避免泄露完整持仓。

## 10. P2：配置环境变量优先级不一致

证据：

- `stock_advisor/config.py:246` webhook 只读 YAML。
- `stock_advisor/config.py:270` app_id/app_secret 支持环境变量覆盖。
- `stock_advisor/config.py:279` DeepSeek 只读 YAML。
- `stock_advisor/llm_analyst.py:31` 优先读取 `config.yaml`，只有读取失败才 fallback 到环境变量。

问题：

- app secret 支持环境变量，webhook 不支持。
- DeepSeek 如果 `config.yaml` 存在但 key 为空，环境变量不会生效。
- 配置策略不统一，部署时容易误判。

建议：

1. 所有敏感字段统一环境变量优先。
2. YAML 中只放非敏感默认值。
3. `validate-config` 输出“当前字段来源：env/yaml/default”，便于排查。

## 11. P2：信号反转抑制规则过粗

证据：

- `stock_advisor/runtime.py:231`
- `stock_advisor/runtime.py:255`
- `stock_advisor/runtime.py:273`

问题：

- 逻辑注释写的是“yesterday→today”，但状态文件只记录最后一次 action，没有交易日期。
- 同一天内的强反转、隔夜消息后的反转、不同置信度反转，都被简单压制。

影响：

- 可能漏掉真正需要处理的风险信号。
- 例如昨天 `buy`，今天出现重大利空触发 `reduce`，可能被 suppress。

建议：

1. 状态中记录 `date`、`score`、`confidence`、`reason_hash`。
2. 只有低置信度、同一交易日、分数差小的反转才 suppress。
3. 重大公告、跌破止损、市场崩盘等信号不参与 suppress。

## 12. P2：signals 表缺少幂等约束

证据：

- `stock_advisor/storage.py:49`
- `stock_advisor/storage.py:63`
- `stock_advisor/storage.py:272`

问题：

- `quotes` 表有 `uq_quotes_symbol_time`。
- `signals` 表没有 `(symbol, signal_time, action/score bucket)` 级别的唯一约束。
- 同一 quote 重复分析会插入多条 signal。

影响：

- 回放统计、简报、信号准确率可能被重复样本污染。
- daemon 重启或重复执行时，信号数量膨胀。

建议：

1. 给 signals 增加幂等键，例如 `(symbol, signal_time, signal_level)`。
2. 或者增加 `analysis_version` 与 `decision_hash`，支持同一行情不同策略版本共存。
3. 回测统计默认按最新策略版本去重。

## 13. 已修复/已改善项

本轮确认以下上轮问题已改善：

- 多周期过滤初始化顺序已修复：`stock_advisor/analysis.py:652`。
- 盘中公告通知配置路径已修复：`stock_advisor/runtime.py:340`。
- `_safe_pnl_pct` 对缺失字段不再抛异常：`stock_advisor/analysis.py:1339`。
- 飞书持仓文档解析测试当前通过。
- 当前单元测试整体通过。

但注意：`_safe_pnl_pct` “缺字段返回 None”也导致深套禁补测试可以绕过风控，见 P0。

## 14. 建议修复顺序

第一优先级：

1. 轮换本地已出现的真实凭证。
2. 修正深套禁补测试，恢复 `action != buy` 的强约束。
3. 给缺少 `current_price` 的持仓增加保守风控：不能判断亏损时不允许补仓。
4. bridge 改为复用主配置和主通知函数。

第二优先级：

1. 单元测试彻底 mock 网络调用。
2. outbox 引入明确状态机和审计字段。
3. 飞书 bot 在生产环境强制 verification token。
4. 统一敏感配置环境变量优先级。

第三优先级：

1. signals 表增加幂等策略。
2. 反转抑制规则增加日期、置信度和重大事件例外。
3. 将行情增强、ATR、板块强度作为可注入依赖，核心评分函数保持纯函数。

## 15. 上线判断

当前适合：

- 本地运行。
- 人工查看飞书提醒。
- 收盘复盘和持仓辅助判断。

当前不适合：

- 直接接自动下单。
- 公网暴露飞书 bot。
- 作为无人工确认的交易执行系统。

达到以下条件后，再考虑进一步自动化：

- 敏感凭证全部外置并轮换。
- 深套禁补、止损、仓位上限、现金线测试全部强约束通过。
- 测试完全离线可重复。
- outbox 具备可审计状态机。
- 飞书 bot 有强鉴权和允许名单。
- 回测包含手续费、滑点、涨跌停不可成交、T+1、仓位约束。

