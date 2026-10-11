"""向量存储：进程内索引与 Qdrant 两个后端。

**为什么要搬。** 现在的向量索引是**派生态**：每次检索先算一次 chunk id 集合的
签名，签名变了就把整个工作区的 embedding 从 MySQL 拉出来重建。这套设计的限制很
具体，而且都不会报错：

1. **多 worker 各建一份。** uvicorn 起 4 个 worker 就是 4 份索引、4 份内存。
   一次上传只让处理那个请求的 worker 失效，于是"刚上传的文档能不能检索到"
   取决于下一个请求打到了谁身上。
2. **重启即全丢。** 进程一起来，第一个查询要付一次全库重建的代价。
3. **暴力扫描。** ``IndexFlatIP`` 是精确检索，召回率 100%，但复杂度线性于库规模。

搬到 Qdrant 之后向量是**持久态**：多 worker 共享同一份、重启不丢、带 payload
过滤的 ANN 查询。代价是多一个要运维的服务。

**语义变化最容易出错的地方**：签名机制原本的作用是"判断要不要重建"，Qdrant 之后
它变成"判断两边一致不一致"。派生态可以随便丢（丢了重建就行），持久态丢了就是
真的丢了，所以增删必须**同步写**，而不是靠下一次检索去兜。

**为什么单 collection + payload 过滤，而不是每个工作区一个 collection**：后者
会随用户数线性增长，而 Qdrant 每个 collection 都有固定的内存与文件开销。带 filter
的 ANN 是 Qdrant 的主场——``workspace_id`` 建了 payload 索引之后，过滤会下推到
图遍历里，不是"先取 top-k 再筛掉别人的"（那种做法会让隔离和召回打架）。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from config import settings
from services.retrieval_index import VectorIndex, get_scope_indexes

logger = logging.getLogger("vector_store")


@dataclass(frozen=True, slots=True)
class VectorRecord:
    """一条待写入的向量。``document_id`` 存进 payload，删文档时按它批量删。"""

    chunk_id: str
    document_id: str
    vector: list[float]


@runtime_checkable
class VectorStore(Protocol):
    async def upsert(self, scope_id: str, records: list[VectorRecord]) -> None: ...

    async def delete_document(self, scope_id: str, document_id: str) -> None: ...

    async def search(
        self, scope_id: str, vector: list[float], top_k: int
    ) -> list[tuple[float, str]]: ...

    async def count(self, scope_id: str) -> int | None: ...


class MemoryVectorStore:
    """进程内索引后端。包住现有 ``VectorIndex``，不改它一行。

    ``upsert`` / ``delete_document`` 都是空操作：这个后端的索引是从 MySQL 派生的，
    由 ``retriever`` 在检索时按签名重建。保留这两个方法只是为了让两个后端满足
    同一个协议——调用方因此不需要 ``if settings.VECTOR_STORE == ...``。

    唯一例外是"配置要用 Qdrant 但当前降级到这儿"：那时 upsert 空转意味着那些
    chunk 永远进不了 Qdrant，恢复之后检索会开始漏它们（``uses_qdrant()`` 变回
    True 就不再从 MySQL 拉 embedding 重建了）。所以这里要数一下空转了多少条，
    好在恢复时明确告诉运维"这些得回填"。
    """

    async def upsert(self, scope_id: str, records: list[VectorRecord]) -> None:
        note_write_dropped(len(records))
        return None

    async def delete_document(self, scope_id: str, document_id: str) -> None:
        return None

    async def search(
        self, scope_id: str, vector: list[float], top_k: int
    ) -> list[tuple[float, str]]:
        index: VectorIndex = get_scope_indexes(scope_id).vector
        return index.search(vector, top_k=top_k)

    async def count(self, scope_id: str) -> int | None:
        # 派生态没有"权威计数"可言，返回 None 表示"不适用"，
        # 于是一致性核对那条路径在这个后端上直接跳过
        return None


class QdrantVectorStore:
    """Qdrant 后端。单 collection，``workspace_id`` 走 payload 过滤。"""

    _PAYLOAD_SCOPE = "workspace_id"
    _PAYLOAD_DOCUMENT = "document_id"

    def __init__(self) -> None:
        self._client = None
        self._ready = False

    # ---- 连接与建表 ----

    def _get_client(self):
        if self._client is not None:
            return self._client
        from qdrant_client import AsyncQdrantClient

        self._client = AsyncQdrantClient(
            url=settings.QDRANT_URL,
            api_key=settings.QDRANT_API_KEY or None,
            timeout=settings.QDRANT_TIMEOUT_SECONDS,
        )
        return self._client

    async def _ensure_collection(self, dimension: int) -> None:
        """collection 不存在就按这个维度建，并给 workspace_id 建 payload 索引。

        维度从**首批实际向量**推断，不从配置读：embedding 模型换了维度就变，而
        配置里再写一个维度就等于同一个事实有两处来源，改一处不改另一处不会报错，
        只会让每次写入都撞维度不匹配。
        """
        if self._ready:
            return
        from qdrant_client.models import Distance, VectorParams

        client = self._get_client()
        if not await client.collection_exists(settings.QDRANT_COLLECTION):
            await client.create_collection(
                collection_name=settings.QDRANT_COLLECTION,
                vectors_config=VectorParams(
                    size=dimension,
                    distance=Distance.COSINE,
                    # HNSW 参数和 memory/hnsw 后端共用同一组配置项:概念是同一套,
                    # 分成两组只会让两个后端的对照失去意义
                    hnsw_config=self._hnsw_config(),
                ),
            )
            # payload 索引是"过滤下推到图遍历"的前提。不建的话 Qdrant 只能先取
            # top-k 再筛掉别的工作区——那时隔离和召回会打架:邻居的文档挤掉了
            # 本工作区的候选,表现是"明明有文档却检索不到",而且随数据量变化。
            await client.create_payload_index(
                collection_name=settings.QDRANT_COLLECTION,
                field_name=self._PAYLOAD_SCOPE,
                field_schema="keyword",
            )
            logger.info(
                "created Qdrant collection %s (dim=%s)",
                settings.QDRANT_COLLECTION,
                dimension,
            )
        self._ready = True

    @staticmethod
    def _hnsw_config():
        from qdrant_client.models import HnswConfigDiff

        return HnswConfigDiff(
            m=max(4, settings.VECTOR_HNSW_M),
            ef_construct=max(8, settings.VECTOR_HNSW_EF_CONSTRUCT),
        )

    # ---- 写 ----

    async def upsert(self, scope_id: str, records: list[VectorRecord]) -> None:
        if not records:
            return
        from qdrant_client.models import PointStruct

        await self._ensure_collection(len(records[0].vector))
        points = [
            PointStruct(
                # chunk 的主键本来就是 uuid 字符串,Qdrant 直接收
                id=record.chunk_id,
                vector=record.vector,
                payload={
                    self._PAYLOAD_SCOPE: scope_id,
                    self._PAYLOAD_DOCUMENT: record.document_id,
                },
            )
            for record in records
        ]
        await self._get_client().upsert(
            collection_name=settings.QDRANT_COLLECTION, points=points, wait=True
        )

    async def delete_document(self, scope_id: str, document_id: str) -> None:
        """按 document_id 批量删。

        ``wait=True`` 不是保守：删完紧接着就可能有一次检索，异步删除会让刚删掉的
        文档再被召回一次——而那时 MySQL 里已经没有这一行了，``retriever`` 的
        ``if chunk_id in by_id`` 会把它静默丢掉，于是表现是"top_k 少了一条"。
        """
        if not self._ready and not await self._collection_present():
            return
        await self._get_client().delete(
            collection_name=settings.QDRANT_COLLECTION,
            points_selector=self._filter(scope_id, document_id),
            wait=True,
        )

    # ---- 读 ----

    async def search(
        self, scope_id: str, vector: list[float], top_k: int
    ) -> list[tuple[float, str]]:
        if not vector:
            return []
        if not self._ready and not await self._collection_present():
            return []
        from qdrant_client.models import SearchParams

        response = await self._get_client().query_points(
            collection_name=settings.QDRANT_COLLECTION,
            query=vector,
            query_filter=self._filter(scope_id),
            limit=max(1, top_k),
            with_payload=False,
            search_params=SearchParams(hnsw_ef=max(8, settings.VECTOR_HNSW_EF_SEARCH)),
        )
        # distance=COSINE 时 Qdrant 的 score 就是余弦相似度，与 memory 后端的
        # 分数量纲一致——RAG_MIN_SCORE 这个阈值才能在两个后端下含义相同
        return [(float(point.score), str(point.id)) for point in response.points]

    async def count(self, scope_id: str) -> int | None:
        if not self._ready and not await self._collection_present():
            return 0
        result = await self._get_client().count(
            collection_name=settings.QDRANT_COLLECTION,
            count_filter=self._filter(scope_id),
            exact=True,
        )
        return int(result.count)

    # ---- 辅助 ----

    async def _collection_present(self) -> bool:
        try:
            present = await self._get_client().collection_exists(
                settings.QDRANT_COLLECTION
            )
        except Exception as exc:
            logger.warning("Qdrant unreachable: %s", type(exc).__name__)
            return False
        self._ready = present
        return present

    def _filter(self, scope_id: str, document_id: str | None = None):
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        conditions = [
            FieldCondition(
                key=self._PAYLOAD_SCOPE, match=MatchValue(value=scope_id)
            )
        ]
        if document_id is not None:
            conditions.append(
                FieldCondition(
                    key=self._PAYLOAD_DOCUMENT, match=MatchValue(value=document_id)
                )
            )
        return Filter(must=conditions)


_memory_store = MemoryVectorStore()
_qdrant_store: QdrantVectorStore | None = None

# 降级是**暂时的**，不是终态。
#
# 退到内存索引这个决定本身站得住：一个可选的外部依赖不该让整个问答功能不可用
# （和"web_search 没配就不注册工具""faiss 缺失就走 numpy"同一套）。但原来那一版
# 把标记设成一次性的 ``_degraded = True``，只有测试用的 ``reset_for_tests()`` 会清
# ——于是 Qdrant 重启十秒，这个进程就永久停在内存索引直到自己也被重启，而"多 worker
# 共享一份、重启不丢"这些搬过来的理由一个都不成立了。症状还不报错：检索照样有结果。
#
# 所以这里改成带窗口的降级：过一段时间再试一次。恢复时明确报出降级期间空转了多少条
# upsert——那些 chunk 没进 Qdrant，恢复之后检索会开始漏它们（``uses_qdrant()`` 一旦
# 回到 True，retriever 就不再从 MySQL 拉 embedding 重建了），得回填。
_QDRANT_RETRY_SECONDS = 60.0

# time.monotonic() 的到期时刻；0 表示当前没有降级
_retry_after = 0.0
_degraded_reason: str | None = None
_degraded_episodes = 0
_writes_dropped_while_degraded = 0


def note_write_dropped(count: int) -> None:
    """记一笔"这次 upsert 因为降级而空转了"。没在降级时什么都不做。"""
    global _writes_dropped_while_degraded
    if _degraded_reason is not None:
        _writes_dropped_while_degraded += max(0, int(count))


def get_store() -> VectorStore:
    """按配置返回后端。Qdrant 连不上就退回进程内索引，并安排稍后重试。"""
    global _qdrant_store, _retry_after, _degraded_reason, _degraded_episodes
    global _writes_dropped_while_degraded

    if (settings.VECTOR_STORE or "").strip().lower() != "qdrant":
        return _memory_store
    if _qdrant_store is not None:
        return _qdrant_store

    now = time.monotonic()
    if _degraded_reason is not None and now < _retry_after:
        return _memory_store

    try:
        store = QdrantVectorStore()
        store._get_client()
    except Exception as exc:
        _degraded_reason = f"{type(exc).__name__}: {exc}"[:200]
        _degraded_episodes += 1
        _retry_after = now + _QDRANT_RETRY_SECONDS
        _qdrant_store = None
        if _degraded_episodes == 1:
            logger.error(
                "VECTOR_STORE=qdrant but the client could not be created (%s); "
                "falling back to the in-process index, retrying every %ss",
                _degraded_reason,
                _QDRANT_RETRY_SECONDS,
            )
        else:
            logger.warning(
                "Qdrant still unreachable (attempt %d): %s",
                _degraded_episodes,
                _degraded_reason,
            )
        return _memory_store

    if _degraded_reason is not None:
        dropped = _writes_dropped_while_degraded
        logger.info(
            "Qdrant reachable again after %d degraded episode(s)", _degraded_episodes
        )
        if dropped:
            logger.warning(
                "%d chunk upserts were dropped while degraded and never reached "
                "Qdrant — run scripts/backfill_qdrant.py to repair the collection",
                dropped,
            )
        _degraded_reason = None
        _degraded_episodes = 0
        _writes_dropped_while_degraded = 0
        _retry_after = 0.0

    _qdrant_store = store
    return store


def degradation_state() -> dict[str, Any] | None:
    """配置写着 qdrant、当前却停在内存索引时的现场。没降级就是 None。

    给健康检查用：降级不报错，检索也照样出结果，唯一能看出来的地方就是这里。

    ``errorType`` 与 ``reason`` 分开是因为 ``/health`` 不需要认证：异常消息里可能
    带着 URL 一类的内部信息，所以对外只给类型名，完整的那份留在日志里。
    """
    if _degraded_reason is None:
        return None
    return {
        "errorType": _degraded_reason.split(":", 1)[0],
        "episodes": _degraded_episodes,
        "writesDropped": _writes_dropped_while_degraded,
        "retryInSeconds": max(0.0, round(_retry_after - time.monotonic(), 1)),
    }


def uses_qdrant() -> bool:
    """当前**实际**在用 Qdrant 吗（区别于配置说要用）。

    分开一个函数是因为这两件事会不一致：配置写着 qdrant 但服务连不上时已经降级。
    ``retriever`` 要靠它决定还要不要从 MySQL 拉 embedding 大字段——判错的后果是
    热路径白拉几十兆，或者索引根本没建起来。
    """
    return get_store() is not _memory_store


def reset_for_tests() -> None:
    """清掉降级状态与客户端。只给测试用。"""
    global _qdrant_store, _retry_after, _degraded_reason
    global _degraded_episodes, _writes_dropped_while_degraded
    _qdrant_store = None
    _retry_after = 0.0
    _degraded_reason = None
    _degraded_episodes = 0
    _writes_dropped_while_degraded = 0
