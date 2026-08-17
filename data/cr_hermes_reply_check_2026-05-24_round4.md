## 19. 对 Hermes 最新回复的四次核对（2026-05-24 12:32 CST）

审查对象：

- 当前仓库：`/root/projects/stock-advisor-bot`
- Hermes 外部配置：`/root/.hermes/scripts/stock_advisor_bridge.sh`、`/root/.hermes/cron/jobs.json`
- 飞书文档中 Hermes 对 §18 的回复

结论：Hermes 关于 `deliver=origin` 是“框架层能力、不在 stock-advisor-bot 仓库内”的说法已找到外部证据，可以从“缺少证据”更新为“外部配置证据成立”。但仓库内 outbox 仍没有 delivered/failed/blocked 状态机，且当前 bridge 在 stdout 投递确认前就执行 consume，投递失败时存在消息丢失风险。测试虽通过，但仍不 hermetic，并且会污染 tracked 数据文件。

### 19.1 本轮验证结果

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
Ran 24 tests in 33.892s
OK
```

测试期间仍出现外部网络 warning：

- `stock_advisor.sector_strength`: Eastmoney 连接被远端关闭
- `stock_advisor.atr_risk`: ATR 拉取 OHLC 失败

新增观察：测试运行后修改了 tracked 文件 `data/signals/signal_log.jsonl`，追加了多条测试信号记录。这说明测试不仅依赖外部网络，还会污染工作树，仍不能算可重复、无副作用的单元测试。

### 19.2 Hermes DM / deliver=origin 证据已补齐

Hermes 回复称：

```text
stock-advisor-bot 的 bridge_validator.py --mode validate 输出通知到 stdout；
Hermes cron 捕获 stdout；
Hermes 框架用 deliver=origin 投递到飞书会话。
```

本轮已找到外部证据：

- `/root/.hermes/scripts/stock_advisor_bridge.sh:16`
- `/root/.hermes/scripts/stock_advisor_bridge.sh:17`
- `/root/.hermes/scripts/stock_advisor_bridge.sh:18`

脚本实际执行：

```bash
python3 scripts/bridge_validator.py --mode validate \
  && python3 scripts/bridge_validator.py --mode consume
```

Hermes cron 配置证据：

- `/root/.hermes/cron/jobs.json:5`：job 名为 `stock-advisor-feishu-bridge`
- `/root/.hermes/cron/jobs.json:12`：脚本为 `stock_advisor_bridge.sh`
- `/root/.hermes/cron/jobs.json:13`：`no_agent=true`
- `/root/.hermes/cron/jobs.json:20`：每 1 分钟执行
- `/root/.hermes/cron/jobs.json:32`：最近状态 `ok`
- `/root/.hermes/cron/jobs.json:35`：`deliver=origin`
- `/root/.hermes/cron/jobs.json:37`：origin 平台为 `feishu`

因此本项判断更新为：

```text
Hermes DM / deliver=origin 在本仓库内没有实现是事实；但外部 Hermes cron 配置中确实存在 deliver=origin 投递链路。此项可以关闭“缺证据”争议，但应在 README/运维文档中写明依赖 /root/.hermes，而不是让读者在仓库内寻找 origin delivery mode。
```

### 19.3 新发现：bridge consume 早于投递确认，存在丢消息窗口

当前 Hermes bridge 脚本逻辑是：

```bash
validate 输出 stdout && consume 标记 sent
```

但 Hermes 的 `deliver=origin` 是框架在脚本 stdout 产生后进行的外部投递。也就是说，仓库脚本并不知道 Hermes 是否真的投递成功。

代码证据：

- `scripts/bridge_validator.py:388`：`run_validate()` 输出通过校验的消息到 stdout
- `scripts/bridge_validator.py:446`：逐条 `print(msg)`
- `scripts/bridge_validator.py:491`：`run_consume()` 标记已消费
- `scripts/bridge_validator.py:493`：实际调用 `_mark_sent(limit=10)`
- `scripts/bridge_validator.py:472`：`_mark_sent()` 直接把未发送记录置为 `sent=True`

风险：

- 如果 `validate` 输出成功，但 Hermes 框架投递飞书失败，脚本仍已执行 `consume`。
- outbox 记录会被标记为 `sent=True`，后续不会重试。
- `last_delivery_error` 在 Hermes cron 层可见，但 stock-advisor-bot 的 outbox 不知道失败，无法审计或补偿。

这就是之前 “outbox 缺少 delivered/blocked/failed 状态机” 的具体落点。建议保留为 P1/P2 之间的问题，至少需要在运维文档中明确“依赖 Hermes delivery 层保证”，更稳妥的方案是由 Hermes 投递成功后再 ack，或让 bridge 自己负责 app_dm/webhook 投递并按结果更新状态。

### 19.4 当前仍未关闭的问题

#### A. 测试不 hermetic，且污染 tracked 文件

当前测试通过，但同时满足两个不理想条件：

- 真实访问外部行情接口。
- 向 `data/signals/signal_log.jsonl` 追加记录，导致 `git status` 出现 tracked diff。

建议：

1. 单元测试 mock `sector_strength` 和 ATR/OHLC 拉取。
2. 测试中把 signal log path 指向临时目录。
3. CI 增加 `git diff --exit-code` 或类似检查，避免测试污染仓库。

#### B. 本仓库配置仍不是 `origin` delivery mode

仓库内状态仍是：

- `config.yaml`：`monitor.notification.feishu.enabled=false`
- `config.yaml`：`delivery_mode=webhook`，但因 enabled=false 不生效
- `stock_advisor/config.py:350`：允许值仍只有 `webhook/direct_dm/app_dm/outbox`
- `scripts/bridge_validator.py:619`：内置 deliver 模式只处理 `webhook`
- `scripts/bridge_validator.py:626`：内置 deliver 模式只处理 `app_dm`

这与 Hermes 的说法不冲突，但需要文档化：

```text
stock-advisor-bot 本身不实现 origin delivery；当前生产投递由 /root/.hermes cron 捕获 stdout 并 deliver=origin 到飞书。
```

#### C. feishu_bot 已降级，但弱鉴权入口仍存在

当前状态：

- `stock_advisor/feishu_bot_server.py` 已标记 deprecated。
- `feishu_bot.enabled=false` 时拒绝启动。
- README 已标记历史功能。
- 但 `_is_valid_verification_token()` 在 token 为空时仍返回 `True`。
- CLI 入口 `serve-feishu-bot` 仍存在。

建议状态：P2，后续删除入口或强制 token 非空即可。

### 19.5 最新状态表

| 项目 | 当前状态 |
|---|---|
| P0 深套禁补逻辑 | 已修复，可关闭 |
| P0 深套边界测试 | 已新增，可关闭 |
| bridge 正则解析配置 | 已修复，可关闭 |
| 单元测试 | 当前 24/24 通过 |
| README feishu_bot 废弃说明 | 已修复 |
| Hermes DM / deliver=origin 证据 | 外部 Hermes 配置已证实，可关闭“缺证据”争议 |
| 测试外部网络依赖 | 未关闭，保留 P2 |
| 测试污染 tracked 数据文件 | 新发现，建议 P2 |
| outbox delivered/failed/blocked 状态机 | 未关闭，且 stdout 投递前 consume 是具体风险点 |
| 凭证治理 | 未关闭，建议 P1 |
| feishu_bot 弱鉴权废弃入口 | 已降级，保留 P2 清理 |

### 19.6 更新后的准确结论

```text
Hermes 已修复关键深套风控、bridge 配置读取、README/feishu_bot 废弃说明；当前 24 个测试通过。Hermes DM / deliver=origin 的外部配置证据成立。但测试仍不 hermetic 且会污染 tracked 数据文件，outbox 在 Hermes 投递确认前就 consume，仍存在消息丢失和审计缺口。项目可以继续作为人工辅助提醒工具运行，但不建议接自动下单。
```
