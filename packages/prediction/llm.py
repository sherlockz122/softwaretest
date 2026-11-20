"""大模型评审：把一次提交的说明和改动概况交给外部大模型，拿一个"是否会引入缺陷"的判断。

和本地模型是两套东西：本地模型吃 14 项数值特征，这里吃的是自然语言。
只发送提交说明、改动的文件路径和增删行数，不发送源码内容。
"""

import json
import time
import urllib.error
import urllib.request

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.persistence.models import FileChange, GitCommit
from packages.prediction.service import PredictionError

FILES_SENT = 30
MESSAGE_LIMIT = 2000
SYSTEM_PROMPT = (
    "你是一名代码评审助手。你会收到一次 Git 提交的说明和改动概况（改了哪些文件、增删多少行）。"
    "看不到源码，所以请根据改动规模、涉及范围、说明是否讲清楚意图来估计风险。"
    "参考经验：改动越大、涉及核心逻辑越多、说明越含糊、删除代码却没有说明原因，风险越高；"
    "只加注释、只改文档、新增独立小功能，风险较低。"
    "只输出一个 JSON 对象，不要输出其它内容，格式为："
    '{"risk":0到1的小数,"verdict":"likely_defect"|"likely_clean"|"uncertain","reason":"不超过80字的中文理由"}'
)
VERDICTS = {"likely_defect", "likely_clean", "uncertain"}


def _commit_context(db, repository_id, sha):
    commit = db.scalar(
        select(GitCommit).where(
            GitCommit.repository_id == repository_id, GitCommit.sha == sha
        )
    )
    if commit is None:
        raise PredictionError("PRED_COMMIT_NOT_FOUND", 404)
    changes = db.scalars(
        select(FileChange)
        .where(FileChange.commit_id == commit.id)
        .order_by(FileChange.ordinal)
        .limit(FILES_SENT)
    ).all()
    files = [
        {
            "path": change.new_path or change.old_path,
            "insertions": change.insertions,
            "deletions": change.deletions,
        }
        for change in changes
        if change.new_path or change.old_path
    ]
    return commit, files


def _prompt(commit, files):
    lines = [
        f"提交说明：{(commit.message or '（空）')[:MESSAGE_LIMIT]}",
        f"改动文件数：{len(files)}",
    ]
    for item in files:
        lines.append(
            f"- {item['path']}（新增 {item['insertions']} 行 / 删除 {item['deletions']} 行）"
        )
    return "\n".join(lines)


def _parse(content):
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return {"verdict": "uncertain", "confidence": None, "reason": text[:200] or "模型没有返回可解析的结果"}
    risk = data.get("risk")
    if not isinstance(risk, (int, float)) or not 0 <= float(risk) <= 1:
        risk = None
    verdict = data.get("verdict")
    if verdict not in VERDICTS:
        # 没给结论时按风险分换算，保证两边能放在一张表里比较。
        verdict = (
            "uncertain"
            if risk is None
            else "likely_defect"
            if risk >= 0.6
            else "likely_clean"
            if risk <= 0.4
            else "uncertain"
        )
    reason = data.get("reason")
    return {
        "risk": round(float(risk), 4) if risk is not None else None,
        "verdict": verdict,
        "reason": str(reason)[:300] if reason else "",
    }


def providers(settings):
    """可选的大模型入口；没配好的条目也会列出来，只是标记成未配置。"""
    deepseek_key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else ""
    custom_key = (
        settings.custom_llm_api_key.get_secret_value()
        if settings.custom_llm_api_key
        else ""
    )
    return [
        {
            "id": "deepseek",
            "label": "DeepSeek",
            "model": settings.llm_model,
            "configured": bool(deepseek_key),
            "needs_key": True,
        },
        {
            "id": "ollama",
            "label": "本地 Ollama",
            "model": settings.ollama_model,
            "configured": bool(settings.ollama_base_url),
            "needs_key": False,
        },
        {
            "id": "custom",
            "label": "自定义（OpenAI 兼容）",
            "model": settings.custom_llm_model,
            "configured": bool(settings.custom_llm_base_url and settings.custom_llm_model),
            "needs_key": False,
        },
    ]


def _target(settings, provider):
    """按入口取出 base_url、模型和 key；key 可以为空（本地 Ollama）。"""
    if provider == "ollama":
        return settings.ollama_base_url, settings.ollama_model, ""
    if provider == "custom":
        key = (
            settings.custom_llm_api_key.get_secret_value()
            if settings.custom_llm_api_key
            else ""
        )
        return settings.custom_llm_base_url, settings.custom_llm_model, key
    key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else ""
    return settings.llm_base_url, settings.llm_model, key


def review(settings, engine, repository_id, sha, provider="deepseek"):
    """调用外部大模型评审一次提交；入口没配置时返回 503。"""
    entry = next((item for item in providers(settings) if item["id"] == provider), None)
    if entry is None:
        raise PredictionError("LLM_PROVIDER_UNKNOWN", 404)
    if not entry["configured"]:
        raise PredictionError("LLM_NOT_CONFIGURED", 503)
    base_url, model, api_key = _target(settings, provider)
    with Session(engine) as db:
        commit, files = _commit_context(db, repository_id, sha)
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _prompt(commit, files)},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        },
        ensure_ascii=False,
    ).encode()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=body,
        headers=headers,
        method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=settings.llm_timeout_seconds) as reply:
            payload = json.loads(reply.read().decode())
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise PredictionError("LLM_UNAVAILABLE", 503) from None
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise PredictionError("LLM_UNAVAILABLE", 503) from None
    result = _parse(content)
    result.update(
        {
            "model": model,
            "provider": provider,
            "commit": sha,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "files_sent": len(files),
        }
    )
    return result
