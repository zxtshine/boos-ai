"""
面试问答Agent - LLM客户端模块
- Embedding: Ollama nomic-embed-text
- 出题: Ollama qwen2.5:14b
- 批改: DeepSeek API
"""

import httpx
import numpy as np
import json
import re
import os
import time
from datetime import datetime
from typing import List, Optional

# Ollama配置
OLLAMA_BASE = "http://localhost:11434"
EMBED_MODEL = "nomic-embed-text"
LLM_MODEL = "qwen2.5:14b"


# AI配置（每次调用时从SQLite设置读取）
def _load_ai_config():
    cfg = {
        "api_key": "",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "debug_llm_context": False,
    }
    try:
        import sys, os

        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from boss_state import get_setting, get_db

        get_db()
        key = get_setting("ai_api_key")
        if key:
            cfg["api_key"] = key
        url = get_setting("ai_base_url")
        if url:
            cfg["base_url"] = url
        model = get_setting("ai_model")
        if model:
            cfg["model"] = model
        cfg["debug_llm_context"] = get_setting("debug_llm_context", "false") == "true"
    except Exception:
        pass
    return cfg


def get_embedding(text: str) -> List[float]:
    """获取文本的embedding向量"""
    t0 = time.time()
    preview = text[:60].replace("\n", " ")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔄 正在调用 Ollama embedding 生成向量... (model={EMBED_MODEL}, text_len={len(text)}, preview=\"{preview}...\")")
    resp = httpx.post(
        f"{OLLAMA_BASE}/api/embed",
        json={"model": EMBED_MODEL, "input": text},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    elapsed = time.time() - t0
    print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ Ollama embedding 完成 (耗时: {elapsed*1000:.0f}ms)")
    return data["embeddings"][0]


def cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
    """计算余弦相似度"""
    a = np.array(vec_a, dtype=np.float32)
    b = np.array(vec_b, dtype=np.float32)
    if np.linalg.norm(a) == 0 or np.linalg.norm(b) == 0:
        return 0.0
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


def llm_chat_ollama(messages: list, system_prompt: Optional[str] = None, temperature: float = 0.7) -> str:
    """调用Ollama大模型（出题用）"""
    t0 = time.time()
    msg_count = len(messages) + (1 if system_prompt else 0)
    if system_prompt:
        messages = [{"role": "system", "content": system_prompt}] + messages

    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔄 正在调用 Ollama 大模型... (model={LLM_MODEL}, messages={msg_count}, temp={temperature})")
    print("─" * 60)
    for i, m in enumerate(messages):
        role = m["role"]
        content = m["content"]
        display = content if len(content) <= 3000 else content[:3000] + f"\n... [截断，原{len(content)}字符]"
        print(f"[LLM INPUT {i+1}/{msg_count}] {role}:")
        print(display)
        print()
    print("─" * 60)

    payload = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }

    resp = httpx.post(f"{OLLAMA_BASE}/api/chat", json=payload, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    elapsed = time.time() - t0
    result = data["message"]["content"]
    print(f"[LLM OUTPUT] ({len(result)} chars, {elapsed*1000:.0f}ms):")
    print(result[:2000] if len(result) > 2000 else result)
    if len(result) > 2000:
        print(f"... [截断，原{len(result)}字符]")
    print("─" * 60)
    return result


def llm_chat_deepseek(messages: list, system_prompt: Optional[str] = None, temperature: float = 0.3) -> str:
    """调用AI API（懒加载配置，每次从SQLite读取）"""
    t0 = time.time()
    cfg = _load_ai_config()
    if not cfg["api_key"]:
        raise RuntimeError("AI API Key未配置，请在设置页配置")

    if system_prompt:
        messages = [{"role": "system", "content": system_prompt}] + messages

    msg_count = len(messages)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔄 正在调用 AI API... (model={cfg['model']}, url={cfg['base_url']}, messages={msg_count}, temp={temperature})")
    print("─" * 60)
    for i, m in enumerate(messages):
        role = m["role"]
        content = m["content"]
        display = content if len(content) <= 3000 else content[:3000] + f"\n... [截断，原{len(content)}字符]"
        print(f"[LLM INPUT {i+1}/{msg_count}] {role}:")
        print(display)
        print()
    print("─" * 60)

    payload = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }

    resp = httpx.post(
        f"{cfg['base_url']}/chat/completions",
        json=payload,
        headers={
            "Authorization": f"Bearer {cfg['api_key']}",
            "Content-Type": "application/json",
        },
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    elapsed = time.time() - t0
    result = data["choices"][0]["message"]["content"]
    print(f"[LLM OUTPUT] ({len(result)} chars, {elapsed*1000:.0f}ms):")
    print(result[:2000] if len(result) > 2000 else result)
    if len(result) > 2000:
        print(f"... [截断，原{len(result)}字符]")
    print("─" * 60)
    return result


def parse_json_from_llm(text: str) -> Optional[dict]:
    """从LLM返回文本中提取JSON"""
    json_match = re.search(r"\{.*\}", text, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group())
        except json.JSONDecodeError:
            pass
    return None
