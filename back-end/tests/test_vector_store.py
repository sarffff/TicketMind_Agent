"""向量存储：两个后端满足同一协议、Qdrant 的过滤与降级。

**不要求本地起 Qdrant**：客户端整个替成假的。这里要验的东西都不在网络那一层。

真正值得测的是三处会静默出错的地方：

1. **降级。** 配了 qdrant 但连不上时必须退回 memory，不能 500；冷却窗口内不敲第二次
   门（宕一小时刷爆日志算什么呢），但**窗口到了要再试一次**——降级不是终态，一次瞬时
   连不上不该把这个进程永久钉在内存索引上。降级本身是无声的，所以 ``uses_qdrant()``
   必须说真话（``retriever`` 靠它决定还要不要从 MySQL 拉 embedding 大字段），而
   ``degradation_state()`` 得报出"期间空转了几条 upsert"——那些 chunk 没进 Qdrant，
   恢复之后会开始被检索漏掉，除了这个数没有任何地方说得出来。
2. **workspace 过滤。** 单 collection 存所有工作区，filter 漏了就是跨工作区串数据。
3. **删除。** MySQL 那边有 ON DELETE CASCADE，Qdrant 没有外键。漏删的后果是删掉的
   文档还能被召回，而 ``retriever`` 的 ``if chunk_id in by_id`` 会静默丢掉它——
   表现是 top_k 少了几条，没有任何报错。
"""
from __future__ import annotations

import pytest

from config import settings
from services import vector_store as vs
from services.vector_store import MemoryVectorStore, VectorRecord, VectorStore
from conftest import run


@pytest.fixture(autouse=True)
def _reset():
    vs.reset_for_tests()
    yield
    settings.VECTOR_STORE = "memory"
    vs.reset_for_tests()


# ========== 协议 ==========


def test_memory_store_satisfies_the_protocol():
    """两个后端满足同一协议，调用方才不需要到处写 if VECTOR_STORE == ...。"""
    assert isinstance(MemoryVectorStore(), VectorStore)


def test_memory_writes_are_no_ops():
    """这个后端的索引是从 MySQL 派生的，由 retriever 按签名重建。
    保留这两个方法只为满足协议。"""
    store = MemoryVectorStore()

    run(store.upsert("w1", [VectorRecord("c1", "d1", [1.0, 0.0])]))
    run(store.delete_document("w1", "d1"))

    # 派生态没有"权威计数"，None 表示不适用 → 一致性核对在这个后端直接跳过
    assert run(store.count("w1")) is None


def test_memory_search_delegates_to_the_process_index():
    from services.retrieval_index import get_scope_indexes, invalidate_scope_indexes

    invalidate_scope_indexes("w-vs")
    assert run(MemoryVectorStore().search("w-vs", [1.0, 0.0], 5)) == []
    assert get_scope_indexes("w-vs") is not None


# ========== 后端选择与降级 ==========


def test_default_is_the_memory_backend():
    settings.VECTOR_STORE = "memory"
    vs.reset_for_tests()

    assert vs.uses_qdrant() is False
    assert vs.get_store() is vs._memory_store


def test_qdrant_client_failure_degrades_once(monkeypatch):
    """降级不该 500，也不该每次检索刷一条日志。"""
    settings.VECTOR_STORE = "qdrant"
    vs.reset_for_tests()
    calls = {"count": 0}

    def _boom(self):
        calls["count"] += 1
        raise ImportError("qdrant_client not installed")

    monkeypatch.setattr(vs.QdrantVectorStore, "_get_client", _boom)

    assert vs.get_store() is vs._memory_store
    assert vs.uses_qdrant() is False
    # 第二次不再重试
    vs.get_store()
    assert calls["count"] == 1


def test_uses_qdrant_tells_the_truth_after_degrading(monkeypatch):
    """retriever 靠它决定还要不要从 MySQL 拉 embedding 大字段。
    判错的后果是热路径白拉几十兆，或者索引根本没建起来。"""
    settings.VECTOR_STORE = "qdrant"
    vs.reset_for_tests()
    monkeypatch.setattr(
        vs.QdrantVectorStore, "_get_client", lambda self: (_ for _ in ()).throw(OSError())
    )

    assert vs.uses_qdrant() is False


def test_unknown_backend_name_falls_back_to_memory():
    settings.VECTOR_STORE = "pinecone"
    vs.reset_for_tests()

    assert vs.get_store() is vs._memory_store


# ========== 降级不是终态 ==========


class _Clock:
    """替掉 ``vs.time``：降级窗口是按 monotonic 算的，测试不能真等 60 秒。

    换整个模块属性而不是 ``monkeypatch.setattr(time, "monotonic", ...)``——后者会
    改到进程级的 time，同一轮里别的东西（超时、退避）就跟着一起被冻住了。
    """

    def __init__(self, start: float = 1000.0):
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _refusing(monkeypatch, counter: dict):
    def _boom(self):
        counter["count"] = counter.get("count", 0) + 1
        raise OSError("connection refused")

    monkeypatch.setattr(vs.QdrantVectorStore, "_get_client", _boom)


def test_degradation_retries_after_the_window(monkeypatch):
    """一次瞬时连不上不该把这个进程永久钉在内存索引上。

    踩过的形状：``_degraded`` 是单向门，只有测试用的 ``reset_for_tests()`` 会清。
    Qdrant 重启十秒 → 本进程整个生命周期都用内存索引，而"多 worker 共享、重启不丢"
    这些搬过去的理由一个都不成立了，症状还不报错。
    """
    settings.VECTOR_STORE = "qdrant"
    vs.reset_for_tests()
    clock = _Clock()
    monkeypatch.setattr(vs, "time", clock)
    calls: dict = {}
    _refusing(monkeypatch, calls)

    assert vs.get_store() is vs._memory_store
    assert vs.degradation_state() is not None
    assert calls == {"count": 1}

    # 冷却窗口内不敲第二次门（否则 Qdrant 宕一小时 = 刷爆日志）
    for _ in range(20):
        vs.get_store()
    assert calls == {"count": 1}

    # 到点再试一次；这次连上了就该切回去
    monkeypatch.setattr(vs.QdrantVectorStore, "_get_client", lambda self: object())
    clock.advance(vs._QDRANT_RETRY_SECONDS + 1)
    assert vs.get_store() is not vs._memory_store
    assert vs.uses_qdrant() is True
    assert vs.degradation_state() is None, "恢复之后不该还报着降级"


def test_writes_dropped_while_degraded_are_counted(monkeypatch):
    """降级期间的 upsert 是空转的，那些 chunk 永远进不了 Qdrant。

    恢复之后 ``uses_qdrant()`` 回到 True，retriever 就不再从 MySQL 拉 embedding
    重建了——于是这几条开始被检索漏掉，而没有任何一条日志说过"少了东西"。
    计数是这件事唯一的出口。
    """
    import asyncio

    settings.VECTOR_STORE = "qdrant"
    vs.reset_for_tests()
    clock = _Clock()
    monkeypatch.setattr(vs, "time", clock)
    _refusing(monkeypatch, {})
    assert vs.get_store() is vs._memory_store

    records = [vs.VectorRecord(chunk_id=f"c{i}", document_id="d1", vector=[0.1]) for i in range(3)]
    asyncio.run(vs._memory_store.upsert("ws-1", records))

    state = vs.degradation_state()
    assert state["writesDropped"] == 3

    # 恢复时这个数会被报进日志再清零——留着不清就是"每次恢复都报一遍陈年旧账"
    monkeypatch.setattr(vs.QdrantVectorStore, "_get_client", lambda self: object())
    clock.advance(vs._QDRANT_RETRY_SECONDS + 1)
    vs.get_store()
    assert vs.degradation_state() is None


def test_memory_backend_never_reports_degradation():
    """没配 Qdrant 就不是降级，是正常状态。把两者混起来会让健康检查天天红。"""
    settings.VECTOR_STORE = "memory"
    vs.reset_for_tests()

    assert vs.get_store() is vs._memory_store
    assert vs.degradation_state() is None


# ========== Qdrant：过滤与写入 ==========


class _FakeQdrant:
    """记下每次调用的参数。不连网络——这里验的东西都不在网络那一层。"""

    def __init__(self, *, exists: bool = True, points=()):
        self.exists = exists
        self.created: list[dict] = []
        self.payload_indexes: list[str] = []
        self.upserted: list[dict] = []
        self.deleted: list[dict] = []
        self.searches: list[dict] = []
        self._points = list(points)

    async def collection_exists(self, collection_name):
        return self.exists

    async def create_collection(self, collection_name, vectors_config):
        self.created.append({"name": collection_name, "config": vectors_config})
        self.exists = True

    async def create_payload_index(self, collection_name, field_name, field_schema):
        self.payload_indexes.append(field_name)

    async def upsert(self, collection_name, points, wait=False):
        self.upserted.append({"points": points, "wait": wait})

    async def delete(self, collection_name, points_selector, wait=False):
        self.deleted.append({"selector": points_selector, "wait": wait})

    async def query_points(self, **kwargs):
        self.searches.append(kwargs)

        class _Result:
            points = self._points

        return _Result()

    async def count(self, collection_name, count_filter=None, exact=True):
        class _Count:
            count = len(self._points)

        return _Count()


class _Point:
    def __init__(self, point_id: str, score: float):
        self.id = point_id
        self.score = score


def _store(fake) -> vs.QdrantVectorStore:
    store = vs.QdrantVectorStore()
    store._client = fake
    return store


def _qdrant_available() -> bool:
    try:
        import qdrant_client  # noqa: F401
    except ImportError:
        return False
    return True


_needs_qdrant = pytest.mark.skipif(
    not _qdrant_available(),
    reason="qdrant-client 未安装；这些用例只验参数构造，不连网络",
)


@_needs_qdrant
def test_collection_is_created_with_the_observed_dimension():
    """维度从首批实际向量推断，不从配置读：配置里再写一个维度就等于同一个事实
    有两处来源，改一处不改另一处不报错，只会让每次写入都撞维度不匹配。"""
    fake = _FakeQdrant(exists=False)
    store = _store(fake)

    run(store.upsert("w1", [VectorRecord("c1", "d1", [0.1] * 1024)]))

    assert fake.created and fake.created[0]["config"].size == 1024


@_needs_qdrant
def test_workspace_payload_index_is_created():
    """没有 payload 索引时过滤没法下推到图遍历，Qdrant 只能先取 top-k 再筛掉
    别人的——那时隔离和召回会打架：邻居的文档挤掉本工作区的候选。"""
    fake = _FakeQdrant(exists=False)

    run(_store(fake).upsert("w1", [VectorRecord("c1", "d1", [0.1, 0.2])]))

    assert "workspace_id" in fake.payload_indexes


@_needs_qdrant
def test_upsert_carries_scope_and_document_in_payload():
    fake = _FakeQdrant()

    run(
        _store(fake).upsert(
            "w1",
            [
                VectorRecord("c1", "d1", [0.1, 0.2]),
                VectorRecord("c2", "d1", [0.3, 0.4]),
            ],
        )
    )

    points = fake.upserted[0]["points"]
    assert [point.id for point in points] == ["c1", "c2"]
    assert points[0].payload == {"workspace_id": "w1", "document_id": "d1"}
    # wait=True：写完紧接着就可能有一次自检索
    assert fake.upserted[0]["wait"] is True


@_needs_qdrant
def test_empty_upsert_is_skipped():
    fake = _FakeQdrant()

    run(_store(fake).upsert("w1", []))

    assert fake.upserted == []


@_needs_qdrant
def test_search_filters_by_workspace():
    """单 collection 存所有工作区，filter 漏了就是跨工作区串数据。"""
    fake = _FakeQdrant(points=[_Point("c1", 0.9), _Point("c2", 0.4)])

    result = run(_store(fake).search("w1", [0.1, 0.2], 5))

    assert result == [(0.9, "c1"), (0.4, "c2")]
    query_filter = fake.searches[0]["query_filter"]
    conditions = query_filter.must
    assert len(conditions) == 1
    assert conditions[0].key == "workspace_id"
    assert conditions[0].match.value == "w1"


@_needs_qdrant
def test_search_with_empty_vector_short_circuits():
    fake = _FakeQdrant()

    assert run(_store(fake).search("w1", [], 5)) == []
    assert fake.searches == []


@_needs_qdrant
def test_delete_filters_by_document_too():
    """漏掉 document_id 会把整个工作区删空。"""
    fake = _FakeQdrant()

    run(_store(fake).delete_document("w1", "d1"))

    conditions = fake.deleted[0]["selector"].must
    keys = {condition.key: condition.match.value for condition in conditions}
    assert keys == {"workspace_id": "w1", "document_id": "d1"}
    assert fake.deleted[0]["wait"] is True


@_needs_qdrant
def test_missing_collection_makes_reads_empty_not_broken():
    """collection 还没建（全新部署、还没回填）时读操作返回空，而不是抛异常。"""
    fake = _FakeQdrant(exists=False)
    store = vs.QdrantVectorStore()
    store._client = fake

    assert run(store.search("w1", [0.1], 5)) == []
    assert run(store.count("w1")) == 0
