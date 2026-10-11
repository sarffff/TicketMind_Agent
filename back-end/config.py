"""
应用配置模块
使用 pydantic-settings 自动从环境变量和 .env 文件读取配置
"""

import os
from typing import Optional

from pydantic_settings import BaseSettings


_DEFAULT_JWT_KEY = "your-secret-key-change-this-in-production"


class Settings(BaseSettings):
    """
    应用配置类
    """
    # ========== 数据库配置 ==========
    DATABASE_URL: str = "mysql+pymysql://root:password@localhost:3306/ai_workspace_py"

    # ========== 服务器配置 ==========
    PORT: int = 3000
    ENV: str = "dev"  # dev / production

    # ========== LLM API 配置 ==========
    LLM_API_KEY: str = "your_api_key_here"
    LLM_BASE_URL: str = "https://open.bigmodel.cn/api/paas/v4/"
    LLM_MODEL: str = "glm-4.6v"
    # 流式请求附带 stream_options.include_usage 以拿到真实 token 用量。
    # 部分 OpenAI 兼容端点不认这个参数,被拒一次后会自动停发并改用本地估算。
    LLM_STREAM_USAGE: bool = True

    # ========== 模型路由 ==========
    # 辅助任务(历史摘要/查询改写/重排/指代消解/记忆抽取)用的便宜模型。
    # 留空回退 LLM_MODEL。这些任务对推理能力要求低、调用频次高,与主回答
    # 同价是纯浪费——埋点里的 purpose 字段早就区分了用途,缺的只是这张路由表。
    LLM_UTILITY_MODEL: str = ""
    # 裁判模型。LLM-as-judge 的底线是裁判与被评模型分开:同一个模型给自己打分
    # 存在系统性的自我偏好(self-preference bias),变体对比的结论会被污染。
    # 留空依次回退 LLM_UTILITY_MODEL / LLM_MODEL。
    JUDGE_MODEL: str = ""
    # 主回答的备用模型链(逗号分隔,按顺序试)。留空 = 没有降级路径,与改动前逐位相同。
    #
    # 补的是这个缺口:辅助调用(重排/HyDE/摘要)全都有降级路径,唯独**主回答**没有
    # ——提供商对某个模型限流(429)、那个模型临时 5xx、或模型名被下线(404)时,
    # 用户直接看到报错。配上备用模型后,开流之前那一次失败会自动换下一个模型重试。
    #
    # 边界(刻意的):
    # - **只在开流之前切换。** 一旦有字节流给了用户就不再切,否则备用模型会把主模型
    #   已经说过的话再说一遍。这与 LLM_CHAT_MAX_RETRIES 的重试窗口是同一个。
    # - **同一个 endpoint(共用 LLM_BASE_URL / LLM_API_KEY)。** 换的是模型名,不是提供商。
    #   它挡的是单模型级的限流与故障;整个提供商宕机需要跨提供商凭证,那是另一件事。
    # - **只对可切换的错误降级**(429 / 5xx / 超时 / 连接失败 / 404)。400 / 401 / 403
    #   这类请求本身的错,换个同端点的模型也修不好,直接抛,不白白多等一次。
    LLM_FALLBACK_MODELS: str = ""

    # 跨提供商故障切换：整个提供商宕机（base_url / key 级，而非单模型 429/404）时的
    # 备用。上面的 LLM_FALLBACK_MODELS 换的是**同一个 endpoint 上的模型名**，挡不住
    # "整个 open.bigmodel.cn 连不上"——那需要另一组凭证指向另一家。
    #
    # 值是一个 JSON 数组，按顺序尝试；每项 {base_url?, api_key?, model}：
    #   model    必填——那家提供商上要用的模型名
    #   base_url 省略则沿用 LLM_BASE_URL；api_key 省略则沿用 LLM_API_KEY
    # 例（换到 SiliconFlow 兜底）：
    #   LLM_FALLBACK_PROVIDERS=[{"base_url":"https://api.siliconflow.cn/v1","api_key":"sk-xxx","model":"deepseek-ai/DeepSeek-V3"}]
    #
    # 顺序：主模型 → LLM_FALLBACK_MODELS（同端点换名）→ 这里的跨提供商项。只在开流
    # 之前、且只对可切换的错（429 / 5xx / 超时 / 连接失败 / 404）降级，与
    # LLM_FALLBACK_MODELS 同一套判据。JSON 解析失败只告警并忽略（退回不配跨提供商），
    # 不拖垮启动。
    #
    # ⚠ 成本归因：trace / 定价按**模型名**查价目表、无 provider 维度。两家用同一个
    # 模型名但价格不同的话，成本列分不开它们（span 会带 fallback_provider 标出切换，
    # 便于排查）。
    LLM_FALLBACK_PROVIDERS: str = ""

    # ========== Redis 配置 (可选) ==========
    REDIS_URL: Optional[str] = None

    # ========== Embedding 配置 ==========
    # 独立的 Embedding API 配置;留空则回退到 LLM_API_KEY / LLM_BASE_URL
    EMBEDDING_API_KEY: str = ""
    EMBEDDING_BASE_URL: str = ""
    EMBEDDING_MODEL: str = "embedding-2"
    # 单次 embeddings 请求最多提交多少条文本,长文档分批发送
    EMBEDDING_BATCH_SIZE: int = 32
    RAG_MIN_SCORE: float = 0.3
    RAG_TOP_K: int = 5

    # 同一个 (工具, 参数) 在一次回答里最多执行几次,第 N 次起不再执行,改为回灌
    # 一句纠正说明。轮次上限和字符预算都管不到这件事:重复调用每次都是合法调用、
    # 都在预算内,只是拿回来的东西一模一样。0 表示关闭检测(退回改动前的行为)。
    #
    # 为什么算总次数而不是"连续三次":A、B、A、B、A 这种在两个相同查询之间来回
    # 摆的情况是同一种病,而只看连续完全抓不到它。
    AGENT_REPEAT_LIMIT: int = 3

    # ========== 多代理协作（委派） ==========
    # off        : 单代理,与此前逐位相同(默认)
    # augment    : 主代理保留全部工具,额外多一个 delegate。它可以自己做也可以派人,
    #              于是"什么时候值得委派"由模型判断——这是能看出委派有没有用的模式。
    # supervisor : 查询类工具从主代理手里收走,只留 delegate、写操作与 load_skill。
    #              分工更干净——所有事实都经 inquiry/policy 两个子代理之手,
    #              代价是简单问题也得多付一次生成。
    #
    #              写操作**任何模式都不进子代理工具面**：审批闸门在主循环按
    #              proposals 判,而子代理跑在一个工具处理器里面,委派出去的执行
    #              不过人审（详见 agent_roles 模块文档）。
    #
    # 默认 off 不是保守:委派会把一次工单的模型调用次数变成不确定的(每次委派多一次
    # 完整的子代理循环),成本与延迟都随之上升。开之前应该先在 traces 里看清
    # 单代理模式下究竟是哪一步不够用。
    AGENT_DELEGATION_MODE: str = "off"

    # 一张工单最多委派几次。子代理内部的工具调用**也**计入
    # ``TICKET_MAX_TOOL_CALLS``（见 graph 的 execute 记账）,所以这个上限管的是
    # "另起几个独立上下文"——那才是成本与延迟的真正来源:每次委派是一整个
    # 3~5 轮的循环,而它烧的钱不体现在主循环的轮次里。
    # 没有上限时,一个"稳妥"的模型能把索引里每个角色都派一遍再开始办事。
    AGENT_MAX_DELEGATIONS: int = 2

    # 子代理每次工具结果回灌给它的字符上限。主代理的工具结果整条进 messages,
    # 而子代理的那些会连同它的报告一起回到主代理上下文里——不加一道截断,
    # 一次委派就能把三张订单的全文带回来,而主代理要的只是那一句结论。
    AGENT_SUBAGENT_RESULT_MAX_CHARS: int = 4000

    # 单个页面允许读取的字节上限（超出即判失败）与注入上下文的字符上限
    WEB_FETCH_MAX_BYTES: int = 200 * 1024
    WEB_FETCH_TIMEOUT_SECONDS: float = 10.0

    # ========== Skill（作业指导） ==========
    # 一份 skill 回答"这件事在本组织该怎么做"——报销怎么审、季度报告怎么写。
    # 它是**指令**不是工具:加载之后由当前这个代理照着做,不带独立的执行上下文
    # (那是子代理角色的事,见 agent_roles 与 skill_library 的模块文档)。
    #
    # 两层:内置(back-end/skills/ 跟代码版本化,可带附带文件)+ 工作区
    # (workspace_skills 表,admin 在界面上写,不改代码不重启)。同名时工作区盖内置。
    #
    # 索引(名字 + 一句描述)进独立 system 消息,正文由模型调 load_skill 按需取。
    # 不全量注入:一个企业几十个 SOP 全塞进去的话每轮都要付这笔固定成本,
    # 而其中至多一个和当前问题有关。
    SKILL_ENABLED: bool = False
    # 一回合最多加载几份。没有上限时模型会把索引里每一个都加载一遍再开始干活,
    # 那是最贵的一种"稳妥"。
    SKILL_MAX_LOADS: int = 3
    # 索引里最多列几条。超出时如实说明被截断——不说的话模型会以为清单是完整的
    SKILL_INDEX_MAX_ITEMS: int = 40
    # 单份 skill 正文的字符上限(写入时校验)。给到 8000 而不是更小:
    # 一份完整的审核流程带上判据与例外,几千字是正常的
    SKILL_MAX_CHARS: int = 8000
    # 附带文件注入上下文的字符上限。比 FS_READ_MAX_CHARS 略大:
    # 模板类文件通常要完整看,截一半的模板不如不给
    SKILL_ATTACHMENT_MAX_CHARS: int = 4000



    # ========== 摄取层清洗 ==========
    # 脏输入不是"质量差一点",它会让召回通道整条失效:GBK 文档被 errors="replace"
    # 变成一串 U+FFFD 之后,retrieval_index.tokenize() 两个正则都匹配不到,BM25
    # 建索引时直接跳过整块——稀疏通道彻底看不见这篇文档,而状态还是 indexed。
    # 关掉即退回改动前的行为(硬 utf-8 解码 + PyPDF2 纯文本抽取),作为对照组。
    INGEST_CLEAN: bool = True
    # 非 utf-8 文档的解码先验(逗号分隔,按顺序严格试)。
    #
    # 为什么需要先验而不是纯靠嗅探:同一串中文字节在 GB18030 / EUC-KR / Shift-JIS
    # 下**都能严格解通**,从字节本身分辨不了,任何检测器都不行——实测一段 GBK 短句
    # 会被 charset-normalizer 判成 EUC-KR,解出来是一串谚文。而猜错编码时解出的文本
    # 没有任何替换符,INGEST_MIN_TEXT_RATIO 那道自检也抓不到它。
    #
    # gb18030 是 GBK / GB2312 的超集,一条就够。代价说清楚:一份真正的韩文文档会被
    # 当成中文解错。对这个项目(中文语料、中文用户)这是正确的取舍,但它是取舍。
    INGEST_ENCODING_HINTS: str = "gb18030"
    # 用 pdfplumber 的字号与坐标恢复标题层级、剔页眉页脚、修词内空格。
    # 这是让 chunking 那四件事对 PDF 重新生效的唯一途径:PyPDF2 只给字符串,
    # 没有字号,标题层级怎么正则都推不出来。关掉则走 PyPDF2 纯文本路径。
    INGEST_PDF_STRUCTURE: bool = True
    # 可读字符占比(1 − U+FFFD 替换符占比)低于此值即判 failed。
    #
    # 2026-08-23 从 0.6 提到 0.9。原来的理由是"真正的编码错误会低到接近 0",
    # **实测不成立**:GBK 文档硬解 utf-8 之后 ratio 落在 0.2755–0.6547,上界取决于
    # 文档里有多少 ASCII(代码标识符、数字、表格竖线全都完好无损)。技术文档 ASCII
    # 多,于是中文全毁也能拿到 0.65。有一篇正好 0.6011,**过了 0.6 这道门**——
    # 状态是 indexed、界面上和正常文档没差别,而 BM25 只切出 11 个 token(原文 47)
    # 且全是 ASCII 残留,中文查询一个字都命中不了。
    #
    # 提到 0.9 是安全的:U+FFFD 在真实内容里没有任何合理来源,出现一个就说明解码
    # 坏了。实测正常语料、pdf_like、noisy_unicode 三类**全是 1.0000**,与坏文档
    # 之间是大片空白,0.9 落在空白中间,零误伤。
    #
    # 它抓不到的两类要知道:(1) 编码猜错但解通的文本没有替换符(见
    # INGEST_ENCODING_HINTS);(2) 扫描件 ratio 也是 1.0000 但 token 为 0,
    # 由 index_document 的 no_chunks 自检兜。
    INGEST_MIN_TEXT_RATIO: float = 0.9
    # 页眉页脚的判据是"在至少这么多页的相同边缘区域重复出现"。
    # 按位置单独判会把首页正文第一行也剔掉,所以必须要有跨页重复这个条件。
    INGEST_HEADER_FOOTER_MIN_PAGES: int = 3
    # 索引完成后随机抽一块做一次自检索,命中不了自己就记一条 warning。
    # 这是把"索引成功"的判据从"没抛异常"换成"真的检索得到"——静默失败的那几种
    # (空文本、乱码、维度不匹配)全都不抛异常。代价是每篇文档多一次 embedding 调用。
    INGEST_SELF_CHECK: bool = True


    # ---- 入库任务队列（持久化，替代进程内 fire-and-forget）----
    # 改动前上传后的索引是 FastAPI BackgroundTasks：同事件循环、不落盘、无并发上限、
    # 无进度、无重试。进程在索引中途重启那篇文档就永远卡在 processing。这里换成一张
    # document_jobs 表 + 认领/租约/重试，照搬 agent_runs 的 reaper 模式（惰性挂在读
    # 路径上，项目里没有调度器）。幂等已满足（index_document 删后插），至少一次投递安全。
    # 一个进程里最多同时索引几篇。索引慢在 embedding（N 次网络调用），并发太高会打爆
    # embedding 端点的速率；2 是保守默认。
    INGEST_QUEUE_CONCURRENCY: int = 2
    # 认领一篇后租约多久过期。超过没完成即判为"认领它的进程死了"，重新入队。给得宽：
    # 一篇大文档的 embedding 可能要几分钟，比"一篇真的可能花多久"宽才不会把正在跑的
    # 任务误判成死锁（同 orphan_timeout 的取舍）。
    INGEST_QUEUE_LEASE_SECONDS: float = 600.0
    # 一篇最多尝试几次。瞬时失败（embedding 端点抖动）值得重试；到顶仍失败即落 failed。
    INGEST_QUEUE_MAX_ATTEMPTS: int = 3
    # 重试退避基数（秒），实际等待 = 基数 × 已尝试次数。避免 embedding 端点挂了之后
    # 立刻原地重试、把失败放大成一个紧循环。
    INGEST_QUEUE_BACKOFF_SECONDS: float = 30.0
    # 评估语料的降级方式:none | pdf_like | gbk_bytes | scanned(见 eval/corpus_degrade.py)。
    # 只影响离线评估,线上永远是 none。
    #
    # 它存在的理由是一个盲区:eval/corpus/ 下 6 篇是自造的干净 utf-8 Markdown,
    # 于是这套评估**量不出任何清洗改动的价值**——清洗代码根本没有输入可清。
    # 降级把干净语料变成"真实上传"的样子,再和清洗开关配对跑,差值就是清洗值多少。
    EVAL_CORPUS_DEGRADE: str = "none"

    # ========== 分块配置 ==========
    # token 计数器: heuristic(零依赖估算) | tiktoken(精确,需额外安装且首次会下载词表)
    TOKEN_COUNTER: str = "heuristic"
    CHUNK_MAX_TOKENS: int = 320
    # 仅在单个超长块被硬切时生效;跨段落上下文由检索阶段的邻域扩展补全
    CHUNK_OVERLAP_TOKENS: int = 40
    # 分块策略: structural | semantic
    #   structural — 按 Markdown 结构(标题层级、代码围栏、段落)切,默认,零成本。
    #   semantic   — 按句切开后算相邻句向量的余弦距离,在距离突变处断开。
    #                好处是话题真正转折的地方才断,而不是恰好写了个空行的地方;
    #                代价是入库时每篇文档多一批 embedding 调用。
    #
    # 为什么不做 late chunking:那需要 token 级 hidden states 再按块池化,而
    # /embeddings 每条输入只返回一个池化后的向量,hosted API 拿不到 token 级输出。
    CHUNK_STRATEGY: str = "structural"
    # 相邻句距离取这个分位数作断点阈值。95 表示只在最"跳"的那 5% 处断开。
    # 用分位数而不是绝对阈值:余弦距离的绝对值随 embedding 模型变,换个模型
    # 绝对阈值就得重调,而分位数是自适应的。
    CHUNK_SEMANTIC_PERCENTILE: float = 95.0
    # 语义分块的最小句数。太短的文档没有可统计的距离分布,直接走 structural
    CHUNK_SEMANTIC_MIN_SENTENCES: int = 6

    # ========== 检索管线 ==========
    # 稠密向量 + BM25 双路召回后用 RRF 融合。关闭则退化为纯向量检索,便于对照
    RAG_HYBRID: bool = True
    # 每条召回通道各取多少候选进入融合
    RAG_CANDIDATES_PER_CHANNEL: int = 20
    # 命中分块前后各带几个相邻分块,补全被切断的上下文(0 表示关闭)
    RAG_CONTEXT_WINDOW: int = 1
    # 多查询改写:提召回,代价是每次检索多一次模型调用
    RAG_MULTI_QUERY: bool = False
    RAG_MULTI_QUERY_COUNT: int = 2
    # LLM listwise 重排:提精度,代价是每次检索多一次模型调用
    RAG_RERANK: bool = False
    RAG_RERANK_CANDIDATES: int = 20
    RAG_RERANK_SNIPPET_CHARS: int = 500
    # 重排方式: off | llm | api
    #   llm — 现有的 LLM listwise:把候选编号列给通用模型让它排序。留着当对照组,
    #         能量出"专用 cross-encoder 比让通用模型排序好多少"。
    #   api — 专用 rerank 接口。query 与 document 拼在一起过一遍模型输出标量
    #         相关度,这是它比稠密检索准的原因:稠密是 bi-encoder,两侧各自独立
    #         编码,编码时从未见过对方。
    # 留空则按 RAG_RERANK 布尔量决定(True → llm),保持现有变体与测试不变。
    RAG_RERANK_MODE: str = ""
    RERANK_MODEL: str = "rerank"
    # 留空回退 LLM_BASE_URL / LLM_API_KEY —— 同一家提供商时 rerank 与对话共用凭证。
    #
    # ⚠️ 2026-08-23 实测:智谱 /rerank 返 **429 / code 1113**(账号无该项额度),
    # 而 chat 用同一个 key 是 200。诊断方式是换个模型名对比——`rerank` 报
    # 429/1113、`rerank-2` 报 400/1211(模型不存在),说明模型名对、是额度问题。
    # 后果:mode=api 在智谱上等于**静默降级成融合序**,报告里看着就是"专用重排
    # 没有增益"。rerank-api 变体因此长期从未真正执行过。
    #
    # 可用的替代:SiliconFlow 的 BAAI/bge-reranker-v2-m3,非 Pro 层免费。配法是
    #     RERANK_BASE_URL=https://api.siliconflow.cn/v1
    #     RERANK_API_KEY=<SiliconFlow key>
    #     RERANK_MODEL=BAAI/bge-reranker-v2-m3
    # 不把它写成默认值:默认值指向一个需要另一家凭证的服务,没配 key 的人会从
    # "不启用重排"变成"启用了但每次请求都 401"。默认仍是同源回退,由 .env 决定。
    RERANK_BASE_URL: str = ""
    RERANK_API_KEY: str = ""
    RERANK_TIMEOUT_SECONDS: float = 10.0

    # 辅助模型调用（重排 / HyDE / 多查询改写 / 摘要）的超时与重试上限。
    #
    # 2026-08-27 加。此前 ``AsyncOpenAI`` 是不带这两个参数构造的，于是吃 SDK 默认：
    # **read=600s、max_retries=2 → 最坏一次调用 1800 秒**。实测 rerank 变体
    # p90 延迟 255 秒、最大 336 秒（baseline 是 37 秒），那个 336 就是重试链。
    #
    # 为什么辅助调用要单独设一个更短的值：它们**全都有降级路径**——重排失败退回
    # 融合序、HyDE 失败用原查询。为一个可以放弃的增强等 10 分钟是纯亏，而且拖慢
    # 的是用户正在等的那次回答。主回答的调用不受这个约束（用户确实在等它）。
    #
    # 60 秒的依据：实测一次 20 候选的 listwise 重排约 32 秒成功，60 秒给一倍余量。
    # 重试 1 次而不是 2：辅助调用失败退回降级路径比多试一次更划算。
    LLM_AUXILIARY_TIMEOUT_SECONDS: float = 60.0
    LLM_AUXILIARY_MAX_RETRIES: int = 1

    # 主回答调用的上界。2026-08-29 加。
    #
    # 上面那条注释说「主回答的调用不受这个约束(用户确实在等它)」——那个判断对,
    # 但它得出的结论应该是「给一个更宽松的超时」,而不是「不给超时」。不给的结果是
    # 落回 SDK 默认 600s × 3 = **最坏 1800 秒**,而这条路径上挂着的东西比辅助调用
    # 多得多:一个数据库会话、一条 SSE 连接、以及 Agent 循环最多 6 轮里的这一轮。
    # 连接池打满就是全站不可用,而不只是这一次回答变慢。
    #
    # **流式下这个值的语义是「两个分片之间最多等多久」,不是整条流的总时长。**
    # httpx 的 read timeout 在每次读之间重置,所以正常输出的长回答不会被这个值切断
    # (每个分片都在刷新计时),真正被它拦住的是「连上了但不再吐字节」的挂死连接
    # ——那恰好是我们要防的。整条流的总时长另有 AGENT_MAX_TOOL_ROUNDS 与
    # 结果字符预算兜着。
    #
    # 120 秒的依据:实测 baseline 一次对话调用 p90 约 37 秒(见上面 EVAL 那段的实测
    # 数据),给三倍余量。带思考链的推理模型首字延迟更长,所以不取更小的值。
    #
    # 重试 2 次:与辅助调用不同,主回答**没有降级路径**——失败就是用户看到报错,
    # 所以这里值得多试。安全性见 model_adapter._open_stream 的注释:重试发生在
    # 开流之前,不会让用户看到重复内容。
    LLM_CHAT_TIMEOUT_SECONDS: float = 120.0
    LLM_CHAT_MAX_RETRIES: int = 2

    # ---- 评估链路的限流与 429 退避(eval/runner) -------------------------------
    #
    # 2026-08-27 那轮 3 变体评估打了 **211 次 HTTP 429**:评估一次性在几分钟内
    # 打出几百次对话调用(~问题数×变体数×2,外加重试),没有真实用户节奏兜底,
    # 很容易把账号的每分钟配额打满。线上靠人肉节奏天然散开,评估没有——所以
    # 在这里显式节流,而不是去改 LLM_AUXILIARY_*(那会影响线上辅助调用)。
    #
    # 这三个开关只作用于 eval/runner 里的 _LLMRateGate:评估跑完不再需要,
    # 设默认值即可,不设也不会影响线上路径。
    #
    # min_interval:每次 LLM 调用之间的最小间隔。0 关闭(不加间隔)。
    #   它的作用是**防突发**,不是限速——每次调用本身要花好几秒,间隔只在
    #   配额已耗尽(服务端秒拒)时才真正起作用。
    # cooldown:撞上 429 且服务端没给 Retry-After 时,退避多少秒再重试。
    # max_retries:撞上 429 后重试几次(含按配额窗口退避)。默认 12 次是给
    #   一分钟配额窗口留下足够余量;retries 是给「重试次数」的兜底。
    EVAL_LLM_MIN_INTERVAL_SECONDS: float = 0.5
    EVAL_LLM_RATE_LIMIT_COOLDOWN_SECONDS: float = 8.0
    EVAL_LLM_MAX_RATE_RETRIES: int = 12

    # HyDE(Hypothetical Document Embeddings):先让辅助模型编一段假答案,拿它去
    # 检索。反直觉但有效——用户的问题和文档的措辞常常不在同一个语域("报销要几天"
    # vs "费用审批时限"),而一段假答案的措辞天然更接近文档。
    #
    # 关键实现约束:假答案**只喂稠密通道**,BM25 继续用原始 query。两路都换等于
    # 亲手废掉稀疏通道:假答案里的专有名词、编号、错别字全是模型编的,拿它做字面
    # 匹配只会命中一堆无关内容。
    RAG_HYDE: bool = False
    # 实测原值 200 不够:HyDE 的正确输出只有 88 字,但思考先把 200 花光,返回空串,
    # 于是 hyde 变体与 baseline 逐位相同。见下面那组"辅助调用的输出预算"。
    RAG_HYDE_MAX_TOKENS: int = 2048
    # 查询路由:让辅助模型判断这个查询偏字面还是偏语义,据此调整两路的 RRF 权重。
    # eval 数据集的 probe 标注(lexical / paraphrase / table_lookup ...)天生就是
    # 这个分类器的标注集,所以它的准确率是可测的,不用凭感觉。
    RAG_QUERY_ROUTE: bool = False
    # RRF 的默认通道权重。路由关闭时两路都是 1.0,与改动前逐位相同。
    RAG_RRF_DENSE_WEIGHT: float = 1.0
    RAG_RRF_SPARSE_WEIGHT: float = 1.0
    # 路由判定为偏字面/偏语义时,弱侧通道的权重降到多少
    RAG_ROUTE_WEAK_WEIGHT: float = 0.4

    # ---------- 辅助调用的输出预算 ----------
    # 这一组存在的理由是两次实测,而不是"参数最好都可配"。
    #
    # 第一次(scripts/probe_structured_budgets.py):量全库 7 个辅助模型调用点,
    # 同一段提示词跑现有预算和 2048 两次,**5 个在现有预算下 100% 返回空串**。
    # 原因是 glm-4.5-air 这类混合推理模型会先花 max_tokens 思考,预算不够时
    # 一个字都不吐——不是截断到一半的 JSON,是空串。
    #
    # 空串之后的链路每一环都"合理":解析不出 JSON → 调用方按"这是增强不是依赖"
    # 静默降级 → 没有日志。叠起来的结果是这四个检索增强从来没有执行过,而
    # eval/variants.py 里对应的 5 个变体与 baseline 逐位相同——报告上读作
    # "这个技术没有增益",真相是它没跑。
    #
    # 第二次(scripts/sweep_worst_case_budgets.py):第一次的结论是**假绿灯**。
    # 那个探针用玩具输入(一个短问题、5 个候选),而思考开销跟着输入长度涨。
    # 按各自的输入上界重量之后,统一配 1024 的三个调用点仍然 100% 失败:
    #
    #   rerank         提示词 10241 字(20 候选 × 500)  最低 3072
    #   query_condense 提示词  2653 字(6 轮 × 400)     最低 2048
    #   history_summary提示词 15945 字(无硬截断!)      最低 2048
    #   memory_extract 提示词  6322 字(2000 + 4000)    最低 1024
    #
    # 教训不是"1024 不够",是**按典型输入定预算会漏掉配置放大的那一档**。
    # 所以下面每个值都留一倍余量:候选数、历史预算都是可配的,贴着最低值配
    # 等于把"改大那个配置"变成一个静默失效的开关。
    #
    # 为什么可以大方给:max_tokens 是**上界不是账单**。计费按真实输出 token,
    # 而失效那一侧的代价是"思考的 token 全额付费、拿回来一个空串"——所以
    # 宁大勿小在这里连成本权衡都不算。
    #
    # 现在这类失效不再无声:finish_reason=length 且正文为空会在 model_adapter
    # 里记 span 并打 warning(见 _record_truncation),structured 层再补一条
    # ``truncated`` 失败标签和 ``budget_exhausted`` 埋点。
    #
    # 这三个的输入是固定的短提示词(一个问题),量下来 1024 就够,但仍然给到
    # 2048 保持一致的余量——见上面"上界不是账单"。
    RAG_ROUTE_MAX_TOKENS: int = 2048
    RAG_MULTI_QUERY_MAX_TOKENS: int = 2048
    # 输入随 RAG_RERANK_CANDIDATES × RAG_RERANK_SNIPPET_CHARS 增长,是七个调用点里
    # 唯一输入会被配置放大的,也是唯一一个"按合成语料扫出来的下限不够用"的。
    #
    # 扫出来的最低值是 3072(20 候选 × 500 字的填充文本),按惯例给一倍余量配了
    # 6144。然后真实语料打回来了:2026-08-22 那轮 30 题 × 2 个重排变体里
    # **7 次仍然空串**(约 12%)。真实分块的信息密度比填充文本高,读 20 段要想的
    # 更多。所以这里按实测又翻了一倍。
    #
    # 这一条要记住的不是数字,是**输入会被配置放大的调用点必须按真实数据校准**,
    # 合成输入只能给下界。改动 RAG_RERANK_CANDIDATES 或 RAG_RERANK_SNIPPET_CHARS
    # 之后要重新跑一遍 scripts/sweep_worst_case_budgets.py,并盯住运行日志里
    # 有没有 "llm.rerank returned empty content"。
    RAG_RERANK_MAX_TOKENS: int = 12288

    # ========== 向量存储 ==========
    # memory | qdrant
    #
    # memory 是进程内 FAISS/numpy 索引:按工作区隔离、按签名失效、每次进程重启
    # 从 MySQL 重建。它的限制很具体——**多 worker 部署时每个 worker 各建一份**,
    # 于是一次上传之后哪个 worker 能检索到取决于请求打到了谁身上。
    #
    # qdrant 把向量搬成持久态:多 worker 共享、重启不丢、带 payload 过滤的 ANN。
    # 默认仍是 memory,切换是显式动作——它需要一个跑着的服务
    # (docker-compose.qdrant.yml),而"配置默认值悄悄要求一个外部依赖"是很坏的默认。
    VECTOR_STORE: str = "memory"
    # exact | hnsw。只作用于 memory 后端。
    #
    # exact 是暴力扫描(IndexFlatIP / numpy 矩阵乘),召回率恒为 100%。
    # hnsw 是近似最近邻,拿召回率换延迟——**在当前语料规模下它只会更差**:
    # 几千个向量的暴力扫描本来就是毫秒级,而 HNSW 引入了图构建开销和召回损失。
    # 它存在的意义是让"ANN 的代价"变成一个能量出来的数(recall@5 与 avgRetrievalMs
    # 一起看),而不是一句"到了大规模就该上 ANN"的口号。
    VECTOR_ANN: str = "exact"
    # HNSW 参数。M 是每个节点的出边数,ef_construction 是建图时的候选池大小,
    # ef_search 是查询时的候选池大小——三者都是"更大=更准更慢"。
    # 它们同时作用于 memory/hnsw 与 qdrant 两个后端,概念是同一套。
    VECTOR_HNSW_M: int = 16
    VECTOR_HNSW_EF_CONSTRUCT: int = 100
    VECTOR_HNSW_EF_SEARCH: int = 64

    QDRANT_URL: str = "http://localhost:6333"
    QDRANT_API_KEY: str = ""
    # 单 collection,workspace_id 存在 payload 里并建索引,查询时按它过滤。
    # 不做 collection-per-workspace:那会随用户数线性增长,而 Qdrant 的每个
    # collection 都有固定的内存与文件开销。
    QDRANT_COLLECTION: str = "ai_workspace_chunks"
    QDRANT_TIMEOUT_SECONDS: float = 5.0



    # ========== 安全护栏 ==========
    # 关闭后检索内容原样拼进提示词(只建议在排查护栏误报时临时关闭)
    GUARDRAIL_ENABLED: bool = True
    # 注入模式累计分数达到该值时,整段检索结果不再注入(0 = 只标记不拦截)。
    # 默认只观测:误报的表现是"明明有资料却答不出来",比漏报更难排查,
    # 先在 trace 里看一段时间命中情况再决定收紧到多少。
    GUARDRAIL_BLOCK_SCORE: int = 0

    # ========== 出站出口控制（SSRF + 白名单，见 services/egress.py） ==========
    # 模型可控的出站只有 fetch_web_page(URL 由模型给),它天然是 SSRF / 数据外泄入口。
    #
    # 拦私网/环回/链路本地/保留段(含云元数据 169.254.169.254)。默认开:正当的公网
    # 抓取不会指向这些地址,几乎无误报,而它挡的是"把抓网页变成探内网/读云凭证"这条
    # 经典手法。运营方自己配的 endpoint(LLM/embedding/rerank/web_search/Qdrant 的
    # host)不受此限——那是运营方选的、非模型可控,自建内网网关是常态。
    EGRESS_BLOCK_PRIVATE_IPS: bool = True
    # 额外放行的出站 host(逗号分隔,支持 *.example.com 通配)。
    # 空 = 只做上面的 SSRF 拦截、放行任意公网 host(fetch 对公网照常可用);
    # 非空 = 把 fetch_web_page 收紧到"运营方 endpoint + 这里列的 host",其余一律拒。
    # 想让联网抓取只能碰几个可信域时设它。
    EGRESS_ALLOWLIST: str = ""

    PROMPT_EVAL_ANSWER_VERSION: str = ""
    # 子代理提示词的版本。三个角色各自一项——共享一个开关就没法单独 A/B 某个
    # 角色,动一个会让另两个的结果一起失效。
    #
    # 没有这三项之前,``role_prompt()`` 唯一的出口是 SPECS 里的 default_version,
    # 也就是说新版本只能靠改源码才能生效,eval 变体扫不到它——``prompt_key``
    # 那套版本化机制是空转的。
    PROMPT_TICKET_SUB_INQUIRY_VERSION: str = ""
    PROMPT_TICKET_SUB_POLICY_VERSION: str = ""
    PROMPT_TICKET_SUB_REASSURANCE_VERSION: str = ""

    # ========== 结构化输出 ==========
    # 模型输出解析/校验失败时的重试次数。重试会把 Pydantic 的报错原文回灌给模型
    # 让它自己改——和工具参数校验失败时回灌 INVALID_ARGUMENTS 是同一个套路。
    # 0 表示不重试(解析失败即按各调用方的降级路径处理)。
    #
    # 默认只给 1 次:这些都是辅助任务(改写/重排/记忆抽取),失败的代价是"这次
    # 增强没生效",而不是回答出错。重试三次的钱花在主回答上更划算。
    STRUCTURED_OUTPUT_RETRIES: int = 1

    # ========== 工单域（客服 + 工单解决 Agent）==========
    # 整个工单域的总开关。默认关，与一切会改变状态的能力一致
    # （SKILL_ENABLED / AGENT_DELEGATION_MODE 都是关的）。
    #
    # 关着的时候工单路由不注册、业务工具不注册，知识库的行为逐位等价于
    # 加这套东西之前——这是本仓库每一个新能力的通式，不是形式主义：它让
    # "上线出问题时把开关拨回去"成为一条不需要回滚部署的动作。
    TICKET_AGENT_ENABLED: bool = False
    # 受理哪些渠道。逗号分隔，与 intake.CHANNELS 取交集（交集而不是直接采信配置：
    # 一个手抄成 ``emial`` 的值如果照单全收，工单会以一个不存在的渠道名建进库，
    # 之后所有按渠道筛选的指标都看不见它）。
    #
    # 默认只放已经有入口适配器的四个。wecom / phone 需要转写与企微回调，接上之后
    # 加进这里才有意义——在此之前列上它们也不会自己收到流量，但队列里会出现一个
    # "配置说支持、实际没人能提交"的渠道，而那种差别只有等到有人试了才发现。
    TICKET_CHANNELS: str = "web_chat,email,app,api"
    # 工单正文的字符上限。**超限明确报错，不静默截断**：被截掉的尾部往往是
    # 订单号与真正的诉求，而一张缺了订单号的工单会走到"查不到订单"的分支上，
    # 最后转人工——看起来安全，实际是我们把信息扔了。
    # 6000 约等于一封带完整历史引述的邮件投诉正文。
    TICKET_INTAKE_MAX_CHARS: int = 6000
    # 附件条数上限。真正的解析在摄取层，这里只挡量：正文里的文件名列表如果
    # 长到几百条，多半是转发邮件把整条线程拖进来了
    TICKET_INTAKE_MAX_ATTACHMENTS: int = 8
    # SLA 时限（小时），建单时据此写 sla_due_at。0 表示不设 SLA（列留空）。
    #
    # 为什么要有这一列：文档把"首次响应时间"和"平均处理时长"列为核心指标，
    # 而它们只有在**当时**记下的时限上才有意义——事后用一个统一标准去倒推，
    # 等于用今天的口径审判历史。
    TICKET_SLA_HOURS: int = 24
    # 退款金额的人审阈值（元）。达到它就把这张工单送进人审，不管是模型说"很确定"
    # 还是规则说"这就是个小额退款"。
    #
    # 为什么是钱而不是意图：意图判错顶多白跑一轮，退款判错是**真金白银打错账户**，
    # 而文档把"错误操作率（尤其是退款、改订单）"列为核心指标——那这个阈值就是
    # 指标的定义本身，不是一个可调的小数点。设 0 表示任何退款都要人批。
    TICKET_REFUND_REVIEW_THRESHOLD: float = 200.0
    # 法律与投诉关键词（逗号分隔）。命中即视为高风险：这类工单的错误代价不是钱，
    # 是法务与舆情，而那两样都不在 Agent 的判断范围里。
    #
    # 用关键词而不是让模型判断"有没有法律风险"：这一条要的是**宁可多转**。
    # 漏判一条律师函的代价远大于多一次人工过目，而模型对措辞的敏感度取决于
    # 它当天心情，关键词表是可以被 review、被追责的东西。
    TICKET_LEGAL_KEYWORDS: str = "律师,起诉,法院,仲裁,315,消协,工商,市场监管,曝光,媒体,维权"
    # 强负面情绪词（逗号分隔）。命中两条以上算"情绪极负面"，转人工——不是因为它
    # 高风险，而是因为这种客户这时候最不需要机器人回话。
    TICKET_NEGATIVE_KEYWORDS: str = "垃圾,太差,骗子,欺诈,愤怒,气死,无语,崩溃,投诉,差评,离谱"
    # 抽取走不走模型。关掉只留规则层（订单号/金额/意图/关键词），
    # 商品名与情绪判定就没有了——那是一个明确的降级面，而不是悄悄变笨。
    #
    # 它是"增强不是依赖"的那一类：调用失败会打 warning 并退回规则层结果，
    # 而规则层已经足够把工单正确地分到人手上。
    TICKET_UNDERSTAND_LLM: bool = True
    # 办单之前要不要先规划几步（文档§4 第 5 步）。
    #
    # 默认开：这份计划的主要读者不是模型（执行循环本来就能随机应变），
    # 而是审批收件箱和轨迹回放里的人——"它打算先查单再退款"写在工单上，
    # 人批那笔退款时才知道自己批的是第几步。
    TICKET_PLAN_ENABLED: bool = True
    # 计划步数上限。工单不是研究课题，超过 5 步的计划基本是在给简单问题硬凑步骤，
    # 白花一次调用的钱还不改变结果。
    TICKET_PLAN_MAX_STEPS: int = 5
    # 单张工单的默认工具调用次数上界（治理层与熔断用）。
    # 放在这里而不是只留在 governor 里：governor 行是可选的运营覆盖，
    # 而"没配过治理的工作区"也要有一个数。0 = 不限。
    TICKET_MAX_TOOL_CALLS: int = 12
    # 每个工作区每日退款总额上限（元）。按应用时区的自然日聚合，不是滚动 24 小时。
    #
    # 默认给一个很小的数是有意的：这套系统刚接上真实支付通道时，正确的姿态是
    # "几乎一切都过人审"，然后按观测到的错误操作率逐步放宽。默认值放宽等于
    # 在没有任何数据的时候假定 Agent 是对的。0 = 一律不许自动退。
    TICKET_DAILY_REFUND_LIMIT: float = 1000.0
    # 单工单的模型成本上限（元）。超了就地收尾并转人工——一张工单烧掉多少钱
    # 与它该不该被自动解决是两件不相干的事，不该让前者绑架后者。0 = 不限。
    TICKET_MAX_COST_PER_TICKET: float = 0.0
    # LangGraph 检查点文件的位置：挂在人审上的那张工单，状态就存在这里。
    #
    # 它是**运行数据**而不是配置，所以进 .gitignore；但它必须是一个活得比进程长的
    # 路径——放到临时目录上，等于把"跨天恢复"这条能力悄悄换成"重启即失忆"。
    # 多实例部署要换成共享存储（或 Postgres saver）：两个进程各写一份 sqlite，
    # 会得到两条互不知情的挂起线程。
    TICKET_CHECKPOINT_DB: str = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "ticket_checkpoints.db"
    )
    # 回复出口的重试形状（见 services/ticket/outbox.py）。租约秒数要明显长于一次
    # 外发调用的超时：比它短就会出现"进程还在发、租约到期、第二条进程又发一遍"，
    # 而那对客户端就是一条重复的邮件。
    TICKET_OUTBOX_LEASE_SECONDS: int = 120
    TICKET_OUTBOX_MAX_ATTEMPTS: int = 3
    # 退避基数，实际间隔是 基数 × 已尝试次数
    TICKET_OUTBOX_BACKOFF_SECONDS: int = 60
    # 办结后顺带排一条邀评。默认开：CSAT 是文档§1 列的核心指标，而没人被邀请过
    # 的话这个指标永远只能等客户自己上门骂一句时才有一条数据。
    # 没有接任何发送通道时它只是往队列里多放一行，不会真发出什么。
    TICKET_CSAT_INVITE_ENABLED: bool = True
    # 自动排空待发队列。**默认关**，和仓库里每一样新能力一样：出问题要能用一条配置
    # 拨回去，而不是回滚部署。
    #
    # 关着的时候只有治理台上那个手动按钮会去 drain，于是"办结的回复什么时候发出去"
    # 取决于有没有人恰好去点一下——队列只会变长。打开它之前请先确认
    # ``services/ticket/senders.build_sender()`` 真的返回了一个通道，否则这个循环
    # 每 N 秒醒来一次、发现没有出口、又睡回去，除了日志什么也不改变。
    TICKET_OUTBOX_AUTODRAIN: bool = False
    TICKET_OUTBOX_DRAIN_INTERVAL_SECONDS: int = 30
    TICKET_OUTBOX_DRAIN_BATCH: int = 10

    # ========== 外发通道：SMTP ==========
    # 空 host = 没有邮件通道，``senders.build_sender()`` 返回 None，行为与今天一致
    # （队列只增不减，治理台如实说"没有接入任何发送通道"）。
    #
    # 只覆盖 ``email`` 这一个工单渠道：``web_chat`` / ``app`` / ``wecom`` 的行不会被
    # 这一条通道认领出去（见 ``outbox.claim_next`` 的 channels 过滤）。宁可让它们继续
    # 躺在 pending 里被看见，也不要用邮件通道把它们"发成功"。
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    # 发件地址。留空视为"通道没配好"而不是"用默认值试试"——半套配置会让每一条
    # 外发都失败，而那时最难查的是"到底是服务商的问题还是我们没配完"。
    SMTP_FROM: str = ""
    SMTP_SUBJECT_PREFIX: str = "【客服工单】"
    # starttls（默认，587）| ssl（465）| none（本机 relay 之类）。
    # none 会把凭据和正文都明文发出去，所以用得上它的人应当知道自己为什么用得上。
    SMTP_SECURITY: str = "starttls"
    SMTP_TIMEOUT_SECONDS: float = 15.0

    # ========== 时区 ==========
    # 应用写入 naive DATETIME 列时使用的时区偏移(小时)。必须与数据库服务器的
    # 墙上时间一致,否则 server_default=func.now() 写的行和应用写的行会差一个时区。
    APP_TZ_OFFSET_HOURS: int = 8

    # ========== 可观测性 ==========
    # 关闭后所有埋点退化为无副作用的空操作,不写库
    TELEMETRY_ENABLED: bool = True
    # 单条 span 的 attributes JSON 上限。埋点只存元数据,不存提示词与用户文本,
    # 这个上限是防止将来误加字段时把整段上下文写进库的兜底。
    TELEMETRY_ATTR_MAX_CHARS: int = 2000
    # 价目表 JSON 路径(相对 back-end/)。缺失时成本一律为"未知"而不是编一个数字。
    PRICING_CONFIG_PATH: str = "model_prices.json"
    # 用量查询的默认统计窗口(天)
    METRICS_DEFAULT_DAYS: int = 7

    # ---- 用量闸门(services/usage_guard) ---------------------------------------
    #
    # 2026-08-29 加。此前 ``/chats/completions/stream`` 上一个上界都没有:
    # ``@limiter.limit`` 只挂在 auth 的两个端点上,而这条路径是全项目最贵的
    # ——最多 AGENT_MAX_TOOL_ROUNDS 轮模型调用,每轮可能带 web_search 与向量化。
    # 成本那边 telemetry 一直在算并写进 trace_spans.cost,但**没有任何一处因为
    # 成本拒绝执行**。两条合起来就是:一个脚本 = 无上界的花钱。
    #
    # 三个上界互相独立,分别防三件不同的事,全为 0 表示关闭:
    #
    # 1. **请求频率**(RATE):防滥用。进程内计数,每个请求都算,**不管有没有花钱**
    #    ——早早失败的请求也要算,否则打空请求的脚本永远撞不到上限。
    #    代价见 usage_guard 模块文档:多 worker 下每个进程各有一份计数。
    # 2. **成本**(COST):防超支。读 trace_spans.cost,也就是**真实记账**。
    # 3. **token**(TOKENS):成本的兜底。价目表里没有的模型 cost 是 NULL,
    #    只卡成本的话换个未定价模型就绕过去了;token 永远有记录(实测或本地估算)。
    #
    # 窗口都是滑动的,不按自然日对齐:按自然日对齐会在午夜给出一个整份的新配额,
    # 于是"卡住了就等到零点"变成一种可行的绕过方式。
    USAGE_GUARD_ENABLED: bool = True
    # 每用户每窗口的对话请求数上限。窗口用分钟计。
    USAGE_RATE_WINDOW_MINUTES: float = 1.0
    USAGE_RATE_MAX_REQUESTS: int = 20
    # 成本与 token 的窗口(小时)。默认 24 小时。
    USAGE_QUOTA_WINDOW_HOURS: float = 24.0
    # 每用户每窗口的成本上限,单位与价目表的 currency 一致。0 = 不限。
    #
    # 混币种时按**各币种独立比较**,不做汇率换算——项目里没有汇率来源,
    # 编一个换算率会得到一个看起来精确的假数字(同 pricing 模块的取舍)。
    # 仍然是 0(不限):成本上限要有价目表才有意义,而生产模型 glm-4.6v 现在不在
    # model_prices.json 里,它的 cost 是 NULL。给这一项配个非零值只会产生一个
    # 永不触发的闸门,而那比明摆着关掉更糟——它会让人以为超支这件事已经被管住了。
    # 补上价目之后再给它一个默认值。
    USAGE_QUOTA_MAX_COST: float = 0.0
    # 每用户每窗口(24 小时)的 token 上限,输入+输出。0 = 不限。
    #
    # 2026-09-12 从 0 改成 2,000,000。改动之前**三个上界里只有频率是活的**:
    # 成本与 token 都默认 0,而 usage_guard 里两个都是 0 就直接 return。也就是说
    # 默认配置下没有任何花钱的上界,只有 20 次/分钟——而 20 次/分钟换算下来是
    # 每天 28,800 次请求,按下面测出来的单回合开销算是两亿多 token。
    #
    # 这个数是量出来的,不是拍的。2026-09-12 那轮 48 任务全量:50 个回合共
    # 382,091 token,**单回合平均 7,642、中位数 5,681、最贵的一个 16,165**
    # (memory-web,4 轮)。据此:
    #
    #   200 回合/天 ≈ 1,530,000 token  ← 很重的正常使用
    #   2,000,000 → 大约 260 个平均回合,或 120 个最贵的回合
    #
    # 所以它拦不住任何真人的正常一天,但把跑飞的脚本从"两亿"压到"两百万",
    # 差两个数量级。
    #
    # 已知的不精确,都写在 usage_guard 的模块文档里:这个判据**滞后一个回合**
    # (读的是已记账用量),所以实际可能超出一个回合的量。在"防跑飞"这个目标下
    # 可接受——它拦的是第 N+1 次,不是让第 N 次刚好停在线上。
    #
    # 上面那些数字是在一个 92 块的小语料上量的。真实部署的知识库更大、预检索
    # 塞进去的材料更多,单回合会更贵,所以这是个**起点**而不是定论:
    # 私有化部署按自己的 trace_spans 调。
    USAGE_QUOTA_MAX_TOKENS: int = 2_000_000

    # ---- 线上健康监控与主动告警 ----
    # usage_guard 防的是**单个用户**跑飞,它答不了另一个问题:整条线**作为一个
    # 整体**是不是在变坏——错误率爬升、人工介入变多、单次回答变贵。离线金标
    # (eval + 门禁)量的是"模型在固定题上的质量",但它跑在温度 0、固定语料上,
    # 和线上(温度 0.7、真实流量)是两套分布。这里补的是"线上这半环":从
    # trace_spans + agent_runs 聚合真实指标,越阈值就经 B1 通知出口推给管理员。
    #
    # 默认关(与其它运营开关一致)。打开后由 production_monitor 评估;没有内置
    # 定时器——评估**顺带挂在通知未读数轮询上**(前端的徽标心跳),也可以让外部
    # cron 打 GET /metrics/health。多 worker 下每个 worker 各评估一次,靠"每个管理员
    # 同时只留一条未读健康告警"去重吸收重复(同 VECTOR_STORE=memory 那类诚实边界)。
    MONITOR_ENABLED: bool = False
    # 聚合窗口(小时)。滑动窗口,与 usage 的自然日对齐无关。
    MONITOR_WINDOW_HOURS: float = 24.0
    # 样本不足时不告警:窗口内**新建工单**数低于此值直接跳过。
    # 三五个工单里挂一个就是 20%+ 失败率,那是噪声不是信号(同 calibration 的 MIN_N)。
    MONITOR_MIN_TICKETS: int = 20
    # 两次真正评估之间的最小间隔(分钟)。评估挂在高频读路径上,靠它节流:
    # 间隔内的调用是一次时间戳比较就返回,只有跨过间隔的那一次付聚合查询的钱。
    MONITOR_INTERVAL_MINUTES: float = 15.0
    # 四条阈值,各自独立,0 = 关掉这一条(同 usage_guard 的约定)。
    # 失败率 / 人工介入率是比例(0–1);成本是"单张工单"的币值;处理时长是毫秒。
    #
    # 失败率 = status=failed 的工单 / 窗口内新建工单。
    MONITOR_MAX_ERROR_RATE: float = 0.0
    # 人工介入率 = (挂在审批上 或 已转人工)的工单 / 全部工单。
    # 它爬升意味着"自动闭环"在变成"事事要人"——人在回路是卖点,但全靠人就不是自动化了。
    MONITOR_MAX_INTERVENTION_RATE: float = 0.0
    # 单张工单平均成本上限。混币种时按相加算(有汇率问题),所以默认 0:
    # 生产模型未进价目表时成本是 NULL,配非零只会得到永不触发的假闸门(同 USAGE_QUOTA_MAX_COST)。
    MONITOR_MAX_COST_PER_TICKET: float = 0.0
    # 处理时长(created→resolved)的 p95(毫秒)。0 = 不看。就是文档里那个"平均处理时长"。
    MONITOR_MAX_P95_HANDLE_MS: float = 0.0

    @property
    def embedding_api_key(self) -> str:
        """实际使用的 Embedding API Key (优先 EMBEDDING_API_KEY,回退 LLM_API_KEY)"""
        return self.EMBEDDING_API_KEY or self.LLM_API_KEY

    @property
    def utility_model(self) -> str:
        """辅助任务(摘要/改写/重排/记忆抽取)实际使用的模型"""
        return self.LLM_UTILITY_MODEL or self.LLM_MODEL

    @property

    @property
    def judge_model(self) -> str:
        """裁判实际使用的模型。优先独立裁判模型,其次辅助模型,最后主模型"""
        return self.JUDGE_MODEL or self.LLM_UTILITY_MODEL or self.LLM_MODEL

    @property
    def embedding_base_url(self) -> str:
        """实际使用的 Embedding Base URL (优先 EMBEDDING_BASE_URL,回退 LLM_BASE_URL)"""
        return self.EMBEDDING_BASE_URL or self.LLM_BASE_URL

    # ========== 文件上传配置 ==========
    UPLOAD_DIR: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")

    # ========== JWT 认证配置 ==========
    JWT_SECRET_KEY: str = _DEFAULT_JWT_KEY
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_EXPIRE_MINUTES: int = 30
    JWT_REFRESH_EXPIRE_DAYS: int = 7

    # ========== CORS 白名单 ==========
    CORS_ORIGINS: str = "http://localhost:5173,http://localhost:4173,http://127.0.0.1:5173"

    class Config:
        """Pydantic 配置"""
        env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        env_file_encoding = "utf-8"
        case_sensitive = True

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.ENV == "production"


settings = Settings()

# 启动时校验 JWT 密钥不能为默认占位符
if settings.JWT_SECRET_KEY == _DEFAULT_JWT_KEY:
    raise RuntimeError(
        "JWT_SECRET_KEY 仍为默认占位符,请在 .env 中配置一个长度 >= 32 字符的随机字符串"
    )
