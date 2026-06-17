#!/usr/bin/env python3
"""RAG 检索链路评测 — 召回率 / 命中率 / MRR + 多策略对比 + 消融实验

用法:
    cd interview && python benchmark_rag.py                  # 默认100条，原问题搜自己
    cd interview && python benchmark_rag.py --paraphrase     # 口语化改写后搜（真实检索场景）
    cd interview && python benchmark_rag.py --all            # 全部QA对
    cd interview && python benchmark_rag.py --size 200       # 指定条数

输出:
    - 表格：Recall@1 / @3 / @5, MRR, P50 / P95 时延
    - V2 vs V3 提升百分比
"""

import json
import time
import sys
import os
import argparse
from datetime import datetime
from typing import Dict, List, Tuple
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(__file__))

from fast_qa import (
    _retrieve_fulltext,
    _retrieve_keyword,
    _retrieve_semantic,
    rerank_fusion,
    rewrite_query,
    domain_fulltext_search,
    domain_semantic_search,
    domain_exact_match,
    SEMANTIC_MATCH_THRESHOLD,
)
from llm_client import llm_chat_deepseek
from mysql_config import get_conn


# ═══════════════════════════════════════
#  测试集加载
# ═══════════════════════════════════════

def load_test_set(sample_size: int = None) -> List[Dict]:
    """从 DB 加载 QA 对作为标注测试集：原问题=query，自身 ID=正确答案"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) as cnt FROM interview_qa_pairs "
                "WHERE embedding IS NOT NULL AND embedding != ''"
            )
            total = cur.fetchone()["cnt"]
        if sample_size and sample_size < total:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, category, question FROM interview_qa_pairs "
                    "WHERE embedding IS NOT NULL AND embedding != '' "
                    "ORDER BY RAND() LIMIT %s",
                    (sample_size,),
                )
                rows = [dict(r) for r in cur.fetchall()]
        else:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, category, question FROM interview_qa_pairs "
                    "WHERE embedding IS NOT NULL AND embedding != ''"
                )
                rows = [dict(r) for r in cur.fetchall()]
        return rows
    finally:
        conn.close()


# ═══════════════════════════════════════
#  口语化改写测试集（真实检索场景）
# ═══════════════════════════════════════

_PARAPHRASE_CACHE_FILE = os.path.join(os.path.dirname(__file__), ".benchmark_paraphrase_cache.json")


def _load_paraphrase_cache() -> Dict[str, str]:
    """加载已缓存的改写结果 {original_question: paraphrased}"""
    if os.path.exists(_PARAPHRASE_CACHE_FILE):
        try:
            with open(_PARAPHRASE_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_paraphrase_cache(cache: Dict[str, str]):
    with open(_PARAPHRASE_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def generate_paraphrased_test_set(test_set: List[Dict]) -> List[Dict]:
    """用 DeepSeek 将每条原问题改写为口语化提问，构建硬测试集。

    每条原问题生成 1 个口语化变体，正确答案 ID 不变。
    改写结果缓存到本地文件，重复跑不消耗 API。
    """
    cache = _load_paraphrase_cache()
    new_set = []
    pending = []

    for item in test_set:
        q = item["question"]
        if q in cache:
            new_set.append({"question": cache[q], "id": item["id"], "category": item.get("category", "")})
        else:
            pending.append(item)

    if not pending:
        print(f"   (全部 {len(new_set)} 条命中缓存)")
        return new_set

    print(f"   缓存命中 {len(test_set) - len(pending)}, 需改写 {len(pending)} 条...")
    for i, item in enumerate(pending, 1):
        q = item["question"]
        try:
            prompt = (
                "把下面这道面试题改写成一个求职者的口语化提问（15-35字），"
                "用日常闲聊的语气，保留原意但换一种问法，不要术语堆砌：\n"
                f"原题：{q}\n"
                "只输出改写后的问题，不要任何解释。"
            )
            paraphrased = llm_chat_deepseek(
                [{"role": "user", "content": prompt}], temperature=0.8
            )
            paraphrased = paraphrased.strip().strip('"').strip("'").strip()
            if len(paraphrased) < 5 or len(paraphrased) > 80:
                paraphrased = q  # 改写失败时保留原题
            cache[q] = paraphrased
            _save_paraphrase_cache(cache)
        except Exception as e:
            print(f"   ⚠️ 第{i}条改写失败: {e}，使用原题")
            cache[q] = q
            _save_paraphrase_cache(cache)
            paraphrased = q

        new_set.append({"question": paraphrased, "id": item["id"], "category": item.get("category", "")})
        if i % 10 == 0:
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"   [{ts}] 改写进度: {i}/{len(pending)}")

    print(f"   改写完成，测试集共 {len(new_set)} 条")
    return new_set


# ═══════════════════════════════════════
#  评测指标
# ═══════════════════════════════════════

def compute_metrics(
    results: List[Dict], correct_id: int, k_values: Tuple[int, ...] = (1, 3, 5)
) -> Dict:
    """计算单条 query 的 hit@k 和 reciprocal rank"""
    result_ids = [r["id"] for r in results]
    metrics = {}
    for k in k_values:
        metrics[f"hit@{k}"] = 1 if correct_id in result_ids[:k] else 0
    try:
        rank = result_ids.index(correct_id) + 1
        metrics["rr"] = 1.0 / rank
    except ValueError:
        metrics["rr"] = 0.0
    return metrics


def aggregate(metrics_list: List[Dict], latencies: List[float]) -> Dict:
    """汇总多条 query 的指标"""
    total = len(metrics_list)
    agg = defaultdict(float)
    for m in metrics_list:
        for k, v in m.items():
            agg[k] += v
    sorted_lat = sorted(latencies)
    return {
        "total": total,
        "recall_at_1": agg["hit@1"] / total,
        "recall_at_3": agg["hit@3"] / total,
        "recall_at_5": agg["hit@5"] / total,
        "mrr": agg["rr"] / total,
        "p50_ms": sorted_lat[len(sorted_lat) // 2],
        "p95_ms": sorted_lat[min(int(len(sorted_lat) * 0.95), len(sorted_lat) - 1)],
        "avg_ms": sum(latencies) / total,
    }


# ═══════════════════════════════════════
#  检索策略
# ═══════════════════════════════════════

def _run_parallel_recall(query: str, search_query: str, category: str, limit: int = 10):
    """三路并行召回"""
    recall = {}
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {
            ex.submit(_retrieve_fulltext, search_query, category, limit): "fulltext",
            ex.submit(_retrieve_keyword, search_query, category, limit): "keyword",
            ex.submit(_retrieve_semantic, query, category, limit): "semantic",
        }
        for f in as_completed(futures):
            try:
                recall[futures[f]] = f.result()
            except Exception:
                recall[futures[f]] = []
    return recall


class Strategy:
    """检索策略包装器"""

    def __init__(self, name: str, fn):
        self.name = name
        self.fn = fn

    def run(self, test_set: List[Dict]) -> Dict:
        metrics_list, latencies = [], []
        for item in test_set:
            query = item["question"]
            correct_id = item["id"]
            t0 = time.perf_counter()
            try:
                results = self.fn(query, item.get("category"))
            except Exception as e:
                print(f"    ⚠️ {self.name} 异常 (query={query[:30]}): {e}")
                results = []
            elapsed = (time.perf_counter() - t0) * 1000
            latencies.append(elapsed)
            metrics_list.append(compute_metrics(results, correct_id))
        agg = aggregate(metrics_list, latencies)
        agg["strategy"] = self.name
        return agg


# ---- 策略函数 ----

def s_fulltext(query, category):
    return _retrieve_fulltext(query, category, limit=10)


def s_keyword(query, category):
    return _retrieve_keyword(query, category, limit=10)


def s_semantic(query, category):
    return _retrieve_semantic(query, category, limit=10, threshold=0.5)


def s_fusion_v3(query, category):
    rewritten = rewrite_query(query)
    search_q = rewritten if rewritten != query else query
    recall = _run_parallel_recall(query, search_q, category)
    return rerank_fusion(recall, query)


def s_fusion_no_rewrite(query, category):
    recall = _run_parallel_recall(query, query, category)
    return rerank_fusion(recall, query)


def s_v2_baseline(query, category):
    """V2 基线：精确匹配 → 全文检索(含内置回退) → 语义检索
    不含缓存、预置回答、DeepSeek 兜底（公平对比检索层质量）"""
    # L1: 精确匹配
    exact = domain_exact_match(query, category)
    if exact:
        return [exact]
    # L2: 全文检索（内部回退 FULLTEXT → keyword LIKE → broad LIKE）
    results = domain_fulltext_search(query, category, limit=10)
    if results:
        return results
    # L2 fallback: 语义检索
    return domain_semantic_search(query, category, limit=10, threshold=SEMANTIC_MATCH_THRESHOLD)


# ═══════════════════════════════════════
#  Main
# ═══════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="RAG 检索链路评测")
    parser.add_argument("--size", type=int, default=100, help="测试集大小 (默认100)")
    parser.add_argument("--all", action="store_true", help="使用全部QA对")
    parser.add_argument(
        "--paraphrase", action="store_true",
        help="口语化改写测试query（真实检索场景，需要DeepSeek API）",
    )
    args = parser.parse_args()

    print("=" * 72)
    print("  RAG 检索链路评测 — 多策略召回率 · 命中率 · MRR 对比")
    print("=" * 72)

    # 加载测试集
    sample = None if args.all else args.size
    test_set = load_test_set(sample)
    if not test_set:
        print("❌ 测试集为空，请检查 MySQL 中 interview_qa_pairs 表是否有 embedding 数据")
        sys.exit(1)
    print(f"\n📦 测试集: {len(test_set)} 条")
    print(f"   类别分布: {_cat_distribution(test_set)}")

    # 口语化改写
    if args.paraphrase:
        print("\n🔄 生成口语化测试query（LLM改写）...")
        test_set = generate_paraphrased_test_set(test_set)
        print(f"   模式: 口语化查询（真实检索场景）")
    else:
        print(f"   模式: 原文查询（自匹配，仅验证功能）")
        print(f"   提示: 加 --paraphrase 获得真实检索对比数据")

    # 定义策略
    strategies = [
        Strategy("V2基线(串行)", s_v2_baseline),
        Strategy("仅全文检索", s_fulltext),
        Strategy("仅关键词", s_keyword),
        Strategy("仅语义", s_semantic),
        Strategy("V3融合(无改写)", s_fusion_no_rewrite),
        Strategy("V3融合(完整)", s_fusion_v3),
    ]

    print(f"\n🔬 评测 {len(strategies)} 个策略...")
    results = []
    for st in strategies:
        print(f"   ▶ {st.name} ...")
        results.append(st.run(test_set))

    # 表格输出
    print(f"\n{'策略':<20} {'R@1':>8} {'R@3':>8} {'R@5':>8} {'MRR':>8} {'P50':>8} {'P95':>8}")
    print("-" * 72)
    for r in results:
        print(
            f"{r['strategy']:<20} "
            f"{r['recall_at_1']:>7.1%} "
            f"{r['recall_at_3']:>7.1%} "
            f"{r['recall_at_5']:>7.1%} "
            f"{r['mrr']:>7.4f} "
            f"{r['p50_ms']:>6.0f}ms "
            f"{r['p95_ms']:>6.0f}ms"
        )

    # 对比结论
    baseline = results[0]
    best = max(results, key=lambda r: r["mrr"])
    mrr_gain = (
        (best["mrr"] - baseline["mrr"]) / max(baseline["mrr"], 0.001) * 100
    )
    r1_gain = (
        (best["recall_at_1"] - baseline["recall_at_1"])
        / max(baseline["recall_at_1"], 0.001)
        * 100
    )

    v3_full = results[-1]
    v3_no_rw = results[-2]

    print(f"\n{'='*72}")
    print("  📊 结论")
    print(f"  {'='*72}")
    print(f"  最佳策略:         {best['strategy']}")
    print(f"  MRR:              {best['mrr']:.4f} (vs V2基线 {baseline['mrr']:.4f}, +{mrr_gain:.0f}%)")
    print(f"  Recall@1:         {best['recall_at_1']:.1%} (vs V2基线 {baseline['recall_at_1']:.1%}, +{r1_gain:.0f}%)")
    print(f"  查询改写贡献:     MRR {v3_full['mrr']:.4f} vs 无改写 {v3_no_rw['mrr']:.4f}")
    print(f"  融合 vs 最优单通道: MRR {best['mrr']:.4f} vs 单通道最优 {max(r['mrr'] for r in results[1:4]):.4f}")
    print()


def _cat_distribution(test_set: List[Dict]) -> str:
    cats = defaultdict(int)
    for item in test_set:
        cats[item.get("category", "未知")] += 1
    return ", ".join(f"{k}:{v}" for k, v in sorted(cats.items(), key=lambda x: -x[1])[:6])


if __name__ == "__main__":
    main()