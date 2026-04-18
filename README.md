# stock-advisor-bot

股票观察/持仓建议项目的 Python 最小可运行版本，现已扩展为一个本地运行、飞书优先的“AI 辅助股票分析决策工具” MVP。

当前已具备：

- 拉取 A 股公开行情（默认腾讯）
- 计算基础观察信号
- 生成 AI 辅助决策分数、动作、置信度和状态
- 把行情 / 信号 / 决策持久化到 SQLite
- 基于历史信号做回放统计
- 输出适合手机飞书机器人的短摘要
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
- 每条决策都会给出动作、分数、状态、理由、风险标签，便于后续继续接 LLM 总结层
- 当前使用场景按“手机飞书机器人查看”为主来优化输出，不假设你会长期守着电脑终端
- 飞书通知支持两种投递方式：`webhook` 和 `direct_dm`

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
- 单次分析会优先复用 SQLite 中的最近样本，避免只看当前一跳行情

如果使用 `direct_dm` 模式，消息会先写到本地 outbox，可用下面脚本继续转发：

```bash
python3 scripts/flush_direct_dm_outbox.py
```

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

- 当前还没有接入真正的飞书消息回调服务，更多是“飞书友好输出 + webhook 推送”
- 决策层目前是规则评分引擎，不是接入 LLM 的研究代理
- 腾讯行情接口属于公开行情源，稳定性不如正式行情服务
- 还没做 systemd / 开机自启

## 下一步建议

- 加飞书机器人命令回调入口，让手机直接发“/brief”“/replay reduce”这类指令
- 接入大模型做新闻 / 公告 / 财报摘要
- 增加 systemd 托管和开机自启
- 加入导出日报 / 周报
