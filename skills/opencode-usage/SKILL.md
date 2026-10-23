---
name: opencode-usage
description: opencode 用量与计费工具：token 用量报表、缓存命中率、TPS、上下文占比、费用估算（人民币），以及实时仪表盘（--watch）。Use when 用户提到「用量」「token 统计」「计费」「费用」「成本」「缓存命中率」「命中率」「TPS」「上下文占比」「仪表盘」「watch」「prices.json」「导入计费」或要查看/估算 LLM 使用成本。
---

# opencode-usage

只读分析 opencode 本地库（`~/.local/share/opencode/opencode.db`，SQLite `mode=ro`），无网络调用、不写任何文件（`--sync-prices` 除外）。

## 入口

- 报表：`python <skill>/scripts/oc_usage.py [--days N] [--by day|model|session] [--model 子串]`
- 仪表盘：`python <skill>/scripts/oc_usage.py --watch [--interval 秒] [--session id前缀]`
  - Windows 用 `<skill>/scripts/usage_watch.bat` 弹独立窗口（默认置顶）；按 `q` 或 Ctrl+C 退出
  - 跟随最近活跃会话；`--session` 可钉住
- `<skill>` = 本 SKILL.md 所在目录。项目安装在 `.opencode/skills/opencode-usage/`，全局安装在 `~/.config/opencode/skills/opencode-usage/`。

## 展示口径（向用户解释时用）

- 五项 token 与「总」是**会话累计**；「上下文」是**最近一次调用的瞬时上下文占用**（与 opencode 面板「总 token」同口径）
- 命中率 = 缓存读 ÷ (缓存读 + 未命中输入)
- TPS = 最近 5 次调用的（推理+输出）÷ 生成耗时
- 费用单位人民币元，推理 token 按输出价计；**未配置价的模型不计费**，显示「缺省」

## 计费导入

单价表在 `<skill>/scripts/prices.json`（元/百万 token）。换模型时仿照示例加一段
`模型id前缀 → {miss, cache_read, cache_write, output}`；长前缀优先匹配。
可选：`<skill>/scripts/sync_prices.py` 从 models.dev 自动补缺——**独立联网脚本，
仅在用户明确同意后运行（脚本本身也会二次确认）**；不需要可直接删除，主工具不受影响。

## 安全边界

- 主工具只读数据库，不做 kill 进程、不写盘、**不发任何网络请求**
- `sync_prices.py` 是唯一的联网文件（独立、可删除），必须用户明确同意才运行
- 命令参数仅作脚本参数透传，不拼接其他命令
