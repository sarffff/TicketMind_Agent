"""健康检查:它必须能说"我不好"。

改动之前只有 ``/``,而它返回的是一个**写死的字典**(``status: "running"``)。
数据库挂了它照样回 200 + running——于是 k8s 探针、负载均衡、监控告警全都认为
这个实例是好的,流量继续打进来,每个请求都在 500。

一个永远说"我很好"的健康检查比没有健康检查更糟:它让"实例坏了"在监控上不可见。

所以这一组里最要紧的一条是 ``test_数据库不通时回503``——它证明这个端点**会**变红。
其余几条守的是它不要变得太敏感:软依赖(Redis)不该影响状态码,否则 Redis 抖一下
就会让编排系统把一批本来能正常服务的实例全部重启。
"""
from __future__ import annotations

import pytest
import sqlalchemy


@pytest.fixture()
def db_session():
    from database import Base

    import models  # noqa: F401

    engine = sqlalchemy.create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=sqlalchemy.pool.StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    session = sqlalchemy.orm.sessionmaker(bind=engine, future=True)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def client(db_session):
    from fastapi.testclient import TestClient

    from main import app
    from database import get_db

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    app.state.limiter.enabled = False
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.state.limiter.enabled = True
        app.dependency_overrides.clear()


def test_健康时回200且列出各项检查(client):
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["database"] == "ok"
    # Redis 未配置时说 disabled,而不是 error——那是配置选择,不是故障
    assert body["checks"]["redis"] in ("disabled", "ok", "unavailable")
    # 向量后端必须有名字。它和 Redis 同理是软依赖，但比 Redis 更安静：降级之后
    # 检索照样出结果，唯一的症状是重启重建、多 worker 各一份，全是无声的。
    assert body["checks"]["vectorStore"] in ("memory", "ok") or body["checks"][
        "vectorStore"
    ].startswith("degraded:")


def test_向量降级只报告不影响ready(client, monkeypatch):
    """Qdrant 挂了不该让这个实例被踢掉。

    反过来也要成立：不踢掉不等于不吭声——降级期间"配置要 Qdrant、实际在内存"这件事
    必须能从这一格里问出来，否则它就只有一行启动日志，而没人会去翻启动日志。
    """
    import main
    from services import vector_store as vs

    vs.reset_for_tests()
    monkeypatch.setattr(vs.settings, "VECTOR_STORE", "qdrant")
    monkeypatch.setattr(
        vs.QdrantVectorStore,
        "_get_client",
        lambda self: (_ for _ in ()).throw(OSError("connection refused")),
    )
    try:
        response = client.get("/health")
        assert response.status_code == 200, "软依赖不能把 ready 拉下来"
        assert response.json()["status"] == "ok"
        report = response.json()["checks"]["vectorStore"]
        assert report.startswith("degraded:"), report
        assert "OSError" in report, "报的要足够具体才知道该去查什么"
        # 异常消息本身（"connection refused"）不出去：这个端点不需要认证
        assert "connection refused" not in report
    finally:
        vs.reset_for_tests()


def test_数据库不通时回503(client, monkeypatch):
    """这一组的核心。

    健康检查最容易犯的错是**永远返回健康**,而那种错在正向用例里完全看不出来:
    ``status == "ok"`` 既可能是"真的连上了",也可能是"压根没去连"。
    """
    import main

    class _BrokenSession:
        def execute(self, *_args, **_kwargs):
            raise sqlalchemy.exc.OperationalError("SELECT 1", {}, Exception("down"))

        def close(self):
            pass

    monkeypatch.setattr(main, "SessionLocal", lambda: _BrokenSession())

    response = client.get("/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unhealthy"
    assert body["checks"]["database"].startswith("error:")


def test_数据库错误里不带连接串(client, monkeypatch):
    """连接串里可能有凭据,而这个端点通常不需要认证就能打。"""
    import main

    secret = "postgresql://admin:hunter2@db.internal:5432/prod"

    class _LeakySession:
        def execute(self, *_args, **_kwargs):
            raise RuntimeError(f"could not connect to {secret}")

        def close(self):
            pass

    monkeypatch.setattr(main, "SessionLocal", lambda: _LeakySession())

    body = client.get("/health").json()

    assert "hunter2" not in str(body)
    assert "db.internal" not in str(body)
    # 只报类型,够运维知道去查哪一类问题
    assert body["checks"]["database"] == "error: RuntimeError"


def test_redis出问题不影响状态码(client, monkeypatch):
    """软依赖:Redis 没了会退化成每次重算,不是这个实例坏了。

    把它算进状态码会造成一类更糟的故障——Redis 抖一下,编排系统把一批本来能
    正常服务的实例全部重启。
    """
    from config import settings
    import main

    monkeypatch.setattr(settings, "REDIS_URL", "redis://localhost:6379/0")

    class _BrokenRedis:
        def ping(self):
            raise ConnectionError("nope")

    monkeypatch.setattr(main.redis_service, "client", _BrokenRedis(), raising=False)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["checks"]["redis"].startswith("error:")


def test_根路径不再自称健康检查而是指过去(client):
    """``/`` 保持零依赖、永远 200:它回答的是"端口通不通"。

    但它得说清自己不是健康检查,否则下一个配探针的人还会指向它。
    """
    body = client.get("/").json()

    assert body["status"] == "running"
    assert body["health"] == "/health"
