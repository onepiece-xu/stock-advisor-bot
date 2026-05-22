#!/usr/bin/env python3
"""Reconstruct full Feishu doc from backup + new section, then push."""
import json, re, subprocess

DOC_TOKEN = "DXRDdRGRJohquex19VucUqh0nVd"

# Extract markdown from backup (not valid JSON - manual extraction)
with open("/tmp/portfolio_doc_current.md") as f:
    raw = f.read()

m = re.search(r'"markdown": "(.+?)(?:",\s*\n\s*"message")', raw, re.DOTALL)
if not m:
    m = re.search(r'"markdown": "(.+)"', raw, re.DOTALL)
old_md = m.group(1) if m else ""
old_md = old_md.replace('\\u003c', '<').replace('\\u003e', '>').replace('\\"', '"')
old_md = old_md.replace('\\n', '\n')

print(f"Old md: {len(old_md)} chars, starts with: {old_md[:40]}")

# 5.13 section
section_513 = """# 2026.5.13

## 账户概览

- 总资产：47255.35
- 总盈亏：-1435.87
- 今日盈亏：+140.00（+0.30%）
- 总市值：12347.00
- 可用/可取：34908.35 / 34086.76
- 仓位：26.1%

## 持仓明细

<lark-table rows="4" cols="7" header-row="true" column-widths="104,104,104,104,104,104,104">
  <lark-tr>
    <lark-td>股票</lark-td>
    <lark-td>市值 {align="right"}</lark-td>
    <lark-td>盈亏金额 {align="right"}</lark-td>
    <lark-td>盈亏比例 {align="right"}</lark-td>
    <lark-td>持仓/可用 {align="right"}</lark-td>
    <lark-td>成本价 {align="right"}</lark-td>
    <lark-td>现价 {align="right"}</lark-td>
  </lark-tr>
  <lark-tr>
    <lark-td>中国卫通</lark-td>
    <lark-td>7670.00 {align="right"}</lark-td>
    <lark-td>313.20 {align="right"}</lark-td>
    <lark-td>4.26% {align="right"}</lark-td>
    <lark-td>200 / 200 {align="right"}</lark-td>
    <lark-td>36.783 {align="right"}</lark-td>
    <lark-td>38.350 {align="right"}</lark-td>
  </lark-tr>
  <lark-tr>
    <lark-td>中兴通讯</lark-td>
    <lark-td>3848.00 {align="right"}</lark-td>
    <lark-td>-54.92 {align="right"}</lark-td>
    <lark-td>-1.41% {align="right"}</lark-td>
    <lark-td>100 / 100 {align="right"}</lark-td>
    <lark-td>39.029 {align="right"}</lark-td>
    <lark-td>38.480 {align="right"}</lark-td>
  </lark-tr>
  <lark-tr>
    <lark-td>南网能源</lark-td>
    <lark-td>829.00 {align="right"}</lark-td>
    <lark-td>-1694.15 {align="right"}</lark-td>
    <lark-td>-67.16% {align="right"}</lark-td>
    <lark-td>100 / 100 {align="right"}</lark-td>
    <lark-td>25.240 {align="right"}</lark-td>
    <lark-td>8.290 {align="right"}</lark-td>
  </lark-tr>
</lark-table>

## 备注

- 南网能源已减仓至100股，成本25.240
- 中国卫通浮盈 +4.26%
- 中兴通讯微亏 -1.41%
- 数据来源：券商APP

"""

# 5.15 section
section_515 = """# 2026.5.15

## 账户概览

- 总资产：46341.35
- 总盈亏：-2349.41
- 当日参考盈亏：-236.00（-0.51%）
- 总市值：11432.00
- 可用/可取：34909.35 / 34909.35
- 仓位：24.7%

## 持仓明细

<lark-table rows="4" cols="7" header-row="true" column-widths="104,104,104,104,104,104,104">
  <lark-tr>
    <lark-td>股票</lark-td>
    <lark-td>市值 {align="right"}</lark-td>
    <lark-td>盈亏金额 {align="right"}</lark-td>
    <lark-td>盈亏比例 {align="right"}</lark-td>
    <lark-td>持仓/可用 {align="right"}</lark-td>
    <lark-td>成本价 {align="right"}</lark-td>
    <lark-td>现价 {align="right"}</lark-td>
  </lark-tr>
  <lark-tr>
    <lark-td>中国卫通</lark-td>
    <lark-td>7018.00 {align="right"}</lark-td>
    <lark-td>-338.46 {align="right"}</lark-td>
    <lark-td>-4.60% {align="right"}</lark-td>
    <lark-td>200 / 200 {align="right"}</lark-td>
    <lark-td>36.783 {align="right"}</lark-td>
    <lark-td>35.090 {align="right"}</lark-td>
  </lark-tr>
  <lark-tr>
    <lark-td>中兴通讯</lark-td>
    <lark-td>3671.00 {align="right"}</lark-td>
    <lark-td>-231.84 {align="right"}</lark-td>
    <lark-td>-5.94% {align="right"}</lark-td>
    <lark-td>100 / 100 {align="right"}</lark-td>
    <lark-td>39.029 {align="right"}</lark-td>
    <lark-td>36.710 {align="right"}</lark-td>
  </lark-tr>
  <lark-tr>
    <lark-td>南网能源</lark-td>
    <lark-td>743.00 {align="right"}</lark-td>
    <lark-td>-1779.11 {align="right"}</lark-td>
    <lark-td>-70.55% {align="right"}</lark-td>
    <lark-td>100 / 100 {align="right"}</lark-td>
    <lark-td>25.230 {align="right"}</lark-td>
    <lark-td>7.430 {align="right"}</lark-td>
  </lark-tr>
</lark-table>

## 备注

- 三只持仓全部浮亏，中国卫通从盈利转亏
- 中国卫通本周跌9%，中兴通讯跌至-5.94%
- 南网能源深套-70.55%，永不补仓
- 数据来源：券商APP（2026-05-15）

"""

# Combine
full_md = section_515 + "\n" + section_513 + "\n" + old_md

with open("data/portfolio_doc_latest.md", "w", encoding="utf-8") as f:
    f.write(full_md)

sections = re.findall(r'^# 2026\.\d+\.\d+', full_md, re.MULTILINE)
print(f"✅ Sections: {sections}, {len(full_md)} chars")

# Push
result = subprocess.run(
    ["lark-cli", "docs", "+update", "--doc", DOC_TOKEN, "--mode", "overwrite",
     "--markdown", "@./data/portfolio_doc_latest.md"],
    capture_output=True, text=True, timeout=30
)
print(result.stdout[:300])
if result.returncode != 0:
    print("ERR:", result.stderr[:200])
