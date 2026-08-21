#!/usr/bin/env python
"""skill-gardener watchdog: 模型断粮 + cron 健康检查。

设计为 no_agent cron 的 script：每天跑一次，stdout 即送达内容。
- 一切正常 → 输出空（SILENT，用户零打扰）
- 异常 → 输出报警文本（送达给用户）

检查项:
  1. 中转站 /v1/models 是否还挂着 cron job pin 的模型（防 404 断粮）
  2. skill-gardener-weekly 的 last_status 是否为 error
  3. 该 job 的 last_run_at 是否超过 8 天（防 fast-forward 跳周静默）
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

def _detect_home():
    """HERMES_HOME 四级回退（与 gardener.py / README 承诺一致）。

    仅试 `~/.hermes` 不够——Windows 上 Hermes 常装到 %LOCALAPPDATA%\\hermes，
    漏掉这两级会让 watchdog 读错 HOME，报一堆假警（config 解析失败 / jobs.json 读不到）。
    """
    cands = []
    if os.environ.get("HERMES_HOME"):
        cands.append(os.environ["HERMES_HOME"])
    cands += [
        str(Path.home() / ".hermes"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "hermes"),
        os.path.join(os.environ.get("APPDATA", ""), "hermes"),
    ]
    for c in cands:
        if c and (Path(c) / "config.yaml").is_file():
            return c
    return cands[0] if cands else str(Path.home() / ".hermes")


HOME = Path(_detect_home())
CONFIG = HOME / "config.yaml"
JOBS = HOME / "cron" / "jobs.json"
# 巡检 cron job 的 name（cron 定义里的 name）。可用环境变量 SKILL_GARDENER_JOB_NAME 覆盖，
# 避免硬编码耦合到某个具体 job 名。
JOB_NAME = os.environ.get("SKILL_GARDENER_JOB_NAME", "skill-gardener-weekly")
MAX_STALE_DAYS = 8  # 周任务 + 1 天容差

# 断粮检查（检查 1）只适用于 OpenAI 兼容中转站（暴露 GET /v1/models）。
# 用官方大模型供应商 API（OpenAI/Anthropic/Gemini/xAI 等）或自建非标准 gateway 时，
# 该检查会误报「不可达/断粮」——设 SKILL_GARDENER_SKIP_MODEL_CHECK=1 可跳过它，
# 检查 2（job 健康）与检查 3（超期）不受影响。
SKIP_MODEL_CHECK = os.environ.get("SKILL_GARDENER_SKIP_MODEL_CHECK", "") not in ("", "0", "false", "False")

STALE_FILE = HOME / ".skill-gardener" / "watchdog_state.json"


def _read_yaml_simple(path):
    """不引 PyYAML 的最小解析：只取嵌套 dict/list 的 key: value 行。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    model, provider, base_url, key_env = None, None, None, None
    in_model_block = False
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            in_model_block = stripped.startswith("model:")
            if stripped.startswith("providers:"):
                in_model_block = False
            continue
        if in_model_block and indent == 2:
            if stripped.startswith("default:"):
                model = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("provider:"):
                provider = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("base_url:"):
                base_url = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("key_env:"):
                key_env = stripped.split(":", 1)[1].strip()
    return model, provider, base_url, key_env


def _models_alive(base_url, key_env):
    """GET /v1/models，返回 (ok, 可用模型列表或错误信息)。"""
    key = os.environ.get(key_env or "", "")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/models",
        headers={"Authorization": f"Bearer {key}"} if key else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        ids = [m.get("id", "") for m in data.get("data", [])]
        return True, ids
    except Exception as e:
        return False, str(e)


def _cron_job_health():
    """读 jobs.json 里目标 job 的健康状态（按 name 匹配，不依赖具体 id）。"""
    try:
        jobs = json.loads(JOBS.read_text(encoding="utf-8"))
    except Exception as e:
        return None, f"jobs.json 读取失败: {e}"
    job = None
    candidates = jobs if isinstance(jobs, list) else jobs.get("jobs", [])
    if isinstance(candidates, dict):
        for j in candidates.values():
            if isinstance(j, dict) and j.get("name") == JOB_NAME:
                job = j
                break
    else:
        for j in candidates:
            if isinstance(j, dict) and j.get("name") == JOB_NAME:
                job = j
                break
    if not job:
        return None, f"jobs.json 里找不到名为 '{JOB_NAME}' 的巡检 job"
    return job, None


def main():
    problems = []

    # ── 检查 1: 模型是否还在中转站清单里（仅 OpenAI 兼容中转站适用）──
    if not SKIP_MODEL_CHECK:
        model, provider, base_url, key_env = _read_yaml_simple(CONFIG)
        if base_url and model:
            ok, info = _models_alive(base_url, key_env)
            if not ok:
                problems.append(f"中转站 /v1/models 不可达: {info}")
            elif model not in info:
                provider_hint = provider or "<你的 provider 名>"
                problems.append(
                    f"断粮报警: cron 依赖的模型 '{model}' 已不在中转站清单里。"
                    f"现有: {', '.join(info)}。请跑: hermes cron edit <巡检 job> "
                    f"--provider {provider_hint} --model <清单里的模型>"
                )
        else:
            problems.append(f"config.yaml 解析不出 model/base_url (got model={model})")

    # ── 检查 2: job last_status ──
    job, err = _cron_job_health()
    if err:
        problems.append(f"cron 健康检查失败: {err}")
    else:
        status = job.get("last_status")
        if status == "error":
            problems.append(
                f"cron job {JOB_NAME} 上次运行失败 (last_status=error)。"
                f"多半是模型 404，见上一条；修复后 hermes cron run 手动补跑"
            )
        # ── 检查 3: 是否被 fast-forward 跳周 ──
        last_run = job.get("last_run_at")
        if last_run:
            try:
                lr = datetime.fromisoformat(last_run)
                if lr.tzinfo is None:
                    lr = lr.replace(tzinfo=datetime.now().astimezone().tzinfo)
                if datetime.now(lr.tzinfo) - lr > timedelta(days=MAX_STALE_DAYS):
                    problems.append(
                        f"巡检超期: {JOB_NAME} 上次成功运行在 {last_run}，"
                        f"已超过 {MAX_STALE_DAYS} 天（周六 APP 可能没开导致跳周）。"
                        f"建议 hermes cron run {JOB_NAME} 补跑"
                    )
            except ValueError:
                pass

    # 状态落盘（供人查，不影响逻辑）
    try:
        STALE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STALE_FILE.write_text(
            json.dumps(
                {"checked_at": datetime.now().isoformat(timespec="seconds"),
                 "problems": problems},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass

    if problems:
        print("⚠️ skill-gardener watchdog 报警:")
        for p in problems:
            print(f"  - {p}")
        sys.exit(0)  # 报警文本就是送达内容，不靠 exit code
    # 一切正常: 输出空 = SILENT
    sys.exit(0)


if __name__ == "__main__":
    main()
