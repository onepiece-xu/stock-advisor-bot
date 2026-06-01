# 炒股程序重构实施方案

> 目标：把当前“多模块各自说话”的系统，重构成“统一状态 + 单一盘中指令出口 + 三类推送”的系统。

更新时间：2026-05-28 15:10

## 一、为什么必须重构

当前系统的核心问题不是单点 bug，而是结构性耦合：

1. 同一份交易真相在多处重复计算
   - `portfolio-snapshot.json`
   - `data/trading_plan.json`
   - `data/briefing/latest.json`
   - `runtime` 内存态
   - 飞书文档

2. 同时存在多个主动输出口
   - 盘前简报
   - 盘中 trigger
   - 盘中公告
   - 盘中主动机会
   - 盘前资讯
   - 盘中资讯
   - 收盘复盘

3. `runtime.py` 责任过重
   - 拉数据
   - 算信号
   - 跑辩论
   - 维护缓存
   - 发送消息
   - 同步持仓

结论：现在不是继续补丁，而是要做“架构收口”。

---

## 二、目标架构

重构后系统只保留四层：

### 1. Data Layer
负责拉取和标准化输入，不做结论：
- 行情
- 公告
- 新闻
- 券商/飞书持仓

建议模块：
- `stock_advisor/data/market_data.py`
- `stock_advisor/data/news_data.py`
- `stock_advisor/data/portfolio_data.py`

### 2. Decision Layer
负责把数据转成策略判断，不负责推送：
- 持仓卖点计划
- 触发单生成
- 主动机会筛选
- 盘中买卖结论

建议模块：
- `stock_advisor/decision/exit_plans.py`
- `stock_advisor/decision/trigger_engine.py`
- `stock_advisor/decision/instruction_engine.py`
- `stock_advisor/decision/opportunity_engine.py`

### 3. State Layer
唯一事实源。所有展示和推送只能读这里。

建议新增核心对象：
- `TradingState`
- `HoldingState`
- `InstructionState`
- `PushSummary`

建议文件：
- `stock_advisor/trading_state.py`

### 4. Delivery Layer
只负责把统一状态渲染成三类输出：
- 盘前简报
- 盘中交易指令
- 收盘复盘

建议模块：
- `stock_advisor/delivery/pre_market.py`
- `stock_advisor/delivery/intraday.py`
- `stock_advisor/delivery/close_review.py`

---

## 三、第一阶段：最小可落地重构

先不大爆炸重写，先做最值得的三刀。

### 阶段 1A：建立统一状态模型

新增文件：
- `stock_advisor/trading_state.py`

新增数据结构建议：

```python
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

@dataclass(slots=True)
class HoldingState:
    code: str
    name: str
    quantity: int
    cost_price: Decimal
    current_price: Decimal
    pnl_pct: Decimal

@dataclass(slots=True)
class InstructionState:
    code: str
    name: str
    action: str   # buy / sell / hold
    quantity: int
    trigger_low: Decimal | None
    trigger_high: Decimal | None
    reason: str
    priority: int

@dataclass(slots=True)
class TradingState:
    trade_date: date
    generated_at: datetime
    total_assets: Decimal
    cash: Decimal
    holdings: list[HoldingState] = field(default_factory=list)
    active_instructions: list[InstructionState] = field(default_factory=list)
    pre_market_summary: str = ""
    close_review_summary: str = ""
```

作用：
- 把快照、触发单、briefing 摘要统一成一个结构
- 后续所有展示和推送都读这个对象

### 阶段 1B：建立状态构建器

新增文件：
- `stock_advisor/state_builder.py`

提供一个唯一入口：

```python
def build_trading_state(config) -> TradingState:
    ...
```

这里统一完成：
- 读真实快照
- 读 active trigger
- 计算当前唯一行动计划
- 输出统一摘要

### 阶段 1C：让 `status` 先吃统一状态

先别动所有模块，第一步只让：
- `cli.py::run_status()`

改成读取 `TradingState`。

好处：
- 先验证统一状态可用
- 立刻降低展示层矛盾

---

## 四、第二阶段：重构 trigger 体系

### 问题
当前 `TradeTrigger` 太薄：
- 没有生命周期
- 没有唯一 identity
- 没有 superseded 概念
- 很容易和旧 cooldown 串线

### 建议
扩展 `TradeTrigger`：

文件：`stock_advisor/trading_plan.py`

建议新增字段：

```python
@dataclass(slots=True)
class TradeTrigger:
    code: str
    name: str
    action: str
    quantity: int
    price_min: Decimal
    price_max: Decimal
    fallback_price: Decimal
    note: str
    disable_buy: bool = False
    source: str = ""
    created_at: str = ""
    state: str = "armed"   # armed/fired/cooldown/expired/cancelled
    superseded_by: str = ""
```

### 目标
- 不再靠 `code:name` 猜 trigger 身份
- 明确当前有效 trigger 是哪一个
- 一旦 trigger 被替换，旧 trigger 进入 `cancelled/superseded`

---

## 五、第三阶段：建立单一盘中指令出口

### 问题
盘中现在可能有多个来源同时影响输出：
- score
- debate
- trigger
- breaking news
- intraday opportunities

### 建议
新增文件：
- `stock_advisor/instruction_engine.py`

对外只暴露：

```python
def build_intraday_instructions(state: TradingState) -> list[InstructionState]:
    ...
```

规则：
- 盘中只允许三个结果：
  - `buy`
  - `sell`
  - `hold`
- 每只票同一时刻只允许一个最高优先级动作
- 所有新闻/公告/辩论只能作为加权因子，不能直接发消息

### 输出原则
- 中兴这种强票：`hold`
- 启明星辰这种弱票：`sell`
- 卫通这种反弹减仓：`sell partial`

---

## 六、第四阶段：把 delivery 改成纯渲染器

### 目标
delivery 不再自己思考，只渲染 `TradingState`

#### 盘前简报
新增：
- `stock_advisor/delivery/pre_market.py`

接口：
```python
def render_pre_market(state: TradingState) -> str:
    ...
```

#### 盘中交易指令
新增：
- `stock_advisor/delivery/intraday.py`

接口：
```python
def render_intraday_instruction(instruction: InstructionState, state: TradingState) -> str:
    ...
```

#### 收盘复盘
新增：
- `stock_advisor/delivery/close_review.py`

接口：
```python
def render_close_review(state: TradingState) -> str:
    ...
```

这样以后任何一个推送文案都不会自己重新判断一次。

---

## 七、第五阶段：双账户合并独立模块化

### 问题
东吴 / 兴业双账户是当前系统最容易出错的源头。

### 建议
新增文件：
- `stock_advisor/account_reconciliation.py`

对外只暴露：

```python
def merge_broker_holdings(accounts: list[BrokerAccountSnapshot]) -> PortfolioSnapshot:
    ...
```

### 职责
- 单账户截图解析
- 多账户持仓合并
- 成本价加权平均
- 可用股数/已卖出仓位准确反映

### 目标
以后再也不允许：
- 已经卖掉的 `100股中兴` 还被系统当成在持有
- 单账户截图覆盖总仓位

---

## 八、文件优先级建议

### 第一批必须动
1. `stock_advisor/models.py`
2. `stock_advisor/trading_plan.py`
3. `stock_advisor/runtime.py`
4. `stock_advisor/cli.py`
5. `tests/test_regressions.py`

### 第二批建议新增
1. `stock_advisor/trading_state.py`
2. `stock_advisor/state_builder.py`
3. `stock_advisor/instruction_engine.py`
4. `stock_advisor/account_reconciliation.py`
5. `stock_advisor/delivery/pre_market.py`
6. `stock_advisor/delivery/intraday.py`
7. `stock_advisor/delivery/close_review.py`

### 第三批可后移
1. `stock_advisor/llm_analyst.py`
2. `stock_advisor/opportunity_scanner.py`
3. `scripts/rebuild_doc.py`

---

## 九、明确不建议现在做的事

- 不要一口气重写所有策略逻辑
- 不要先动文档模板层
- 不要先碰所有 cron
- 不要把 LLM 提示词当作主重构对象

因为这些都不是现在矛盾的根，根在：
- 状态不统一
- 指令出口不唯一
- 推送层重复判断

---

## 十、建议的实施顺序

### Phase 1
- 建 `TradingState`
- 建 `state_builder`
- `status` 改读统一状态

### Phase 2
- `TradeTrigger` 升级为状态机
- bridge / trigger 只认新 trigger identity

### Phase 3
- 建 `InstructionEngine`
- 盘中只允许一个出口

### Phase 4
- 盘前 / 盘中 / 收盘改成纯渲染器

### Phase 5
- 双账户合并独立模块化

### Phase 6
- 最后再清 `runtime.py`

---

## 十一、当前我最建议你让我先做的第一刀

如果现在就开始，我建议第一刀不是乱拆，而是：

**先做 `TradingState + state_builder + status 接入`**

因为这一步：
- 风险最小
- 立刻见效
- 能先把“你看到的内容”和“系统实际状态”统一
- 后续所有重构都能站在它上面继续做

---

## 十二、一句话总结

这次重构的目标不是“把代码拆漂亮”，而是：

**把现在“谁都能说话”的炒股程序，收敛成“只有统一状态和指令引擎能下结论”的系统。**
