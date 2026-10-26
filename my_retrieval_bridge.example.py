"""可选：如果你已经有自己的 BM25+Dense 混合检索函数，把此文件复制为
my_retrieval_bridge.py，按实际模块导入并实现 retrieve。不要直接运行本示例。
"""

def retrieve(query: str, top_k: int = 8) -> list[dict]:
    # from scripts.your_existing_retrieval import search
    # hits = search(query, top_k=top_k)
    # return [{"chunk_id": h.chunk_id, "text": h.text,
    #          "title": h.title, "pmcid": h.pmcid, "score": h.score} for h in hits]
    raise NotImplementedError("请调用你现有的真实检索函数，不要用占位证据生成医学回答")
