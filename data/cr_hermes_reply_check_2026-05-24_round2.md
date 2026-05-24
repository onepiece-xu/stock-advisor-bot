
## 17. 对 Hermes 后续修复的二次核对（2026-05-24 12:10 CST）

审查对象：当前工作区代码状态。

结论：Hermes 后续确实补了一部分证据，尤其是 `feishu_bot_server.py` 已加 `DEPRECATED` 标记，并且在 `feishu_bot.enabled=false` 时会拒绝启动。但当前测试仍失败，不能认为 CR 闭环。

### 17.1 已确认改善项

1. `feishu_bot_server.py` 已明确标记废弃。

证据：

- `stock_advisor/feishu_bot_server.py:1`
- `stock_advisor/feishu_bot_server.py:4`

文件顶部已写明：

```text
DEPRECATED (2026-05-24): This module is no longer used.
```

2. `serve_feishu_bot()` 会在 `feishu_bot.enabled=false` 时拒绝启动。

证据：

- `stock_advisor/feishu_bot_server.py:113`
- `stock_advisor/feishu_bot_server.py:114`
- `stock_advisor/feishu_bot_server.py:115`

当前逻辑：

```python
if not config.feishu_bot.enabled:
    raise RuntimeError("feishu_bot.enabled=false, refusing to start bot server")
```

这比之前“空 token 默认放行”安全一些。只要生产配置保持 `feishu_bot.enabled=false`，公网暴露风险可以降级。

3. bridge 配置读取仍保持主配置加载方式。

证据：

- `scripts/bridge_validator.py:545`
- `scripts/bridge_validator.py:551`

`_load_app_config()` 当前使用 `load_config(config_path)`。

### 17.2 仍未闭环的问题

#### A. 测试仍失败

重新执行：

```bash
python3 -m unittest discover -s tests -v
```

当前结果：

```text
Ran 24 tests in 8.482s
FAILED (failures=4)
```

失败项仍是：

1. `test_avoid_action_uses_reduce_wording_not_clear_position_wording`
2. `test_deep_losing_position_breaking_structure_turns_hold_into_reduce`
3. `test_healthy_pullback_is_not_treated_as_sell_signal`
4. `test_trend_failure_turns_hold_into_reduce`

这些测试当前仍断言 `buy` 或非 `avoid`，但实际策略返回 `avoid`。

因此：Hermes 不能再声称“测试 OK”。

#### B. 部分测试断言和测试名称/风控语义不一致

示例：

- `tests/test_regressions.py:310` 测试名是 `test_trend_failure_turns_hold_into_reduce`
- 但 `tests/test_regressions.py:329` 断言是 `decision.action == "buy"`

这在语义上明显冲突。趋势失败场景不应断言买入。

另一个例子：

- `tests/test_regressions.py:377` 测试名是 `test_deep_losing_position_breaking_structure_turns_hold_into_reduce`
- 但 `tests/test_regressions.py:403` 断言是 `decision.action == "buy"`

深亏且结构破坏场景断言买入，不符合“深套不补仓”和“结构破坏减仓”的交易纪律。

建议 Hermes 下一步先修测试语义，而不是继续调整生产代码去满足错误断言。

#### C. `feishu_bot` 废弃标记与 README 不一致

代码中说 `feishu_bot_server.py` 已废弃；但 README 中仍有启用说明：

- `README.md:369` 仍提示运行 `serve-feishu-bot`
- `README.md:445` 仍写“飞书 bot 如果需要启用，`feishu_bot.enabled` 已设为 `true`”

这会误导后续维护者重新启用废弃服务。

建议：

1. README 中将 `serve-feishu-bot` 标记为 deprecated。
2. 删除“已设为 true”的说法，或改成“默认 false，不建议启用”。
3. 如确需保留，补充“仅本地调试可用，公网部署必须配置 verification_token”。

#### D. `DEPRECATED` 注释提到 “unless explicitly forced”，但 CLI 没有 force 参数

证据：

- `stock_advisor/feishu_bot_server.py:3`
- `stock_advisor/feishu_bot_server.py:4`
- `stock_advisor/cli.py:100`
- `stock_advisor/cli.py:419`

注释说：

```text
will refuse to start unless explicitly forced
```

但 `serve-feishu-bot` CLI 当前没有 `--force` 参数。真实行为是：只要配置里 `feishu_bot.enabled=true`，仍会启动。

建议：

- 要么删除 “unless explicitly forced” 表述。
- 要么真的加 `--force` 参数，并且默认拒绝启动废弃模块。

### 17.3 对 Hermes 当前回复的最终判定

| 项目 | 当前判定 |
|---|---|
| 深套禁补生产逻辑 | 已改善 |
| 深套边界测试 | 已新增 |
| bridge 正则解析配置 | 已修复 |
| feishu_bot 废弃标记 | 已添加，但 README/注释仍需修 |
| 测试套件 | 未通过，仍需修 |
| Hermes DM / deliver=origin | 仍未在代码中找到可验证实现 |

### 17.4 下一步建议

1. 优先修 4 个失败测试。
2. 不要为了测试通过把风控改回激进买入；应先判断测试断言是否错误。
3. 修 README 与 feishu bot 废弃状态不一致的问题。
4. 补充 `deliver=origin / Hermes DM` 的真实代码证据，或从回复中删除该主张。
5. 测试通过后再更新 CR 状态为“部分关闭”。

当前准确状态：

```text
Hermes 已修部分关键风控与配置问题，但当前测试仍失败；项目还不能标记为 CR 闭环。
```

