---
description: 统计 token 用量、缓存命中率、TPS 与费用估算（读 opencode 本地库）
---

找到 opencode-usage skill 的 `scripts/oc_usage.py`（先在项目 `.opencode/skills/opencode-usage/` 找，找不到再去 `~/.config/opencode/skills/opencode-usage/`），用 bash 工具执行：

```
python <该路径>/oc_usage.py $ARGUMENTS
```

`$ARGUMENTS` 仅作为脚本参数透传（如 `--days 7 --by model`），不得拼接其他命令。把输出原样展示给用户，不要转述、不要省略表格行。若命令报错，原样展示错误信息。
