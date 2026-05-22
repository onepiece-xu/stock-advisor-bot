#!/usr/bin/env python3
"""Build full markdown with new May 15 section prepended, then push to Feishu."""
import subprocess, re, json

DOC_TOKEN = "DXRDdRGRJohquex19VucUqh0nVd"

# 1. Fetch current doc as JSON
result = subprocess.run(
    ["lark-cli", "docs", "+fetch", "--doc", DOC_TOKEN],
    capture_output=True, text=True, timeout=15
)
data = json.loads(result.stdout)
md_raw = data.get("markdown", data.get("data", {}).get("markdown", ""))

# 2. Strip line numbers
md_clean = re.sub(r'^\s*\d+\|', '', md_raw, flags=re.MULTILINE)
md_clean = md_clean.replace('\\u003c', '<').replace('\\u003e', '>').replace('\\"', '"')

# 3. New May 15 section
new_section = """# 2026.5.15

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
    <lark-td>
      股票
    </lark-td>
    <lark-td>
      市值 {align="right"}
    </lark-td>
    <lark-td>
      盈亏金额 {align="right"}
    </lark-td>
    <lark-td>
      盈亏比例 {align="right"}
    </lark-td>
    <lark-td>
      持仓/可用 {align="right"}
    </lark-td>
    <lark-td>
      成本价 {align="right"}
    </lark-td>
    <lark-td>
      现价 {align="right"}
    </lark-td>
  </lark-tr>
  <lark-tr>
    <lark-td>
      中国卫通
    </lark-td>
    <lark-td>
      7018.00 {align="right"}
    </lark-td>
    <lark-td>
      -338.46 {align="right"}
    </lark-td>
    <lark-td>
      -4.60% {align="right"}
    </lark-td>
    <lark-td>
      200 / 200 {align="right"}
    </lark-td>
    <lark-td>
      36.783 {align="right"}
    </lark-td>
    <lark-td>
      35.090 {align="right"}
    </lark-td>
  </lark-tr>
  <lark-tr>
    <lark-td>
      中兴通讯
    </lark-td>
    <lark-td>
      3671.00 {align="right"}
    </lark-td>
    <lark-td>
      -231.84 {align="right"}
    </lark-td>
    <lark-td>
      -5.94% {align="right"}
    </lark-td>
    <lark-td>
      100 / 100 {align="right"}
    </lark-td>
    <lark-td>
      39.029 {align="right"}
    </lark-td>
    <lark-td>
      36.710 {align="right"}
    </lark-td>
  </lark-tr>
  <lark-tr>
    <lark-td>
      南网能源
    </lark-td>
    <lark-td>
      743.00 {align="right"}
    </lark-td>
    <lark-td>
      -1779.11 {align="right"}
    </lark-td>
    <lark-td>
      -70.55% {align="right"}
    </lark-td>
    <lark-td>
      100 / 100 {align="right"}
    </lark-td>
    <lark-td>
      25.230 {align="right"}
    </lark-td>
    <lark-td>
      7.430 {align="right"}
    </lark-td>
  </lark-tr>
</lark-table>

## 备注

- ⚠️ 三只持仓全部浮亏，中国卫通从盈利转为亏损
- 中国卫通：本周跌幅9%，从+4.3%跌到-4.6%
- 中兴通讯：跌幅加剧至-5.94%
- 南网能源：深套-70.55%，永不补仓
- 数据来源：券商APP截图（Hermes 于 2026-05-15 手动同步）

"""

# 4. Prepend new section
full_md = new_section + "\n" + md_clean.strip()

# 5. Write to file
with open("data/portfolio_doc_latest.md", "w", encoding="utf-8") as f:
    f.write(full_md)

print(f"✅ Written {len(full_md)} chars")
print(f"Lines: {len(full_md.split(chr(10)))}")

# 6. Push to Feishu
result = subprocess.run(
    ["lark-cli", "docs", "+update", "--doc", DOC_TOKEN, "--mode", "overwrite", 
     "--markdown", "@./data/portfolio_doc_latest.md"],
    capture_output=True, text=True, timeout=15
)
print(f"PUSH: {result.stdout[:200]}")
if result.returncode != 0:
    print(f"ERR: {result.stderr[:200]}")
