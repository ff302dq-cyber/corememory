import math
from typing import List, Optional, Any

from astrbot.api import logger, AstrBotConfig
from astrbot.api.star import Context


def cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
    """
    计算两个向量之间的余弦相似度。
    """
    if not isinstance(vec_a, list) or not isinstance(vec_b, list) or len(vec_a) != len(vec_b) or not vec_a:
        return 0.0
    
    dot_product = sum(a * b for a, b in zip(vec_a, vec_b))
    
    norm_a = math.sqrt(sum(a * a for a in vec_a))
    norm_b = math.sqrt(sum(b * b for b in vec_b))
    
    if norm_a == 0 or norm_b == 0:
        return 0.0
        
    return dot_product / (norm_a * norm_b)


class SemanticSearch:
    def __init__(self, context: Context, config: AstrBotConfig):
        self.context = context
        self.config = config
        # 移除初始化时的静态缓存，改为动态获取

    def _get_provider(self):
        """动态获取当前可用的嵌入模型服务商"""
        # 1. 尝试获取配置指定的ID
        provider_id = self.config.get("embedding_provider_id")
        if provider_id:
            # 尝试从所有已加载的 provider 中查找
            # 注意：get_provider_by_id 通常用于 LLM，这里我们需要遍历所有 embedding provider
            all_providers = self.context.get_all_embedding_providers()
            for p in all_providers:
                if p.provider_config.get('id') == provider_id:
                    return p
        
        # 2. 如果指定的不存在，或者没指定，Fallback 到第一个可用的
        all_providers = self.context.get_all_embedding_providers()
        if all_providers:
            # logger.debug(f"使用自动选择的嵌入模型服务商: {all_providers[0].provider_config.get('id')}")
            return all_providers[0]
            
        logger.warning("核心记忆插件：无法获取嵌入向量，未找到任何可用的嵌入模型服务商。")
        return None

    async def get_embedding(self, text: str) -> Optional[List[float]]:
        """为单条文本生成向量。"""
        if not text or not text.strip():
            return None
            
        provider = self._get_provider()
        if not provider:
            return None

        try:
            vector = await provider.get_embedding(text)

            if not vector or not isinstance(vector, list) or all(v == 0 for v in vector):
                logger.warning(f"服务商返回了无效向量。")
                return None
            return vector
        except Exception as e:
            logger.error(f"生成向量时出错: {e}", exc_info=True)
            return None

    async def get_embeddings(self, texts: List[str]) -> Optional[List[List[float]]]:
        """为多条文本批量生成向量。"""
        provider = self._get_provider()
        if not provider:
            return None

        try:
            vectors = await provider.get_embeddings(texts)
            return vectors
        except Exception as e:
            logger.error(f"批量生成向量时出错: {e}", exc_info=True)
            return None

    async def find_related_diaries(self, query_text: str, all_diaries: List[dict], diary_vectors: List[List[float]], num_results: int) -> List[dict]:
        """根据查询文本，从日记和向量中找出最相关的条目。"""
        if not query_text or not all_diaries or not diary_vectors or num_results <= 0:
            return []

        query_vector = await self.get_embedding(query_text)
        if not query_vector:
            return []
            
        similarity_threshold = self.config.get("semantic_similarity_threshold", 0.5)

        scores = [
            (i, cosine_similarity(query_vector, diary_vec))
            for i, diary_vec in enumerate(diary_vectors)
            if diary_vec
        ]
        
        # --- 增加调试日志：查看匹配情况 ---
        if scores:
            max_score = max(scores, key=lambda x: x[1])[1]
            logger.info(f"语义搜索: 查询='{query_text[:100]}...' | 最高相似度: {max_score:.4f} | 阈值: {similarity_threshold}")
        else:
            logger.info(f"语义搜索: 无有效向量数据进行比对。")
        # --------------------------------

        qualified_scores = sorted(
            [score for score in scores if score[1] >= similarity_threshold],
            key=lambda x: x[1],
            reverse=True
        )

        related_diaries = []
        for index, score in qualified_scores[:num_results]:
            if index < len(all_diaries):
                diary = all_diaries[index]
                related_diaries.append(diary)
                logger.debug(f"  -> 选中记忆 [{diary.get('timestamp')}] 相似度: {score:.4f}")
            
        return related_diaries