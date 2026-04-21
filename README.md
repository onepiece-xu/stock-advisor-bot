# stock-advisor-bot

股票观察/持仓建议项目的 Python 最小可运行版本，现已扩展为一个本地运行、飞书优先的“AI 辅助股票分析决策工具” MVP。

当前已具备：

- 拉取 A 股分钟级公开行情（默认东方财富分钟线，保留腾讯实时接口兼容）
- 计算基础观察信号
- 生成 AI 辅助决策分数、动作、置信度和状态
- 支持最近 5 日分钟级信号回测
- 把行情 / 信号 / 决策持久化到 SQLite
- 基于历史信号做回放统计
- 输出适合手机飞书机器人的短摘要
- 支持飞书机器人消息回调服务
- 支持每个交易日收盘后的复盘报告
- 支持按任意历史分钟时点回放并重算当时建议
- 输出控制台报告
- 可选发送飞书 webhook 通知
- 支持收盘持仓建议 CLI
- 支持常驻轮询 daemon
- 简单通知去重
- 后台启动 / 停止 / 状态脚本

## 运行环境

- Python 3.10+
- 依赖：PyYAML、requests

如果系统里没有 `venv`，也可以直接用全局 `python3` 运行。

说明：

- 当前的 “AI 辅助决策” 是可解释的规则 + 评分引擎，不是黑盒大模型推荐
- 默认按分钟级窗口做多周期分析：MA5 / MA15 / MA60 / MA240、量比、相对大盘强弱
- 量能分析已扩展到 5 分 / 30 分双基线，并识别“放量突破前高 / 缩量反弹 / 放量跌破前低”
- 系统会根据你真实回传的成交记录学习交易习惯，并自动校准建议股数
- 新增全市场扫描与热点板块概览，可直接查看行业/概念热度和市场涨跌家数
- 每条决策都会给出动作、分数、状态、理由、风险标签，便于后续继续接 LLM 总结层
- `monitor.signal.decision_thresholds` 可直接配置买入 / 持有 / 减仓分数阈值，不再写死在代码里
- 当前使用场景按“手机飞书机器人查看”为主来优化输出，不假设你会长期守着电脑终端
- 飞书通知支持两种投递方式：`webhook` 和 `direct_dm`
- 飞书机器人服务使用 Feishu/Lark 应用凭证，可直接在手机里发命令查询

## 安装依赖

```bash
cd /home/xulihua/projects/stock-advisor-bot
python3 -m pip install --user -r requirements.txt
```

## 配置文件

- 示例配置：`config.example.yaml`
- 当前实际配置：`config.yaml`
- 长期持仓文档（飞书）：`https://wcntg42cmak8.feishu.cn/docx/DXRDdRGRJohquex19VucUqh0nVd`
- 本地结构化快照：`portfolio-snapshot.json`
- 飞书机器人监听配置：`feishu_bot`
- 交易计划样例：`trading-plan.example.json`
- 收盘复盘配置：`review`
- 用户级 systemd 服务文件：`systemd/user/`

## 单次行情轮询

```bash
python3 -m stock_advisor.cli monitor-once --config config.yaml
```

输出手机友好摘要：

```bash
python3 -m stock_advisor.cli monitor-once --config config.yaml --mobile
```

同时发送飞书：

```bash
python3 -m stock_advisor.cli monitor-once --config config.yaml --mobile --notify
```

## 常驻行情轮询

前台运行：

```bash
python3 -m stock_advisor.cli monitor-daemon --config config.yaml
```

后台运行：

```bash
./run-daemon.sh
```

说明：

- daemon 推送到飞书时，会自动使用手机友好摘要，而不是长篇原始报告
- 默认配置已切到 `eastmoney_minute + history_size=480 + benchmark=sh000001 + notify_on_neutral=false`
- 单次分析会优先拉取最近 480 根分钟线窗口，不再只看最近几次轮询样本
- `quote + signal + decision` 现在走单事务写入，避免孤立行情记录

初始化默认交易计划文件：

```bash
python3 -m stock_advisor.cli init-trading-plan --config config.yaml
```

生成后直接编辑项目根目录下的 `trading-plan.json` 即可，不需要改代码重启部署逻辑。

校验配置：

```bash
python3 -m stock_advisor.cli validate-config --config config.yaml
```

## 交易习惯学习

每次真实成交后，建议回传一次，系统会自动记录样本并更新习惯画像：

```bash
python3 -m stock_advisor.cli record-fill \
  --snapshot portfolio-snapshot.json \
  --config config.yaml \
  --side sell \
  --code 601698 \
  --quantity 100 \
  --price 34.52
```

查看当前学到的习惯画像：

```bash
python3 -m stock_advisor.cli habit-profile --config config.yaml --mobile
```

说明：

- 当前学习内容包括：常用买入手数、常用加仓手数、常见减仓比例、偏分批还是偏果断
- 后续给出的买入/减仓股数，会优先参考你的历史成交，而不是一直写死 100 股
- 飞书机器人支持：`habit`

## 收盘复盘

手动生成：

```bash
python3 -m stock_advisor.cli close-review --config config.yaml
```

生成并推送到飞书：

```bash
python3 -m stock_advisor.cli close-review --config config.yaml --notify
```

说明：

- 报告默认保存到 `data/reviews`
- daemon 在非交易时段运行时，如果时间晚于 `review.send_after_hour:review.send_after_minute`，会每天自动生成并最多推送一次
- 没有持仓快照时，仍会生成基于行情与决策的市场复盘
- 如果存在 `portfolio-snapshot.json`，会额外生成持仓复盘段落

如果使用 `direct_dm` 模式，消息会先写到本地 outbox，可用下面脚本继续转发：

```bash
python3 scripts/flush_direct_dm_outbox.py
```

如果 `webhook` 推送重试后仍失败，会写入失败补偿队列，可用下面脚本重放：

```bash
python3 scripts/flush_failed_notifications.py
```

也可以直接走 CLI：

```bash
python3 -m stock_advisor.cli flush-failed-notifications
```

## 历史时点建议

按任意历史分钟时点，回放当时可用的分钟级行情，并重新计算建议：

```bash
python3 -m stock_advisor.cli advice-at \
  --config config.yaml \
  --at "2026-04-17 14:20"
```

只看单个标的：

```bash
python3 -m stock_advisor.cli advice-at \
  --config config.yaml \
  --at "2026-04-17 14:20" \
  --symbol 601698
```

输出手机友好摘要：

```bash
python3 -m stock_advisor.cli advice-at \
  --config config.yaml \
  --at "2026-04-17 14:20" \
  --mobile
```

比较两个历史时点：

```bash
python3 -m stock_advisor.cli compare-at \
  --config config.yaml \
  --from-time "2026-04-17 14:20" \
  --to-time "2026-04-17 15:00" \
  --mobile
```

比较单个标的：

```bash
python3 -m stock_advisor.cli compare-at \
  --config config.yaml \
  --from-time "2026-04-17 14:20" \
  --to-time "2026-04-17 15:00" \
  --symbol 601698 \
  --mobile
```

说明：

- 历史分钟数据默认按需从东方财富分钟线接口拉取，并自动缓存到 SQLite
- 如果请求时点没有精确分钟样本，会回退到该时点之前最近一笔分钟样本，并在输出里明确标注
- 这条能力也支持飞书机器人命令：`at 2026-04-17 14:20`、`at 2026-04-17 14:20 601698`、`at 601698 2026-04-17 14:20`
- 历史对比命令支持：`compare 2026-04-17 14:20 2026-04-17 15:00`、`compare 601698 2026-04-17 14:20 2026-04-17 15:00`

## 分钟级回测

回测最近 5 个交易日的分钟级信号，统计 5/15/30 分钟后的表现：

```bash
python3 -m stock_advisor.cli backtest-minutes \
  --config config.yaml \
  --mobile
```

回测单个标的：

```bash
python3 -m stock_advisor.cli backtest-minutes \
  --config config.yaml \
  --days 3 \
  --symbol 601698 \
  --mobile
```

说明：

- 原始收益均值：信号发出后实际涨跌幅
- 策略边际均值：`buy/hold` 看上涨是否赚钱，`reduce/avoid` 看后续下跌是否判断正确
- 手机摘要会额外给出 15 分钟动作拆解，避免总结果被 `reduce` 一类动作掩盖
- 飞书机器人支持：`backtest`、`backtest 3`、`backtest 601698`、`backtest 3 601698`

## 阈值优化

用最近几日分钟样本，直接搜索更合适的 `buy/hold/reduce` 分数阈值：

```bash
python3 -m stock_advisor.cli optimize-thresholds \
  --config config.yaml \
  --days 5 \
  --mobile
```

优化单个标的：

```bash
python3 -m stock_advisor.cli optimize-thresholds \
  --config config.yaml \
  --days 3 \
  --symbol 601698 \
  --mobile
```

说明：

- 优化不会重新设计打分逻辑，只会在已有分数上重映射动作阈值，速度更快，也更容易解释
- 输出会同时展示当前阈值表现、候选阈值排名，以及可直接写回 `config.yaml` 的配置片段
- 飞书机器人支持：`optimize`、`optimize 3`、`optimize 601698`、`optimize 3 601698`

查看状态：

```bash
./status-daemon.sh
```

停止：

```bash
./stop-daemon.sh
```

日志文件：

```bash
logs/monitor.log
```

## 收盘持仓建议

```bash
python3 -m stock_advisor.cli portfolio-report \
  --config config.yaml \
  --snapshot portfolio-snapshot.example.json
```

如需同时发送飞书 webhook：

```bash
python3 -m stock_advisor.cli portfolio-report \
  --config config.yaml \
  --snapshot portfolio-snapshot.example.json \
  --notify
```

## 飞书手机简报

输出当前库里各标的最新决策的聚合简报：

```bash
python3 -m stock_advisor.cli mobile-brief --config config.yaml
```

发送到飞书：

```bash
python3 -m stock_advisor.cli mobile-brief --config config.yaml --notify
```

适合后续在定时任务或飞书机器人命令里直接调用。

## 全市场扫描

查看当前全市场涨跌家数、热点行业、热点概念和龙头个股：

```bash
python3 -m stock_advisor.cli market-scan --config config.yaml --mobile
```

说明：

- 数据来自东方财富行情列表接口
- 默认输出上涨/平盘/下跌家数、热点行业、热点概念、领涨个股
- 接口抖动时会自动重试；若实时接口暂不可用，会回退到最近一次成功的市场扫描快照
- 飞书机器人支持：`market`

## 飞书机器人命令服务

启动服务：

```bash
python3 -m stock_advisor.cli serve-feishu-bot --config config.yaml
```

示例配置：

```yaml
feishu_bot:
  enabled: true
  app_id: "cli_xxx"
  app_secret: "xxx"
  verification_token: "xxx"
  listen_host: 0.0.0.0
  listen_port: 8788
  allowed_chat_ids: []
```

说明：

- 需要在飞书开放平台里创建应用，打开机器人能力和事件订阅
- 事件订阅里至少订阅 `im.message.receive_v1`
- 回调 URL 指向你家里机器能被飞书访问到的地址
- 当前实现只支持未加密回调。如果飞书后台开启了 Encrypt Key，需要先关闭加密
- `allowed_chat_ids` 为空表示不限制；如果填了，只允许这些会话触发命令

手机里可直接发送这些命令：

```text
help
brief
review
market
quote 601698
scan 601698
at 2026-04-17 14:20
at 2026-04-17 14:20 601698
at 601698 2026-04-17 14:20
compare 2026-04-17 14:20 2026-04-17 15:00
compare 601698 2026-04-17 14:20 2026-04-17 15:00
backtest
backtest 3
backtest 601698
backtest 3 601698
optimize
optimize 3
optimize 601698
optimize 3 601698
habit
replay
replay reduce
replay ALERT
replay 601698
replay action=reduce level=ALERT symbol=601698
```

命令含义：

- `brief`：返回当前缓存中的聚合决策简报
- `review`：返回最近一个交易日的收盘复盘
- `market`：返回全市场涨跌家数、热点板块和龙头个股
- `quote`：返回某个股票最近一次落库决策
- `scan`：实时拉一次最新行情并临时分析，不写库
- `at`：按历史分钟时点重算当时建议，股票代码可以写前面也可以写后面
- `compare`：比较两个历史时点的价格、动作、评分和状态变化
- `backtest`：回测最近几日的分钟级信号，验证策略是否真的有边际
- `optimize`：回测后给出更合适的动作阈值建议
- `habit`：返回系统当前学到的交易习惯画像
- `replay`：返回历史回放统计，可按动作 / 等级 / 股票过滤

## systemd 自启动

适合把家里的机器作为长期运行节点。

先确认：

- WSL 已启用 `systemd`
- `config.yaml` 已填写完成
- 飞书 bot 如果需要启用，`feishu_bot.enabled` 已设为 `true`

安装用户级服务：

```bash
./scripts/install_systemd_user_services.sh
```

启用监控服务：

```bash
systemctl --user enable --now stock-advisor-monitor.service
```

启用飞书 bot 服务：

```bash
systemctl --user enable --now stock-advisor-feishu-bot.service
```

查看状态：

```bash
systemctl --user status stock-advisor-monitor.service
systemctl --user status stock-advisor-feishu-bot.service
```

跟踪日志：

```bash
journalctl --user -u stock-advisor-monitor.service -f
journalctl --user -u stock-advisor-feishu-bot.service -f
```

## 历史回放统计

```bash
python3 -m stock_advisor.cli replay-signals --config config.yaml
```

按信号等级过滤：

```bash
python3 -m stock_advisor.cli replay-signals \
  --config config.yaml \
  --level ALERT
```

按决策动作过滤：

```bash
python3 -m stock_advisor.cli replay-signals \
  --config config.yaml \
  --action reduce
```

发送回放摘要到飞书：

```bash
python3 -m stock_advisor.cli replay-signals \
  --config config.yaml \
  --action reduce \
  --notify
```

## 当前限制

- 飞书机器人当前只支持文本消息命令，不支持卡片交互和加密事件
- 决策层目前是规则评分引擎，不是接入 LLM 的研究代理
- 腾讯行情接口属于公开行情源，稳定性不如正式行情服务
- 交易计划文件当前是 JSON，尚未接入飞书侧动态改价
- 当前只提供用户级 systemd 文件，未提供 root 级 system service

## 下一步建议

- 给飞书机器人加菜单卡片和快捷按钮
- 接入大模型做新闻 / 公告 / 财报摘要
- 增加 systemd 健康检查和失败告警
- 加入导出日报 / 周报
