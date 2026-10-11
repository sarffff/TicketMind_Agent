/**
 * 后端数据模型的 TypeScript 对照。
 *
 * 三条规矩，每条都对应一个曾经真实发生过的 bug：
 *
 * 1. **响应一律 camelCase，请求体一律 snake_case。** 后端 router 手工拼 camelCase
 *    的 dict，而 Pydantic 请求模型没有 alias 生成器。照着旧字段名写会静默拿到
 *    `undefined`，而 `undefined` 在 JSX 里渲染成空白，看起来像"这条没有数据"。
 * 2. **速率与成本类字段的 `null` 是"不知道"，不是 0。** 后端刻意在样本不足时回
 *    null（否则"错误操作率 0%"会被读成"零错误"），界面必须显示"未知"而不是 0。
 * 3. **一个接口没有活的调用方就删掉。** 上一版这里躺着 60 个接口，其中 40 个的
 *    主人（对话工作台、本机文件、报销审核、长期记忆、消息反馈）已经和后端模块
 *    一起删了——留着它们不会报错，只会让人以为那些能力还在。
 *
 * 枚举值全部对着 router 与 services 的字面量抄，不要凭语义猜。
 */

// ========== Auth ==========

export interface User {
  id: string;
  email: string;
  username: string | null;
  name: string | null;
  avatar: string | null;
  provider: string | null;
  is_active: boolean;
  is_verified: boolean;
  created_at: string;
}

export interface LoginRequest {
  email: string;
  password: string;
}

export interface RegisterRequest {
  email: string;
  username: string;
  password: string;
  name?: string;
}

export interface AuthResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
  user: User;
}

export interface TokenResponse {
  access_token: string;
  refresh_token?: string;
  token_type: string;
  expires_in: number;
}

// ========== 工单 ==========

/**
 * 工单状态机取值（对齐 models.Ticket.status 与 graph 的落库点）。
 *
 * `awaiting_approval` 与 `escalated` 是"有人在等"的两档，界面上必须跳在最前面；
 * 其余按处理时序排列。用宽松联合 + string 兜底：后端加了新状态时界面该照常渲染，
 * 而不是编译期就红掉。
 */
export type TicketStatus =
  | "new"
  | "understanding"
  | "planning"
  | "acting"
  | "awaiting_approval"
  | "escalated"
  | "resolved"
  | "closed"
  | "failed"
  | (string & {});

export type TicketRisk = "low" | "mid" | "high";

/** 渠道取值来自 TICKET_CHANNELS 配置，默认这六个（phone/wecom 尚无入站适配器） */
export type TicketChannel =
  | "web_chat"
  | "email"
  | "app"
  | "wecom"
  | "phone"
  | "api";

/**
 * 工具权限档位。资金类必过人审——这条是治理的唯一判据（后端 TIER_BY_TOOL），
 * 界面用它决定一条调用要不要盖"待批准"的章。
 */
export type ToolTier = "read" | "mutate" | "fund";

/** GET /tickets 列表项（后端 brief 形状） */
export interface TicketSummary {
  id: string;
  channel: TicketChannel;
  status: TicketStatus;
  riskLevel: TicketRisk | null;
  intent: string | null;
  subject: string | null;
  summary: string | null;
  customerId: string | null;
  assigneeId: string | null;
  resolution: string | null;
  escalationReason: string | null;
  csatScore: number | null;
  toolRounds: number | null;
  createdAt: string | null;
  updatedAt: string | null;
  resolvedAt: string | null;
  /** SLA 到点时间。超期未结的后端会在队列响应里单独给计数 */
  slaDueAt: string | null;
  firstResponseAt: string | null;
}

/** GET /tickets/{id} 的完整工单：在 brief 之上多出来的都是回放与处置要用的原文 */
export interface TicketDetail extends TicketSummary {
  requestText: string;
  /**
   * 理解层抽出的事实。**后端已经把 JSON 列解析过了**（`_loads`），所以这里是对象
   * 而不是字符串——把它当字符串再 `JSON.parse` 会得到一个对象，然后
   * "objects are not valid as a React child" 在页面上炸掉整棵树。
   * 解析失败时后端回 null，界面按"还没有抽取结果"处理。
   *
   * 值可能是嵌套的（`identity`、`channel_metadata`），渲染前必须逐层摊平成字符串。
   */
  entities: Record<string, unknown> | null;
  attachments: unknown[] | null;
  resolutionNote: string | null;
  csatComment: string | null;
  /** 这张工单累计的模型成本。null = 未知，不是免费 */
  llmCost: string | null;
  closedAt: string | null;
  /**
   * 这位客户已积累的偏好——**下一张工单会把这些喂给模型**。
   *
   * 它来自客户画像而不是工单行，所以后端把它摆在响应的 `ticket` 之外；
   * `getTicket` 取出来并到这一个对象上，界面上就是"这一屏能看到的东西"。
   * 只有坐席写得进来，模型不参与：画像是会被后续每一单读到的长期状态。
   */
  customerPreferences: string[];
}

export interface TicketQueueResponse {
  /** 工单域总开关。关着时列表为空，而"关着"和"没有工单"必须分开说 */
  enabled: boolean;
  total: number;
  tickets: TicketSummary[];
  reapedOverdue: number;
  overdueStillOpen: number;
  /** 工具名 → 档位。界面用它给每一次调用上色，不再自己维护一份名单 */
  toolTiers: Record<string, ToolTier>;
}

/** POST /tickets 的请求体。后端字段是 snake_case，这里保持同名以免逐字翻译出错 */
export interface SubmitTicketRequest {
  channel: TicketChannel;
  content: string;
  subject?: string;
  external_ref?: string;
  customer_email?: string;
  customer_phone?: string;
  customer_ref?: string;
  attachments?: string[];
  metadata?: Record<string, unknown>;
}

export interface SubmitTicketResponse {
  ticketId: string;
  created: boolean;
  /** 命中已有工单时那张单的 id：同一渠道+内容重复提交不该产生第二张 */
  dedupedAgainst: string | null;
  customerId: string | null;
  status: TicketStatus;
}

/**
 * 挂在人身上的那份待批内容，即 graph 的 ``interrupt()`` 载荷。
 *
 * **这个对象是 snake_case**——它不是 router 拼的 dict，而是编排层的中断载荷被
 * 检查点原样存下来后透出来的。按 camelCase 读会拿到 undefined，而表现是审批
 * 卡片上一片空白：标题、原因、参数全不见，只剩两个按钮——那是一张"看不清就点了"
 * 的卡片，正是人在回路最不该变成的样子。
 */
export interface PendingCall {
  id: string;
  name: string;
  arguments: string;
}

export interface PendingInterrupt {
  ticket_id: string;
  workspace_id: string | null;
  intent: string | null;
  risk_level: TicketRisk | null;
  reason: string;
  calls: PendingCall[];
  round_index: number;
}

export interface PendingTicket {
  /** 解析失败时为 null：工单还在等人，但检查点里读不出待批内容 */
  pending: PendingInterrupt | null;
}

export interface PendingApprovalListResponse {
  count: number;
  items: (TicketSummary & PendingTicket)[];
}

/** POST /tickets/{id}/decision 的请求体 */
export interface DecisionRequest {
  approved: boolean;
  /** 给人看的理由，也是审计里那一条的说明 */
  note?: string;
  /**
   * 改过的参数：{被批准的 call_id: {已有键名: 新值}}。
   * 后端只允许改**已有的键**（approval.validate_edit），新增键整份被拒——
   * 要求的是"看到什么就批什么"。
   */
  edited?: Record<string, Record<string, unknown>>;
}

/** POST /tickets/{id}/run 的返回（一次运行的观察结果） */
export interface TicketRunResult {
  outcome: "resolved" | "escalated" | "awaiting_approval" | "failed" | string;
  reply: string | null;
  escalationReason: string | null;
  plan: { goal: string; tool?: string }[];
  pending: PendingInterrupt | null;
  rounds: number;
  /** 这一趟有没有拿到过用量。false 时 costUsed 的 0 读作"不知道" */
  costKnown: boolean;
  costUsed: number;
}

export interface CsatResponse {
  ticketId: string;
  csatScore: number;
}

export interface CloseTicketResponse {
  ticketId: string;
  status: TicketStatus;
  resolution: string;
}

/** POST /tickets/{id}/preference：回的是**这位客户当前**的偏好全集，不是刚加的那条 */
export interface CustomerPreferenceResponse {
  ticketId: string;
  preferences: string[];
}

/** GET /tickets/{id}/events 的一步。node/kind 是自由文本，界面按"有就显示"处理 */
export interface TicketTraceEvent {
  seq: number;
  node: string;
  kind: string;
  status: string | null;
  tool: string | null;
  /** 参数的 sha256 摘要。和审批记录里的是同一个算法，因此可以直接对账 */
  argumentsDigest: string | null;
  argumentsPreview: string | null;
  result: string | null;
  message: string | null;
  roundIndex: number | null;
  createdAt: string | null;
}

export interface TicketEventsResponse {
  ticketId: string;
  events: TicketTraceEvent[];
}

/** GET /tickets/metrics。所有 rate 都可能为 null，含义是"样本不足" */
export interface TicketMetrics {
  windowDays: number;
  total: number;
  terminal: number;
  deflectionRate: number | null;
  humanHandoffRate: number | null;
  avgHandleMinutes: number | null;
  avgFirstResponseMinutes: number | null;
  csatAverage: number | null;
  csatResponses: number;
  llmCostTotal: number | null;
  /** 价目表没命中的工单数：它们花了钱，只是算不出多少 */
  unpricedTickets: number;
  slaOverdueStillOpen: number;
  rejectedAttemptRate: number | null;
  errorActionRate: number | null;
  reviewedOperations: number;
  unreviewedOperations: number;
  wrongByCorrectiveAction: Record<string, number>;
  operations: {
    executed: number;
    replayed: number;
    failed: number;
    blocked: number;
    pendingApproval: number;
  };
}

/** GET /tickets/governor/state */
export interface GovernorState {
  paused: boolean;
  pauseReason: string | null;
  /** 金额一律字符串：JSON 数字走 float，在钱的算术上迟早咬人 */
  dailyRefundLimit: string;
  refundUsedToday: string;
  maxCostPerTicket: string | null;
  perTicketToolCalls: number | null;
  refundReviewThreshold: string | null;
}

export interface GovernorPauseResponse {
  paused: boolean;
  pauseReason: string | null;
}

/** GET /tickets/outbox：欠客户的话，以及它排在队列里的哪一段 */
export interface OutboxItem {
  id: string;
  ticketId: string;
  kind: "reply" | "csat_invite" | string;
  channel: TicketChannel;
  recipient: string | null;
  status: "pending" | "sending" | "sent" | "failed" | "suppressed";
  attempts: number;
  error: string | null;
  /** 正文前 500 字。抑制一条时该看得见自己按什么按的 */
  body: string | null;
  createdAt: string | null;
  sentAt: string | null;
}

export interface OutboxResponse {
  /** 没有接任何真实发送通道时为 false：这时发不出去不是故障，是没有出口 */
  senderConnected: boolean;
  pending: number;
  items: OutboxItem[];
}

export interface OutboxDrainResult {
  sent: number;
  failed: number;
  suppressed: number;
  [key: string]: unknown;
}

/** 操作台账里的一条写操作。错误操作率的真数据源就是它的 verdict/action */
export interface OperationRow {
  id: string;
  ticketId: string | null;
  tool: string | null;
  operation: string | null;
  permission: ToolTier | string | null;
  status: string;
  amount: string | null;
  currency: string | null;
  argumentsPreview: string | null;
  resultPreview: string | null;
  /** 人工复核结论。null = 还没人看过 */
  verdict: string | null;
  correctedAction: string | null;
  reviewNote: string | null;
  reviewedBy: string | null;
  createdAt: string | null;
}

export interface OperationListResponse {
  count: number;
  items: OperationRow[];
}

export interface OperationReviewRequest {
  verdict: string;
  /** 处置动作，≤40 字。错误操作率按这一列分桶 */
  action?: string;
  note?: string;
}

// ========== 线上健康 ==========

export interface HealthBreach {
  metric: string;
  value: number;
  threshold: number;
}

export interface HealthReport {
  windowHours: number;
  totalTickets: number;
  /** 样本够不够判阈值。不够时 breaches 为空，但那不代表"健康" */
  sufficient: boolean;
  metrics: {
    errorRate?: number;
    interventionRate?: number;
    failedTickets?: number;
    intervenedTickets?: number;
    avgCostPerTicket?: number | null;
    p95CostPerTicket?: number | null;
    p95HandleMs?: number | null;
    ticketsWithKnownCost?: number;
    [key: string]: unknown;
  };
  breaches: HealthBreach[];
}

export interface HealthResponse {
  enabled: boolean;
  report: HealthReport;
  alertsCreated: number;
}

// ========== 知识库 ==========

/** workspace = 团队共享（仅 admin 可增删），private = 只进上传者自己的检索 */
export type DocumentVisibility = "workspace" | "private";

export interface KnowledgeDocument {
  id: string;
  name: string;
  size: number;
  chunks: number;
  status: "indexed" | "processing" | "failed";
  createdAt: string;
  visibility?: DocumentVisibility;
  /** 由后端算：前端比 user_id 会做出一个"能点但 403"的删除按钮 */
  isOwn?: boolean;
  ownerName?: string | null;
  /** 上传者账号已删除、被收编成共享文档 */
  inherited?: boolean;
  /**
   * 会不会进**当前用户**的检索。admin 看得见成员的个人文档但检索不到它们，
   * 所以"可见"与"会被引用"是两件事，界面要分开说。
   */
  retrievable?: boolean;
  parseBackend?: string | null;
  parseWarnings?: string[];
  /** 非终态文档带上的队列进度 */
  jobStatus?: string | null;
  jobProgress?: number | null;
  jobAttempts?: number | null;
}

export interface UploadDocumentResponse extends KnowledgeDocument {
  /** true 表示内容哈希命中已有文档，本次没有重复索引 */
  duplicate: boolean;
}

export interface KnowledgeQueryChunk {
  document_id: string;
  document_name: string;
  chunk_index: number;
  chunk_range?: [number, number] | null;
  content: string;
  /** 稠密通道得分；null 表示只被 BM25 命中 */
  score: number | null;
  fusion_score?: number | null;
  channels?: string[];
}

export interface KnowledgeQueryResult {
  query: string;
  results: KnowledgeQueryChunk[];
  total: number;
}

/** 引用点击看原文：命中块 + 邻域 */
export interface DocumentChunkView {
  documentId: string;
  documentName: string;
  chunks: { chunkIndex: number; content: string }[];
}

// ========== 工作区 ==========

export interface WorkspaceMember {
  id: string;
  name: string;
  /** `member` 是历史值，语义等同 `user`；权限一律看 isAdmin */
  role: "admin" | "user" | "member";
  /** 管理字段，只有 admin 拿得到。渲染时按 isAdmin 判断，不要靠"有没有 email"推断权限 */
  email?: string;
  isActive?: boolean;
  isSelf?: boolean;
}

export interface WorkspaceInfo {
  id: string;
  name: string;
  role: "admin" | "user" | "member";
  isAdmin?: boolean;
  memberCount: number;
  members: WorkspaceMember[];
  /** 后端直接给最后一个管理员的计数，前端不要再数一遍成员列表 */
  adminCount?: number;
  inviteCode?: string | null;
}

export interface WorkspaceMemberMutationResponse {
  success: boolean;
  workspace: WorkspaceInfo;
  member?: { id: string; name: string; role: string };
  removed?: { id: string; name: string };
}

export interface JoinWorkspaceResponse {
  success: boolean;
  workspace: WorkspaceInfo;
  /**
   * 原空间里这个人能看到的文档数。加入是**换空间**不是多一个空间，
   * 这些文档不会被删但也不会再出现在检索里——必须提示，否则像丢了资料。
   */
  leftBehindDocuments: number;
}

// ========== SOP 作业指导 ==========

export interface BuiltinSkill {
  name: string;
  description: string;
  /** 附带文件名，模型用 read_skill_file 取 */
  attachments: string[];
  /** 逗号分隔。Agent 必须先拿齐这些才能下结论 */
  requiredInputs: string;
  /** 被同名的工作区 SOP 盖掉了。不显示的话 admin 会以为自己写的那份没生效 */
  overridden: boolean;
}

export interface WorkspaceSkill {
  id: string;
  name: string;
  description: string;
  instructions: string;
  enabled: boolean;
  requiredInputs: string;
  /** 正文/描述/前置材料变了才 +1。SOP 是给人读的规程，没有版本号的修改无法追溯 */
  version: number;
  updatedAt: string | null;
}

export interface SkillsResponse {
  builtin: BuiltinSkill[];
  workspace: WorkspaceSkill[];
  /** SKILL_ENABLED 关着时写了也不生效，要说清楚 */
  enabled: boolean;
  canEdit: boolean;
}

// ========== 通知 ==========

/**
 * 通知类别决定图标与点击去向。宽松联合 + string 兜底：后端加了新类别时
 * 界面按默认渲染，而不是整块红掉。
 *
 * - `approval_required` → 跳审批收件箱
 * - `ticket_handoff` → 跳那张工单（转人工，需要有人接手）
 * - `health_alert` → 跳线上健康（发给管理员的越阈值告警）
 */
export type NotificationKind =
  | "approval_required"
  | "ticket_handoff"
  | "health_alert"
  | (string & {});

/** 命名加 App 前缀：`Notification` 是浏览器全局类型，撞名会遮盖 DOM 类型 */
export interface AppNotification {
  id: string;
  kind: NotificationKind;
  title: string;
  body: string | null;
  /** 归属工单。null 表示这条不指向某张单（例如全局健康告警） */
  ticketId: string | null;
  createdAt: string | null;
  readAt: string | null;
  read: boolean;
}

export interface NotificationListResponse {
  notifications: AppNotification[];
  /** 总未读数。红点用这个数，不用本页条数 */
  unreadCount: number;
}

// ========== 审计 ==========

export interface AuditEntry {
  id: string;
  /** 同一散列链内的序号。verify 报"第一个断掉的 seq"就靠它 */
  seq: number;
  action: string;
  target: string | null;
  ticketId: string | null;
  argumentsPreview: string | null;
  argumentsDigest: string | null;
  createdAt: string | null;
}

/** GET /audit 只给这一页，不给总数：审计链的长度不是界面上需要核对的量 */
export interface AuditListResponse {
  entries: AuditEntry[];
}

export interface AuditVerifyResponse {
  ok: boolean;
  entries: number;
  /** 链在哪一跳断掉；ok 为 true 时是 null */
  firstBrokenSeq: number | null;
}

// ========== 用量与埋点 ==========

export interface UsageGroup {
  name?: string | null;
  model?: string | null;
  kind?: string | null;
  calls: number;
  promptTokens: number;
  completionTokens: number;
  avgMs: number | null;
  totalMs: number;
  /** null = 价目表里没有这个模型，即成本未知（不是零成本） */
  cost: number | null;
  currency: string | null;
  failures: number;
}

export interface UsageSummary {
  rangeDays: number;
  pricingConfigured: boolean;
  totals: {
    spans: number;
    turns: number;
    promptTokens: number;
    completionTokens: number;
    /** promptTokens 中被上下文缓存命中的部分，是子集不是增量 */
    cachedTokens: number;
    /** 分母只算回传过缓存信息的调用；一类都没有时为 null，读作"未知" */
    promptCacheHitRate: number | null;
    estimatedTokenShare: number | null;
    failures: number;
  };
  costs: { currency: string | null; amount: number | null }[];
  byName: UsageGroup[];
  byModel: UsageGroup[];
  byKind: UsageGroup[];
}

/**
 * 一次编排运行的概览。
 *
 * 一次运行不等于一张工单：挂起等人批之后恢复会再开一条 trace，所以同一张单
 * 通常有多条。业务轨迹看 /tickets/{id}/events，这里看的是埋点。
 */
export interface TraceSummary {
  traceId: string;
  ticketId: string | null;
  startedAt: string | null;
  durationMs: number;
  spans: number;
  promptTokens: number;
  completionTokens: number;
  cachedTokens: number;
  cost: number | null;
  currency: string | null;
  failures: number;
}

export interface TraceSpanNode {
  id: string;
  parentId: string | null;
  name: string;
  kind: string;
  startedAt: string | null;
  durationMs: number | null;
  status: string;
  errorType: string | null;
  model: string | null;
  promptTokens: number | null;
  completionTokens: number | null;
  cachedTokens: number | null;
  tokenSource: string | null;
  cost: number | null;
  currency: string | null;
  /** JSON 字符串：属性只放元数据，不放提示词与用户文本 */
  attributes: string | null;
  children: TraceSpanNode[];
}

export interface TraceDetail {
  traceId: string;
  roots: TraceSpanNode[];
}

// ========== 附件 ==========

export interface AttachmentUploadResult {
  /** /uploads/... 相对路径，调用方拼 baseUrl */
  url: string;
  filename: string;
  size: number;
  contentType: string;
  isImage: boolean;
}

// ========== UI 专用 ==========

/** 左侧导航的模块标识。路由与 NavRail 共用，避免两处各写一份 */
export type TicketDeskModule =
  | "queue"
  | "approvals"
  | "governance"
  | "metrics"
  | "knowledge"
  | "skills"
  | "notifications"
  | "audit"
  | "workspace";
