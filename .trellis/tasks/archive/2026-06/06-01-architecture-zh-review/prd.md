# Architecture Chinese Translation And Review

## Goal

将上一个 Provider Manager 架构任务新增到 `ARCHITECTURE.md` 的英文内容更新为中文，同时保持架构含义、边界和 Mermaid 图表达不变，并对更新后的文档做一次 review。

## What I Already Know

- 上一个任务是 `.trellis/tasks/archive/2026-05/05-31-provider-manager-architecture`。
- 上一个任务在 `ARCHITECTURE.md` 中新增了 Provider Manager / shared provider-model pool 目标架构段。
- 当前 `ARCHITECTURE.md` 前半部分已经主要是中文，新增段落为英文，文档语言不一致。
- 本任务是文档更新，不涉及后端实现、前端页面、API 合约或存储迁移。

## Requirements

- 将 `ARCHITECTURE.md` 中上一个任务新增的 Provider Manager 架构段翻译/改写为中文。
- 保留关键英文协议、产品和技术名词：Local Studio、Provider Manager、Google AI Studio、OpenAI Responses、OpenAI Chat Completions、Gemini、Claude Messages 等。
- 保持架构语义不变：Provider Manager 是控制面，shared runtime gateway 是数据面，Local Studio 是共享池消费者之一，Google AI Studio 基础模块保持独立可用。
- 更新 Mermaid 图中的可见标签为中文或中英混合表达，保持节点关系和 Mermaid 语法有效。
- 保留兼容协议表、路由策略、Local Studio 边界、Google AI Studio 基础模块约束、分阶段落地边界等内容。
- 完成文档 review，重点检查语义一致性、术语一致性、Mermaid 可读性、边界表达是否清晰。

## Acceptance Criteria

- [x] `ARCHITECTURE.md` 中上一个 Provider Manager 架构段不再大段使用英文说明。
- [x] Mermaid 图仍能表达相同架构关系，且没有明显语法破坏。
- [x] 控制面、数据面、协议入口、执行器、请求管理服务、路由策略等边界在中文版本中清晰可读。
- [x] review 后没有发现阻塞性文档问题；如有非阻塞建议，在最终说明中列出。
- [x] 文档任务不运行真实 WSL/API/UI 测试，原因是本次只修改架构文档。

## Definition of Done

- 文档更新完成。
- 执行轻量级文档验证或至少搜索确认英文残留范围可接受。
- 运行 Trellis check/review 流程。
- 提交本任务相关文档和 `.trellis/tasks/06-01-architecture-zh-review/` 任务资料。

## Technical Approach

采用保守翻译方式：只更新上一个任务新增的 Provider Manager 架构段，不重写前文已有中文架构；保留协议名、产品名和接口路径；对 Mermaid 节点 label 做中文化以提升整篇架构文档一致性。

## Decision (ADR-lite)

**Context**: `ARCHITECTURE.md` 已是中文文档，但上个任务新增段落为英文，影响阅读一致性。

**Decision**: 将新增段落整体中文化，同时保留稳定技术名词和协议名，避免翻译后失去行业识别度。

**Consequences**: 中文读者阅读成本降低；后续实现任务仍能通过保留的英文协议名准确定位外部 API 语义。

## Out of Scope

- 不修改后端代码、前端代码、API 路由或数据模型。
- 不新增 Provider Manager 功能实现。
- 不调整上一个架构任务已经定义的架构边界和落地阶段。
- 不做真实 API/UI 测试；本任务属于文档更新。

## Technical Notes

- 当前架构文档：`ARCHITECTURE.md`。
- 上一任务 PRD：`.trellis/tasks/archive/2026-05/05-31-provider-manager-architecture/prd.md`。
- 本任务 research：`research/previous-architecture-context.md`。
- Spec update decision：本任务只做架构文档中文化和 review，不新增或改变可执行 API、存储、跨层契约、环境变量、错误矩阵或实现约定，因此不更新 `.trellis/spec/`。