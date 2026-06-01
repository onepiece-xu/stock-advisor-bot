# Phase 1 代码重构记录

目标：先落地统一状态模型 `TradingState`，并让 `status` 读统一状态，而不是继续拼装快照/trigger/briefing。

本阶段范围：
1. 新增 `stock_advisor/trading_state.py`
2. 新增 `stock_advisor/state_builder.py`
3. 修改 `stock_advisor/cli.py` 的 `run_status()` 改读统一状态
4. 补回归测试，确保 status 展示与统一状态一致

非目标：
- 不重写策略引擎
- 不大改 runtime
- 不重写 trigger 存储格式
