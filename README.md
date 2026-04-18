# stock-advisor-bot

股票观察/持仓建议项目的 Python 最小可运行版本，现已扩展为一个本地运行、飞书优先的“AI 辅助股票分析决策工具” MVP。

当前已具备：

- 拉取 A 股公开行情（默认腾讯）
- 计算基础观察信号
- 生成 AI 辅助决策分数、动作、置信度和状态
- 把行情 / 信号 / 决策持久化到 SQLite
- 基于历史信号做回放统计
- 输出适合手机飞书机器人的短摘要
- 支持飞书机器人消息回调服务
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
- 单次分析会优先复用 SQLite 中的最近样本，避免只看当前一跳行情
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

如果使用 `direct_dm` 模式，消息会先写到本地 outbox，可用下面脚本继续转发：

```bash
python3 scripts/flush_direct_dm_outbox.py
```

如果 `webhook` 推送重试后仍失败，会写入失败补偿队列，可用下面脚本重放：

```bash
python3 scripts/flush_failed_notifications.py
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
quote 601698
scan 601698
replay
replay reduce
replay ALERT
replay 601698
replay action=reduce level=ALERT symbol=601698
```

命令含义：

- `brief`：返回当前缓存中的聚合决策简报
- `quote`：返回某个股票最近一次落库决策
- `scan`：实时拉一次最新行情并临时分析，不写库
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
