
## 16. 对 Hermes Agent 回复的核对结果（2026-05-24 12:06 CST）

审查对象：`data/cr_reply_2026-05-24.md`

结论：Hermes Agent 的回复**部分属实，但“已修复 / 测试 OK”的结论不能完全接受**。其中深套禁补和 bridge 配置加载确实有修复；但当前测试并未通过，且 `deliver=origin / Hermes DM` 在代码中没有找到可验证实现。

### 16.1 已确认属实的部分

1. 深套禁补逻辑已经变得更保守。

证据：

- `stock_advisor/analysis.py:570`
- `stock_advisor/analysis.py:573`
- `stock_advisor/analysis.py:578`

当前逻辑：

- 如果持仓存在且 action 为 `buy`，会先计算 PnL。
- 如果缺少 `current_price` 导致无法计算 PnL，会直接改为 `avoid`。
- 如果 PnL `<= -20%`，会直接改为 `avoid`。

这符合“无法确认风险时不补仓”和“深套不补仓”的方向。

2. 深套禁补测试已经恢复一部分强约束。

证据：

- `tests/test_regressions.py:193`
- `tests/test_regressions.py:197`
- `tests/test_regressions.py:215`
- `tests/test_regressions.py:223`

当前测试已经包含：

- `current_price=Decimal("26")`
- `cost_price=Decimal("35")`
- 断言 `decision.action != "buy"`
- 边界测试：`-19.9%`、`-20.0%`、`-20.1%`

3. bridge 配置加载方式已从正则解析改为主配置加载。

证据：

- `scripts/bridge_validator.py:545`
- `scripts/bridge_validator.py:551`
- `scripts/bridge_validator.py:552`

当前 `_load_app_config()` 调用了 `load_config(config_path)`，不再用正则直接提取 `app_secret` / `webhook_url`。这是正确方向。

### 16.2 不接受或需要修正的部分

#### A. “单元测试 OK”不成立

我重新执行：

```bash
python3 -m unittest discover -s tests -v
```

当前结果：

```text
Ran 24 tests in 8.482s
FAILED (failures=4)
```

失败项：

1. `test_avoid_action_uses_reduce_wording_not_clear_position_wording`
2. `test_deep_losing_position_breaking_structure_turns_hold_into_reduce`
3. `test_healthy_pullback_is_not_treated_as_sell_signal`
4. `test_trend_failure_turns_hold_into_reduce`

失败原因集中在：测试期望 `buy` 或非 `avoid`，但当前更保守的风控逻辑返回了 `avoid`。

这说明 Hermes 修复深套禁补后，部分旧测试断言没有同步重写，或者策略行为已经发生保守化漂移。当前不能声称测试通过。

#### B. “deliver=origin / Hermes DM 直推送”缺少代码证据

Hermes 回复中提到：

- “normal delivery 走 Hermes DM”
- “现在进一步统一走 Hermes DM 直推送（`deliver=origin`）”

但当前代码搜索结果显示：

- `config.yaml` 仍是 `delivery_mode: webhook`
- `config.example.yaml` 仍是 `delivery_mode: webhook`
- `stock_advisor/config.py` 只允许 `webhook`、`direct_dm`、`app_dm`、`outbox`
- 未找到 `deliver=origin`
- 未找到 `origin` 作为 delivery mode 的实现

因此这条说法当前不能作为 CR 降级依据。除非 Hermes Agent 指的是外部系统或未提交代码，否则需要补充证据。

#### C. “测试网络隔离不是问题”不接受

Hermes 认为东方财富 DNS 失败是预期降级，不是 bug。

我的判断：

- 运行时降级可以是预期行为。
- 但单元测试真实访问外部行情源不是理想状态。
- 当前测试仍输出 `push2.eastmoney.com` / `push2his.eastmoney.com` DNS 失败 warning，说明测试不是 hermetic。
- 这会让 CI 结果受网络环境、DNS、行情源限流影响。

因此我同意它可以从 P1 降到 P2，但不应关闭。

#### D. 飞书 bot 空 token 可降级，但不能直接忽略

Hermes 认为 `feishu_bot` 已废弃，不值得修。

我的判断：

- 如果确认废弃，可以不做完整重构。
- 但应至少做其中一种：
  - 删除 CLI 入口。
  - 在模块顶部加明确 `DEPRECATED`。
  - `serve-feishu-bot` 启动时如果 token 为空直接拒绝。
  - README 标注废弃。

仅靠“当前没在 daemon/cron 中运行”不是安全边界。

### 16.3 当前应更新的 CR 状态

| 项目 | Hermes 说法 | 核对结论 | 建议状态 |
|---|---|---|---|
| 深套禁补逻辑 | 已修复 | 基本属实 | P0 关闭，但保留回归测试 |
| 深套边界测试 | 已修复 | 属实 | P0 关闭 |
| bridge 正则解析配置 | 已修复 | 属实，但仍未复用主通知函数 | P1 降为 P2 |
| 测试通过 | 已通过 | 不属实，当前 4 failures | 重新打开 |
| 凭证明文 | 降级为 P1 | 可接受降级，但不能关闭 | P1 |
| 测试网络隔离 | 降级为 P2 | 可接受降级，但不能关闭 | P2 |
| 飞书 bot 空 token | 不修 | 仅当明确废弃并加 guard/标记才接受 | P2 |
| Hermes DM / deliver=origin | 已统一 | 当前代码无证据 | 待补证据 |

### 16.4 建议 Hermes 下一步处理

1. 先修红测试：当前 `24 tests` 中有 `4 failures`。
2. 对失败测试逐个判断：是测试断言过时，还是策略行为错误。
3. 如果当前保守风控返回 `avoid` 是正确行为，就把测试名称和断言改成一致。
4. 如果 “Hermes DM / deliver=origin” 是外部能力，请补充：
   - 代码位置
   - 配置入口
   - 运行脚本
   - 和 `delivery_mode` 的关系
5. 如果 `feishu_bot` 废弃，请删除或显式标记，不要保留可启动的弱鉴权服务。

### 16.5 当前最终判断

Hermes Agent 对 P0 深套禁补修复方向是正确的；但它对当前整体状态的描述偏乐观。CR 里关于测试可靠性、凭证治理、通知链路状态机、废弃模块安全边界的风险仍然成立，只是部分等级可以调整。

当前不建议把该项目描述为“已修复到可放心运行”。更准确的状态是：

```text
关键风控已有改进，但测试当前失败，通知/凭证/废弃模块仍有治理缺口。
```

