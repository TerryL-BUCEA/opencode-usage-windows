---
description: 新窗口启动实时用量仪表盘（token/命中率/TPS/费用）
---

找到 opencode-usage skill 的 `scripts/usage_watch.bat`（先在项目 `.opencode/skills/opencode-usage/` 找，找不到再去 `~/.config/opencode/skills/opencode-usage/`）。

Windows 用 bash 工具执行：

```
Start-Process -FilePath "<该路径>/usage_watch.bat"
```

非 Windows 或无 bat 时，指导用户在新终端运行 `python <该路径>/oc_usage.py --watch`。

执行后用一句话告知用户：仪表盘已在新窗口打开，按 q 退出。不要在当前会话前台运行 `--watch`，不要重复启动多个实例。`$ARGUMENTS` 仅作脚本参数透传。
