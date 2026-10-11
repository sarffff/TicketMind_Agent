import logging
import os

import asyncio

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.middleware.base import BaseHTTPMiddleware

from rate_limit import limiter

from config import settings
from database import init_db, SessionLocal
from redis_service import redis_service
from routers import (
    knowledge_router,
    auth_router,
    attachment_router,
    metrics_router,
    notification_router,
    audit_router,
    workspace_router,
    skill_router,
    ticket_router,
)
from services import prompt_library
from services import security_preflight
from services import skill_library
from services import subagent
from services import ingest_clean
from services import retriever
from services import vector_store
from services.rerank import rerank_client

logger = logging.getLogger("main")

app = FastAPI(
    title="Customer Service Ticket Agent API",
    description="客服工单解决 Agent 的后端 API：接入、编排、治理、审计、指标",
    version="1.0.0",
    # 生产环境关闭文档接口
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None if settings.is_production else "/redoc",
    openapi_url=None if settings.is_production else "/openapi.json",
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# ========== 安全响应头中间件 ==========
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self'"
        return response


app.add_middleware(SecurityHeadersMiddleware)

# CORS 白名单
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(auth_router.router)
app.include_router(ticket_router.router)
app.include_router(knowledge_router.router)
app.include_router(skill_router.router)
app.include_router(attachment_router.router)
app.include_router(metrics_router.router)
app.include_router(notification_router.router)
app.include_router(audit_router.router)
app.include_router(workspace_router.router)

# 静态文件服务：附件上传后的访问入口
os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=settings.UPLOAD_DIR), name="uploads")


@app.get("/")
async def root():
    """API 根路径。**不是**健康检查——它只证明进程在监听。

    健康检查在 ``/health``。分开是刻意的:这个端点要保持零依赖、永远 200,
    它回答的是"端口通不通";而"这个实例现在能不能干活"要真的去连数据库。
    """
    return {
        "message": "Customer Service Ticket Agent API",
        "version": "1.0.0",
        "status": "running",
        "health": "/health",
    }


@app.get("/health")
async def health(response: Response):
    """给编排系统看的健康检查:真的去连一次数据库。

    ## 为什么不能沿用 ``/``

    ``/`` 返回的是一个写死的字典。数据库挂了它照样回 ``running``——而 k8s 探针、
    负载均衡、监控告警全都会据此认为这个实例是好的,于是流量继续打进来,
    每一个请求都在 500。一个永远说"我很好"的健康检查比没有健康检查更糟:
    它让"实例坏了"这件事在监控上不可见。

    ## 判据只有数据库

    数据库是**硬依赖**:它不通,认证、对话、检索没有一个能工作。所以它决定
    HTTP 状态码。

    Redis 是**软依赖**(会话缓存与摘要缓存,没有就退化成每次重算),模型 API
    是外部服务(它挂了是一次请求失败,不是这个实例坏了)。把软依赖算进状态码会
    造成一类更糟的故障:Redis 抖一下,编排系统把一批本来能正常服务的实例全部
    重启。所以它们只报告状态,不影响 ready。

    ## 503 而不是 200 带一个 status 字段

    编排系统默认只看状态码。返回 200 + ``{"status":"unhealthy"}`` 需要在探针
    上额外配一条解析规则,而漏配的后果是这个端点白做。
    """
    checks: dict[str, str] = {}

    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        checks["database"] = "ok"
        ready = True
    except Exception as exc:  # noqa: BLE001 - 任何异常都算不健康
        # 只记类型不记消息:连接串里可能带着凭据,而这个端点通常是不需要认证的
        checks["database"] = f"error: {type(exc).__name__}"
        logger.error("health check: database unreachable (%s)", type(exc).__name__)
        ready = False
    finally:
        db.close()

    if not settings.REDIS_URL:
        checks["redis"] = "disabled"
    else:
        try:
            client = getattr(redis_service, "client", None)
            if client is None:
                checks["redis"] = "unavailable"
            else:
                client.ping()
                checks["redis"] = "ok"
        except Exception as exc:  # noqa: BLE001 - 软依赖,不影响 ready
            checks["redis"] = f"error: {type(exc).__name__}"

    # 向量后端同样是软依赖,不进状态码：降级到进程内索引时检索照样出结果,把它算成
    # 不健康就会让 Qdrant 重启十秒导致编排系统把这个实例踢掉。但"配置要 Qdrant 而
    # 实际停在内存"这件事总得有个地方能问出来——它不报错、不 500,唯一的症状是重启
    # 后重建索引、多 worker 各一份,而这些都是安静发生的。
    #
    # 先 uses_qdrant() 再读降级状态,顺序有意义：降级标记是**有人敲过门才会立起来**的,
    # 而健康检查就是那个敲门的人。反过来写的话,一个刚重启、还没接过流量的实例会先
    # 回几轮 "ok",把 Qdrant 已经挂了这件事藏到第一个检索请求之后。
    # 探测本身受冷却窗口约束（默认 60 秒一次）+ QDRANT_TIMEOUT_SECONDS,不会变成
    # 每次探针都去重连。
    vector_store.uses_qdrant()
    degraded = vector_store.degradation_state()
    if degraded is not None:
        checks["vectorStore"] = (
            f"degraded: {degraded['errorType']}, "
            f"{degraded['writesDropped']} 条写入未落库, "
            f"{degraded['retryInSeconds']}s 后重试"
        )
    elif (settings.VECTOR_STORE or "memory").strip().lower() == "qdrant":
        checks["vectorStore"] = "ok"
    else:
        checks["vectorStore"] = "memory"

    if not ready:
        response.status_code = 503
    return {
        "status": "ok" if ready else "unhealthy",
        "version": "1.0.0",
        "checks": checks,
    }


def _check_ingest_backend() -> None:
    """校验 PDF 结构恢复的依赖装上了。

    这一条必须**拒绝启动**，不能只警告：缺了 pdfplumber 时每一份 PDF 都会静默退回
    无结构抽取，于是 ``heading_path`` 恒为空——``chunking`` 承诺的「标题路径」与
    「章节边界优先」两件事全部失效，而文档状态照样是 ``indexed``。更糟的是同一份
    PDF 在两台机器上会切出不同的块，而 eval 的结论就依赖这个。

    这正是把它定成必需依赖而不是可选降级的理由；只做成一条警告等于把那个决定
    又变回可选。
    """
    if not settings.INGEST_PDF_STRUCTURE:
        return
    if not ingest_clean.structure_backend_available():
        raise RuntimeError(
            "INGEST_PDF_STRUCTURE=true 但 pdfplumber 没有安装。"
            "PDF 会静默退回无结构抽取（标题层级丢失、词内空格不修），"
            "而文档状态仍是 indexed——这种失败查不出来。"
            "请 pip install -r requirements.txt，或把 INGEST_PDF_STRUCTURE 设为 false。"
        )


def _check_multiworker_vector_store() -> None:
    """多 worker 部署但向量库还是 memory —— 响亮告警（不拒启动）。

    memory 后端的索引是每个 worker 进程各建一份、从 MySQL 派生（见 services/vector_store
    模块文档）：一次上传只在服务了那个请求的 worker 上生效，之后能不能检索到取决于
    下个请求打到谁。单 worker 没事，多 worker 就是"刚传的文档一半时间搜不到"这种
    查不出的间歇性 bug。

    不硬拦（不像 pdfplumber 那条必需依赖）：worker 数没有可靠的自省途径——
    ``WEB_CONCURRENCY`` 是 gunicorn/uvicorn 的约定 env，但不是每种起法都设它。按这个
    约定给 hint，误报只多一条告警，漏报由 README 部署章节与这条共同兜。切 Qdrant 消除。
    """
    try:
        workers = int(os.environ.get("WEB_CONCURRENCY", "1"))
    except ValueError:
        workers = 1
    if workers <= 1:
        return
    from services import vector_store

    if not vector_store.uses_qdrant():
        print(
            f"  ⚠ WEB_CONCURRENCY={workers}（多 worker）但 VECTOR_STORE 仍是 memory —— "
            "每个 worker 各建一份索引，刚上传的文档会间歇检索不到。多 worker 请切 "
            "VECTOR_STORE=qdrant（见 docker-compose.qdrant.yml 与 README 部署章节）。"
        )


def _adopt_orphaned_documents() -> None:
    """把删用户留下的无主私有文档收编成工作区共享文档。

    挂在启动上而不是删用户时,因为**没有删用户的接口**:今天删账号只能走 SQL,
    而 SQL 不触发任何应用层逻辑。所以启动是唯一能兜住它的位置。

    正常情况下这里一篇都收编不到(一行都不打)。收编到了就必须打出来:那意味着
    有文档从"谁都搜不到"变成了"全工作区可检索",而这件事在界面上只表现为
    "共享库里多了几篇没人记得传过的文档"——不打出来就没有任何地方能对上因果。

    整段包在 try 里:收编失败不该让服务起不来。它修的是一个已经存在了一段时间的
    滞留状态,晚一次重启再修没有损失,而起不来是立刻的损失。
    """
    from database import SessionLocal
    from services import workspace_service

    db = SessionLocal()
    try:
        adopted = workspace_service.adopt_orphaned_documents(db)
        if adopted:
            print(
                f"  ⚠ 已收编 {adopted} 篇无主个人文档为工作区共享文档 —— "
                "原上传者的账号已被删除，这些文档此前谁都检索不到、也删不掉。"
                "现在管理员能在知识库列表里看到它们（带「继承」标记），自行决定删或留。"
            )
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠ 收编无主个人文档失败（不影响启动）：{type(exc).__name__}: {exc}")
    finally:
        db.close()


def _recover_stalled_documents() -> None:
    """重启后把卡在 ``processing`` 的文档重新排进持久队列。

    上传后的索引现在走 ``document_jobs`` 持久队列（见 services/document_queue.py），
    但进程在索引中途被杀时，可能留下两类残留：文档停在 ``processing``、而队列行停在
    ``running``（认领它的进程没了）。这里把前者重新入队、把后者的过期租约回收，
    然后触发一次抽干。

    ``index_document`` 幂等（先删旧分块再写新的），所以重跑安全；``enqueue`` 对已有
    未结束任务去重，所以重复入队不会把一篇并发索引两次。

    只在 startup 扫一次：``processing`` 是"正在某个进程里跑"的状态，重启后没有别的
    进程会接它们（同 ``_adopt_orphaned_documents`` 的取舍）。队列的持久性 + 启动重驱
    一起把改动前"进程内 fire-and-forget 重启即丢"那个洞补上。
    """
    from models import Document
    from services import document_queue

    enqueued = 0
    db = SessionLocal()
    try:
        stalled = (
            db.query(Document.id)
            .filter(Document.status == "processing")
            .limit(500)
            .all()
        )
        for row in stalled:
            if document_queue.enqueue(db, row[0]) is not None:
                enqueued += 1
        # 回收上次崩在 running 的队列任务（租约早过期），它们也会被重新认领
        document_queue.reap_expired_leases(db)
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠ 扫描中断文档失败（不影响启动）：{type(exc).__name__}: {exc}")
        return
    finally:
        db.close()

    if enqueued:
        asyncio.create_task(document_queue.run_pending())
        print(
            f"  ↻ 重新入队 {enqueued} 篇上次中断在 processing 的文档"
            "（现在走持久队列，重启不丢、失败重试）。"
        )


def _describe_ticket_agent() -> None:
    """把工单域的实际生效状态打出来，尤其是**挂起状态写在哪里**。

    检查点路径配成一个临时目录时，症状不是报错，而是"批到一半的单子凭空消失了"
    ——那是这条能力最难查的一种失效：进程活着一切正常，重启之后那张工单从没存在过。
    """
    if not settings.TICKET_AGENT_ENABLED:
        print("Ticket agent: off（/tickets 只读可看，提交与执行返回 409）")
        return
    path = settings.TICKET_CHECKPOINT_DB
    parent = os.path.dirname(os.path.abspath(path)) or "."
    try:
        if not os.path.isdir(parent):
            raise OSError(f"目录不存在：{parent}")
        probe = os.path.join(parent, ".ticket_write_probe")
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("x")
        os.remove(probe)
    except OSError as exc:
        print(
            f"  ⚠ Ticket agent: 检查点不可写（{path}：{exc}）。"
            "挂在人工审批上的工单会在重启之后消失。"
        )
        return
    print(f"Ticket agent: on，检查点 {path}，渠道 {settings.TICKET_CHANNELS}")
    # 治理数字必须打出来：限额配成 0（= 不限）时，症状不是报错而是"这张单子跑了
    # 四十轮 / 这个月退款没有闸"。界面上看不见这两个开关，它们只在出事时可见。
    print(
        "Ticket limits: 单工单工具调用 "
        + (
            f"≤{settings.TICKET_MAX_TOOL_CALLS} 次"
            if int(settings.TICKET_MAX_TOOL_CALLS or 0) > 0
            else "不限"
        )
        + "，当日退款 "
        + (
            f"≤{settings.TICKET_DAILY_REFUND_LIMIT} 元"
            if int(settings.TICKET_DAILY_REFUND_LIMIT or 0) > 0
            else "不限（生产请设上限）"
        )
        + "，单工单成本 "
        + (
            f"≤{settings.TICKET_MAX_COST_PER_TICKET}"
            if float(settings.TICKET_MAX_COST_PER_TICKET or 0) > 0
            else "不限"
        )
        + f"，SLA {settings.TICKET_SLA_HOURS}h"
    )

    # 多 worker + 本地 sqlite 检查点 = 每个 worker 各开一条连接写同一个文件。
    # 挂起本身还能工作（文件在那儿），但两个 worker 同时恢复同一条线程会互相覆盖，
    # 症状是"点了同意，图又回到挂起前"。和向量库那条一样按 WEB_CONCURRENCY 给 hint：
    # 这个 env 不是每种起法都设，误报只多一条告警，漏报由部署文档兜。
    try:
        workers = int(os.environ.get("WEB_CONCURRENCY", "1"))
    except ValueError:
        workers = 1
    if workers > 1:
        print(
            f"  ⚠ WEB_CONCURRENCY={workers}（多 worker）而工单检查点是本地文件 —— "
            "多个进程写同一个 sqlite 会让挂起的工单在被恢复时互相覆盖。"
            "多实例部署请换成共享存储上的检查点后端的（Postgres saver）。"
        )


@app.on_event("startup")
async def startup():
    """启动自检：配置、模板、依赖、滞留数据，然后把**实际生效**的开关打出来。

    交付前最该拦的事故有两类：拿示例配置（占位密钥 / 示例库口令 / 无鉴权向量库）
    上生产，以及**开着 Agent 却没有治理护栏**。前者由 security_preflight 拒绝启动，
    后者在下面的工单行里必须看得见——限额数字没打出来，"当日退款上限其实没配"这种
    配置就会一直活到第一笔超额退款。
    """
    security_preflight.enforce(settings, logger=logger)
    init_db()
    # 提示词模板有问题（占位符对不上、条件段没闭合、默认版本已归档）就在这里
    # 起不来，而不是等第一个用户提问时才在 500 里暴露。
    prompt_library.validate()
    # skill 同理：缺 frontmatter、目录名和 name 不一致、正文空——宁可在这里起不来，
    # 也不要等第一个用户提问才发现某份 SOP 静默地从索引里消失了（不报错，
    # 只是永远不被选中）。
    skill_library.validate()
    _check_ingest_backend()
    _check_multiworker_vector_store()
    _adopt_orphaned_documents()
    _recover_stalled_documents()
    _describe_ticket_agent()
    # 走 resolve_version：这一行是排查提示词问题的第一现场，打出来的必须是
    # 真正生效的版本，而不是配置项的字面值（留空时那是空串）。
    print(
        "Ticket prompts: understand/"
        + prompt_library.resolve_version("ticket_understand")
        + f", agent/{prompt_library.resolve_version('ticket_agent')}"
        + f", plan/{prompt_library.resolve_version('ticket_plan')}"
    )
    print(
        "SOP skills: "
        + (
            f"{len(skill_library.builtin())} 份内置"
            if skill_library.enabled()
            else "off（模型看不到任何作业指导）"
        )
    )
    # 这几项都会改变循环行为，而它们的效果在界面上看不见：重复拦截只体现在少跑一次
    # 工具，委派只体现在多了一整个子代理循环。启动时打出来，排查"为什么这次和上次
    # 不一样"时不用去翻 .env。
    print(
        "Repeat guard: "
        + (
            f"同一 (工具, 参数) 上限 {settings.AGENT_REPEAT_LIMIT} 次"
            if settings.AGENT_REPEAT_LIMIT > 0
            else "off（不检测重复调用）"
        )
    )
    print(
        "Delegation: "
        + subagent.describe_mode()
        + (
            f"，每张工单 ≤{settings.AGENT_MAX_DELEGATIONS} 次"
            if subagent.enabled()
            else ""
        )
    )
    print(f"Structured output retries: {settings.STRUCTURED_OUTPUT_RETRIES}")
    # 摄取与检索这几项同样"改变行为但界面上看不见":清洗关掉只表现为某些文档
    # 检索不到,重排换后端只表现为顺序不同,向量库降级更是完全无声。
    print(
        "Ingest: clean="
        + ("on" if settings.INGEST_CLEAN else "off（对照组）")
        + f", pdf={'pdfplumber' if settings.INGEST_PDF_STRUCTURE else 'pypdf2'}"
        + f", 编码先验={settings.INGEST_ENCODING_HINTS or '(无)'}"
        + f", 自检={'on' if settings.INGEST_SELF_CHECK else 'off'}"
    )
    print(f"Chunking: {settings.CHUNK_STRATEGY} (max {settings.CHUNK_MAX_TOKENS} tokens)")
    _print_rerank_mode()
    _print_vector_store()
    if settings.RAG_HYDE or settings.RAG_QUERY_ROUTE:
        print(
            "Query rewriting: "
            + ", ".join(
                filter(
                    None,
                    [
                        "HyDE（假答案只喂稠密通道）" if settings.RAG_HYDE else "",
                        "路由（按意图调 RRF 权重）" if settings.RAG_QUERY_ROUTE else "",
                    ],
                )
            )
        )
    _start_outbox_drain()
    print(f"Ticket Agent API is running on: http://localhost:{settings.PORT}")


def _start_outbox_drain() -> None:
    """起自动排空循环，并把它的状态打在启动行里。

    这一行必须看得见：队列"有没有人管"直接决定客户收不收得到回话，而它默认是关的。
    关着的时候 ``deliver`` 全仓库只有一个调用方——治理台上管理员手动点的那一下。

    通道状态和开关状态要**分开**打。只说"on"是不够的：配了循环但没接通道时，
    那个循环每 30 秒醒来一次什么也不发，而启动日志读起来像这件事已经解决了。
    """
    from services.ticket import outbox as ticket_outbox, senders as ticket_senders

    channel = ticket_senders.build_sender()
    print(f"Outbound channel: {ticket_senders.describe()}")

    if not (settings.TICKET_AGENT_ENABLED and settings.TICKET_OUTBOX_AUTODRAIN):
        app.state.outbox_drain = None
        print(
            "Outbox autodrain: off（待发队列只在有人点治理台那个按钮时才会被排空）"
        )
        return
    if channel is None:
        # 开关开着却没有通道：循环只会反复空转。仍然起它（接上通道的瞬间就生效），
        # 但这一行要说清楚现在一条都发不出去。
        print("  ⚠ Outbox autodrain 开着但没有出口通道 —— 队列不会被发送。")

    app.state.outbox_drain = asyncio.create_task(ticket_outbox.run_autodrain())
    print(
        f"Outbox autodrain: on（每 {settings.TICKET_OUTBOX_DRAIN_INTERVAL_SECONDS}s"
        f" 一批，每批 ≤{settings.TICKET_OUTBOX_DRAIN_BATCH} 条"
        + (f"，只发 {'、'.join(sorted(channel.channels))}" if channel else "")
        + "）"
    )


@app.on_event("shutdown")
async def shutdown():
    """停掉后台循环。

    不停的话它会在事件循环关闭时才被取消，测试里表现为一条 "Task was destroyed but
    it is pending" ——而那条警告会让人以为是自己新加的哪个测试有问题。
    """
    task = getattr(app.state, "outbox_drain", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    app.state.outbox_drain = None


def _print_rerank_mode() -> None:
    mode = retriever.rerank_mode()
    if mode == "off":
        print("Rerank: off")
        return
    if mode == "api" and not rerank_client.configured:
        # 不静默退回 llm：那会让"api 比 llm 好多少"这个对比测的是同一个东西
        print(
            f"  警告：RAG_RERANK_MODE=api 但 rerank 接口未配置"
            f"（endpoint={rerank_client.endpoint}），重排会退回融合序、等于没开。"
        )
        return
    detail = (
        f"cross-encoder {settings.RERANK_MODEL} @ {rerank_client.endpoint}"
        if mode == "api"
        else f"LLM listwise（{settings.utility_model}，对照组）"
    )
    print(f"Rerank: {mode} — {detail}，候选 {settings.RAG_RERANK_CANDIDATES}")


def _print_vector_store() -> None:
    """打印**实际**生效的后端，而不是配置里写的那个。

    这两件事会不一致：配置写着 qdrant 但服务连不上时已经降级回 memory。而降级
    是完全无声的——检索照样有结果（走的是进程内索引），只是"多 worker 共享、
    重启不丢"这些收益一个都没有。不打出来的话没人会发现。
    """
    configured = (settings.VECTOR_STORE or "memory").strip().lower()
    actual = "qdrant" if vector_store.uses_qdrant() else "memory"
    if actual == "qdrant":
        print(
            f"Vector store: qdrant @ {settings.QDRANT_URL}"
            f" (collection={settings.QDRANT_COLLECTION},"
            f" M={settings.VECTOR_HNSW_M}, efC={settings.VECTOR_HNSW_EF_CONSTRUCT},"
            f" efS={settings.VECTOR_HNSW_EF_SEARCH})"
        )
        return
    ann = (settings.VECTOR_ANN or "exact").strip().lower()
    suffix = (
        f"HNSW(M={settings.VECTOR_HNSW_M}, efS={settings.VECTOR_HNSW_EF_SEARCH})"
        if ann == "hnsw"
        else "精确检索"
    )
    print(f"Vector store: memory — {suffix}，多 worker 部署时每个 worker 各建一份")
    if configured == "qdrant":
        print(
            "  警告：VECTOR_STORE=qdrant 但已降级回进程内索引。"
            "先 docker compose -f docker-compose.qdrant.yml up -d，"
            "再 python scripts/backfill_qdrant.py。"
        )


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=settings.PORT)
