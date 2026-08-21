---
name: skill-gardener
description: "Audit Hermes skills: dedupe, prune, mine new candidates."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [windows, macos, linux]
metadata:
  hermes:
    tags: [Skills, Maintenance, Deduplication, Cleanup, Gardening]
    category: productivity
    related_skills: [session-librarian, weekly-review-planning, hermes-agent-skill-authoring]
---

# Skill Gardener（技能园艺师）

自动审计 Hermes 技能库健康：去重、找过期、从会话里挖新技能候选。
**只读分析，绝不自动改任何 skill / memory。**

## When to Use
- 会话开始时，如果注入上下文里出现「技能库有未处理的巡检待办」提醒（来自 skill-gardener-reminder 插件的 pre_llm_call 注入），先读 `.skill-gardener/PENDING.md` 与 `inbox/`，主动列给用户确认
- 用户问「技能库乱没乱 / 帮我整理 skill / 哪些 skill 没用过」
- 定期（cron）巡检技能库
- 想从最近会话挖「值得沉淀成 skill」的流程

## 核心原则（硬约束）
1. 脚本只读，产出报告；任何 create/patch/delete 都必须先在会话里向用户确认。
2. 草稿先进 `$HERMES_HOME/.skill-gardener/inbox/`，用户点头才 `skill_manage` 落库。
3. 溯源：新 skill 的 frontmatter 记 `source_session` + 生成时间。
4. 双轨分流：稳定偏好 → memory，可复用流程 → skill，别混。

## 步骤
1. 跑报告：`python scripts/gardener.py`（自动探测 HERMES_HOME；可用 `--home`、`--stale-days`、`--top`、`--sediment-kw "a,b,c"` 覆盖；`--sediment-kw` 用于给非中文会话换一套沉淀关键词）
2. 读 `$HERMES_HOME/.skill-gardener/report.md`（脚本同时写文件 + 打 stdout）
3. 按报告「行动建议」逐项判断，结论列给用户确认。
4. 用户确认后再动手。

## Watchdog（每日断粮检查，no_agent cron `skill-gardener-watchdog`）
`scripts/watchdog.py` 每天 9:55 由 cron 直跑（无 LLM 消耗）：
- 检查中转站 `/v1/models` 是否还有 cron 依赖的模型（防 404 断粮）
- 检查巡检 job `last_status=error` / 上次运行超 8 天（防跳周静默）
- **模型检查的前提**：断粮检查假设你用的是 OpenAI 兼容中转站（暴露 `GET /v1/models`，且 cron 依赖的模型在该清单里）。若用大模型供应商官方 API（OpenAI/Anthropic/Gemini/xAI）或自建非标准 gateway，此项会误报「不可达/断粮」——设环境变量 `SKILL_GARDENER_SKIP_MODEL_CHECK=1` 可跳过它，job 健康与超期检查照常。
- 巡检 job 名默认 `skill-gardener-weekly`，可用环境变量 `SKILL_GARDENER_JOB_NAME` 覆盖（改名后别忘同步）
- 一切正常 → 零输出（SILENT）；异常 → stdout 打报警（存 watchdog cron 输出目录，状态落盘 `.skill-gardener/watchdog_state.json`）
- 会话中若用户说巡检没跑/模型 404，先看 `watchdog_state.json` 和该目录最新输出。

## Cron Backstop（开机窗口兜底）
桌面 APP 关闭时其后端 cron ticker 停转。兜底：Windows 计划任务 `HermesCronBackstop`（SYSTEM 账户，每天 9:50）跑 `scripts/cron-backstop.ps1` → `hermes cron tick`。
- tick 带 `cron/.tick.lock` 文件锁，与桌面 ticker 互斥，不会双跑
- 时序：9:50 backstop tick → 9:55 watchdog → 10:00 周巡检，逐级保险
- 管理：`schtasks /query /tn HermesCronBackstop`（查）、`/run`（手动触发）、`/delete /f`（删除）
- 陷阱：tick 只触发**到期** job（catch-up 窗口半周期上限 2h）；改巡检时间需同步改 backstop 和 watchdog 的时刻，保持 5 分钟梯队

## 硬送达链路（提醒怎么到你眼前）
`plugins/skill-gardener-reminder/`（用户级 Python 插件，桌面后端启动时加载）在每次 LLM 调用前检查 `.skill-gardener/PENDING.md`：
- 无待办 → 零注入（不碰 prompt cache）
- 有待办 → 固定文本注入当前轮 user message
- 待办超 7 天未处理 → 换「已过期」文案，提示确认后删除（防永久噪声）

## 报告各节含义
| 节 | 含义 | 注意 |
|---|---|---|
| §1 热点技能 | 最近被 skill_view 加载的 | 反映真实使用 |
| §2 长期未用 | 曾加载但 ≥N 天没碰 | 数据不足时为空/无意义 |
| §3 疑似重复 | description 相似度 ≥0.62 | 同族工具（claude-code/codex/opencode）属刻意并列，不算重复 |
| §4 从未加载 | 磁盘有但无加载记录 | 多为预装/领域不相关 |
| §5 修改历史 | skill_manage 记录 | 观察技能演化 |
| §6 沉淀候选 | 「记住/下次/别忘了」线索 | 对照 MEMORY.md/USER.md 去重 |
| §7 Memory 概览 | memory 用量 | 判断该进 memory 还是 skill |
| §8 会话概览 | 会话 + 成本 | 背景 |

## 即时沉淀（会话中，不等周扫）
Hermes 原生每 ~15 轮会 nudge「要不要存 skill」，别浪费，趁热沉淀，别拖到每周 cron 翻账本。

触发时机（任一即可主动 offer）：
- creation nudge 触发
- 会话里刚拆完坑 / 摸索出流程 / 多次重试才搞定（见 §6b 难任务信号）
- 用户说「记住这个」「以后都这样」

即时沉淀三步：
1. 双轨判断：稳定偏好 → memory；可复用流程 → skill。
2. 生成草稿到 `$HERMES_HOME/.skill-gardener/inbox/`（用下方模板），不自动发布。
3. 问用户「要不要存」，点头才 `skill_manage` 落库。

## 沉淀草稿模板
写入 inbox 的候选 SKILL.md，frontmatter 至少含：
```yaml
name: <slug>
description: <触发条件 + 一句话行为，前 57 字符要自包含>
source_session: <session_id>
created_at: <ISO>
```

## 陷阱
- §3 是字符串相似，会误报「同族但刻意分开」的技能；只删确属冗余的。
- §6 很多「记住」类信号已被 memory 接住，别重复存成 skill。
- 数据只有几天时，§2 别急着归档。
- 报告顶部有「⚠️ Schema 自检」区块：显示 ❌ 缺列 = Hermes 升级改了 state.db 结构，对应数据 section 已被跳过，报告不可信（别把空表当「一切正常」）。
- Windows 用 `python`（3.11 venv，纯标准库，无第三方依赖）。
