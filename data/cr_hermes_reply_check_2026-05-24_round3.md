
## 18. 对 Hermes 后续修复的三次核对（2026-05-24 12:25 CST）

审查对象：当前工作区代码状态。

结论：Hermes 本轮已把上次最关键的“测试失败”问题修到通过状态，并补了 README 对 `feishu_bot` 的废弃说明。当前 CR 状态可以从“测试未闭环”更新为“测试通过，但仍有测试网络依赖和通知链路证据不足”。

### 18.1 验证结果

执行：

```bash
python3 -m stock_advisor.cli validate-config --config config.yaml
```

结果：通过。

执行：

```bash
python3 -m compileall -q stock_advisor scripts tests
```

结果：通过。

执行：

```bash
python3 -m unittest discover -s tests -v
```

结果：

```text
Ran 24 tests in 50.802s
OK
```

注意：测试通过期间仍有外部行情访问 warning：

- `stock_advisor.sector_strength`: Eastmoney 连接被远端关闭
- `stock_advisor.atr_risk`: ATR 拉取 OHLC 失败

因此“测试通过”成立，但“测试完全离线可重复”仍不成立。

### 18.2 已确认改善项

#### A. 4 个失败测试已修到通过

上一轮失败的 4 个测试当前均通过。

Hermes 对测试名称做了语义调整，例如：

- `test_trend_failure_still_allows_buy_when_pnl_not_deep_loss`
- `test_deep_losing_with_breaking_structure_still_allows_buy_when_pnl_not_deep_loss`

这说明当前策略语义被明确为：

```text
只要不是深套（<= -20%），部分趋势失败/结构破坏场景仍可能允许 buy。
```

这属于策略取舍，不是测试框架错误。CR 中应记录这个策略假设，因为它偏激进。

#### B. `feishu_bot` README 废弃说明已补充

证据：

- `README.md:59`
- `README.md:364`
- `README.md:366`
- `README.md:367`
- `README.md:451`

README 当前已明确：

- `feishu_bot` 已废弃。
- 推送通道统一走 Hermes Agent DM。
- 历史用法放进 `<details>`。
- 原“`feishu_bot.enabled` 已设为 true”已改为废弃/默认 false。

这解决了上一轮指出的“README 仍误导启用废弃 bot”的主要问题。

#### C. `feishu_bot_server.py` 注释已修正

证据：

- `stock_advisor/feishu_bot_server.py:1`
- `stock_advisor/feishu_bot_server.py:4`

上一轮指出注释里 “unless explicitly forced” 与 CLI 不一致；当前已改为：

```text
will refuse to start when feishu_bot.enabled is false
```

这和实际代码一致。

### 18.3 仍未关闭的问题

#### A. 测试仍真实访问外部网络

虽然测试现在通过，但耗时约 50 秒，并且多次出现行情接口 warning。

这说明核心评分测试仍会触发：

- `sector_strength.fetch_sector_boards`
- ATR/OHLC 拉取

影响：

- 本地和 CI 结果仍受外部行情源状态影响。
- 网络慢时测试耗时明显增加。
- 外部接口恢复/失败可能改变增强项行为，造成隐性不稳定。

建议状态：P2 保留。

建议处理：

1. 在单元测试中 mock `sector_strength.fetch_sector_boards`。
2. 在单元测试中 mock ATR/OHLC 拉取。
3. 将核心评分函数尽量保持纯函数，外部增强数据通过参数注入。

#### B. `Hermes Agent DM / deliver=origin` 仍缺少代码级证据

README 和 `feishu_bot_server.py` 注释都写了“推送统一走 Hermes Agent DM”。

但当前代码仍显示：

- `config.yaml`：`delivery_mode: webhook`
- `config.example.yaml`：`delivery_mode: webhook`
- `stock_advisor/config.py` 只允许 `webhook`、`direct_dm`、`app_dm`、`outbox`
- `scripts/bridge_validator.py` 只处理 `webhook` 和 `app_dm`
- 未找到 `deliver=origin`
- 未找到 `origin` delivery mode

因此，如果 Hermes Agent DM 是外部系统能力，不在本仓库中，需要在 README 或运维文档中明确说明：

- 由哪个外部进程接管。
- 如何从 outbox/webhook 转到 Hermes DM。
- 本仓库配置里为何仍是 `webhook`。
- `deliver=origin` 是哪个工具/协议的参数。

建议状态：待补证据，不关闭。

#### C. feishu bot 入口仍保留，但风险已降级

当前状态：

- 模块已标记 deprecated。
- README 已标记 deprecated。
- `feishu_bot.enabled=false` 时拒绝启动。
- 但 CLI 入口 `serve-feishu-bot` 仍存在。
- 如果用户手动把 `feishu_bot.enabled=true` 且不配置 `verification_token`，原 `_is_valid_verification_token()` 仍会在 token 为空时放行。

风险判断：

- 不再是 P1。
- 可降为 P2，作为废弃模块清理项。

建议：

1. 下一版直接移除 `serve-feishu-bot` CLI。
2. 或者在 `feishu_bot.enabled=true` 时强制 `verification_token` 非空。

### 18.4 当前 CR 状态更新

| 项目 | 当前状态 |
|---|---|
| 深套禁补生产逻辑 | 已改善，可关闭 P0 |
| 深套边界测试 | 已新增，可关闭 P0 |
| bridge 正则解析配置 | 已修复，可降级/关闭主要问题 |
| 单元测试通过 | 当前通过 |
| README feishu_bot 废弃说明 | 已修复 |
| feishu_bot 注释与行为一致性 | 已修复 |
| 测试外部网络依赖 | 未关闭，保留 P2 |
| Hermes DM / deliver=origin 证据 | 未关闭，待补证据 |
| outbox 状态机 | 未关闭，仍建议后续治理 |
| 凭证明文 | 未关闭，建议至少 P1 |

### 18.5 最新准确结论

当前可以更新为：

```text
Hermes 已修复关键深套风控测试、bridge 配置读取和 feishu_bot 废弃文档问题；当前测试套件通过。但测试仍依赖外部行情网络，Hermes DM / deliver=origin 缺少本仓库代码证据，凭证和 outbox 审计仍需后续治理。
```

上线判断：

- 可以继续作为“人工辅助提醒工具”运行。
- 仍不建议接自动下单。
- 若要进入更稳定运维状态，下一步优先做测试网络隔离、通知链路文档化、outbox 状态机和密钥治理。

