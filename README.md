# stock-advisor-bot

股票观察/持仓建议项目的 Python 最小可运行版本。

当前已具备：

- 拉取 A 股公开行情（默认腾讯）
- 计算基础观察信号
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

## 安装依赖

```bash
cd /home/xulihua/projects/stock-advisor-bot
python3 -m pip install --user -r requirements.txt
```

## 配置文件

- 示例配置：`config.example.yaml`
- 当前实际配置：`config.yaml`

## 单次行情轮询

```bash
python3 -m stock_advisor.cli monitor-once --config config.yaml
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

## 当前限制

- 历史行情缓存目前在内存里，重启后会清空
- 还没加交易时段限制
- 还没做 systemd / 开机自启
- git commit 尚未完成，因为本机 git user.name / user.email 未配置

## 下一步建议

- 加交易时段限制
- 历史行情持久化到文件
- systemd 托管
- 自动读取持仓截图 / 文档
