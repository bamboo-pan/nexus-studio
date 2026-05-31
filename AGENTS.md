<!-- TRELLIS:START -->
# Trellis Instructions

These instructions are for AI assistants working in this project.

This project is managed by Trellis. The working knowledge you need lives under `.trellis/`:

- `.trellis/workflow.md` — development phases, when to create tasks, skill routing
- `.trellis/spec/` — package- and layer-scoped coding guidelines (read before writing code in a given layer)
- `.trellis/workspace/` — per-developer journals and session traces
- `.trellis/tasks/` — active and archived tasks (PRDs, research, jsonl context)

If a Trellis command is available on your platform (e.g. `/trellis:finish-work`, `/trellis:continue`), prefer it over manual steps. Not every platform exposes every command.

If you're using Codex or another agent-capable tool, additional project-scoped helpers may live in:
- `.agents/skills/` — reusable Trellis skills
- `.codex/agents/` — optional custom subagents

Managed by Trellis. Edits outside this block are preserved; edits inside may be overwritten by a future `trellis update`.

<!-- TRELLIS:END -->

# 真实测试环境

1. 在wsl home目录下下新建临时目录实际测试
   1. \\wsl.localhost\Ubuntu-24.04\home\bamboo
2. 真实凭据
   1. \\wsl.localhost\Ubuntu-24.04\home\bamboo\nexus-studio\data\accounts
3. 除了文档更新类的改动，其他所有改动必须最终真实环境测试通过
   1. 真实测试必须包括API层面和前端UI实际使用层面的测试，不能替代省略
   2. 测试local studio的真实openai key在
      1. C:\Users\bamboo\Documents\github\key.txt
   3. 系统测试计划在
      1. SYSTEM_TEST_PLAN.md
