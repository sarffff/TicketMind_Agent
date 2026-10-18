import type {
  AttachmentUploadResult,
  AuditListResponse,
  AuditVerifyResponse,
  AuthResponse,
  CloseTicketResponse,
  CustomerPreferenceResponse,
  CsatResponse,
  DecisionRequest,
  DocumentChunkView,
  DocumentVisibility,
  GovernorPauseResponse,
  GovernorState,
  HealthResponse,
  JoinWorkspaceResponse,
  KnowledgeDocument,
  KnowledgeQueryResult,
  LoginRequest,
  NotificationListResponse,
  OperationListResponse,
  OperationReviewRequest,
  OperationRow,
  OutboxDrainResult,
  OutboxResponse,
  PendingApprovalListResponse,
  RegisterRequest,
  SkillsResponse,
  SubmitTicketRequest,
  SubmitTicketResponse,
  TicketDetail,
  TicketEventsResponse,
  TicketMetrics,
  TicketQueueResponse,
  TicketRisk,
  TicketRunResult,
  TokenResponse,
  TraceDetail,
  TraceSummary,
  UploadDocumentResponse,
  UsageSummary,
  User,
  WorkspaceInfo,
  WorkspaceMemberMutationResponse,
} from "../types/api.types";

/**
 * FastAPI 后端的 API 客户端。
 *
 * 一个类而不是按域拆十个模块：认证态（access/refresh token、401 单飞刷新）是
 * 跨域共享的可变状态，拆开就要在每个模块里重复一次刷新逻辑，而"某个模块忘了
 * 刷新"这种 bug 只在 token 过期后的第一个请求上出现，测试覆盖不到。
 *
 * **这里不再有 SSE/流式**。上一版有 openStream/readStream/reconnect 那一整套
 * （约 300 行），服务的是对话循环的逐字输出。工单的生命周期比 HTTP 请求长得多
 * ——一张单可能挂几天等人批——所以提交与执行是两个接口，界面读的是**落库的轨迹**
 * （GET /tickets/{id}/events），不是一条需要维持的长连接。留着那套只会让人以为
 * 工单是流式的。
 */
export class ApiClient {
  private baseUrl: string;

  private token: string | null = null;
  private refreshToken: string | null = null;
  private refreshing: Promise<string | null> | null = null;

  constructor(baseUrl: string = "http://localhost:3000") {
    this.baseUrl = baseUrl;
    this.token = localStorage.getItem("access_token");
    this.refreshToken = localStorage.getItem("refresh_token");
  }

  /** 拼接附件等静态资源地址用 */
  getBaseUrl(): string {
    return this.baseUrl;
  }

  setToken(token: string | null) {
    this.token = token;
    if (token) localStorage.setItem("access_token", token);
    else localStorage.removeItem("access_token");
  }

  setRefreshToken(token: string | null) {
    this.refreshToken = token;
    if (token) localStorage.setItem("refresh_token", token);
    else localStorage.removeItem("refresh_token");
  }

  getToken(): string | null {
    return this.token;
  }

  private authHeader(token: string | null): Record<string, string> {
    return token ? { Authorization: `Bearer ${token}` } : {};
  }

  /**
   * 用 refresh token 换一个新的 access token。
   *
   * 单飞（同一个 Promise 复用）：队列页一次并发五六个请求，401 同时到达时
   * 不能各自去刷——refresh 是一次性凭据，并发刷新的第二个必然失败，
   * 表现是"页面自己退出了登录"。
   */
  private async tryRefreshToken(): Promise<string | null> {
    if (this.refreshing) return this.refreshing;
    if (!this.refreshToken) return null;

    this.refreshing = (async () => {
      try {
        const response = await fetch(`${this.baseUrl}/auth/refresh`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ refresh_token: this.refreshToken }),
        });
        if (!response.ok) {
          this.setToken(null);
          this.setRefreshToken(null);
          localStorage.removeItem("user");
          return null;
        }
        const data: TokenResponse = await response.json();
        this.setToken(data.access_token);
        return data.access_token;
      } catch {
        return null;
      } finally {
        this.refreshing = null;
      }
    })();

    return this.refreshing;
  }

  /**
   * 所有请求的唯一出口。
   *
   * 错误一律抛 `Error`，消息取后端的 `detail`：那些文案是后端按情形写的中文
   * （"这张工单不存在"、"仅管理员可暂停"、"TICKET_AGENT_ENABLED=false"），
   * 比前端自己拼一句 "Failed to fetch" 有用得多，也不需要在这里维护第二份
   * 状态码→文案的映射。
   *
   * `allow409` 是给提交/执行用的：409 在这里不是故障而是业务答复（工单域没开），
   * 调用方要能拿到那个 detail 去显示，而不是被当成异常吞掉。
   */
  private async request<T>(
    path: string,
    options: RequestInit = {},
    { allow409 = false }: { allow409?: boolean } = {}
  ): Promise<T> {
    const send = (token: string | null) =>
      fetch(`${this.baseUrl}${path}`, {
        ...options,
        headers: {
          ...(options.body instanceof FormData
            ? {}
            : { "Content-Type": "application/json" }),
          ...this.authHeader(token),
          ...(options.headers as Record<string, string> | undefined),
        },
      });

    let response = await send(this.token);

    if (response.status === 401 && this.refreshToken) {
      const fresh = await this.tryRefreshToken();
      if (fresh) response = await send(fresh);
    }

    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      const detail = (body as { detail?: string })?.detail;
      if (response.status === 409 && allow409) {
        // 409 当正常返回交出：调用方读 message 显示成"这个能力没开"
        return { conflict: true, message: detail } as unknown as T;
      }
      throw new Error(
        detail || `请求失败（${response.status} ${response.statusText}）`
      );
    }

    if (response.status === 204) return undefined as T;
    const text = await response.text();
    return (text ? JSON.parse(text) : undefined) as T;
  }

  private get<T>(path: string, opts?: { allow409?: boolean }) {
    return this.request<T>(path, { method: "GET" }, opts);
  }

  private post<T>(path: string, body?: unknown, opts?: { allow409?: boolean }) {
    return this.request<T>(
      path,
      { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) },
      opts
    );
  }

  // ========== Auth ==========

  /**
   * 注册与登录成功后立刻把两枚 token 存进实例。
   *
   * 不在调用方做：漏一处就意味着后续每个请求都带着空 Authorization 去撞 401，
   * 而 401 的自动刷新又没有 refresh token——表现是"登录成功但整页都是空的"。
   */
  async register(request: RegisterRequest): Promise<AuthResponse> {
    const data = await this.request<AuthResponse>("/auth/register", {
      method: "POST",
      body: JSON.stringify(request),
    });
    this.setToken(data.access_token);
    this.setRefreshToken(data.refresh_token);
    return data;
  }

  async login(request: LoginRequest): Promise<AuthResponse> {
    const data = await this.request<AuthResponse>("/auth/login", {
      method: "POST",
      body: JSON.stringify(request),
    });
    this.setToken(data.access_token);
    this.setRefreshToken(data.refresh_token);
    return data;
  }

  getCurrentUser(): Promise<User> {
    return this.get<User>("/auth/me");
  }

  async logout(): Promise<{ success: boolean; message: string }> {
    const result = await this.post<{ success: boolean; message: string }>(
      "/auth/logout"
    );
    this.setToken(null);
    this.setRefreshToken(null);
    return result;
  }

  getRefreshToken(): Promise<TokenResponse> {
    return this.post<TokenResponse>("/auth/refresh");
  }

  // ========== 工单：提交、队列、详情、轨迹 ==========

  /**
   * 提交一张工单。只落库，不驱动执行。
   *
   * 409（工单域没开）当正常结果交出：调用方显示"后端没开工单能力"，
   * 而不是抛一个看起来像网络故障的异常。
   */
  submitTicket(body: SubmitTicketRequest): Promise<SubmitTicketResponse> {
    return this.post<SubmitTicketResponse>("/tickets", body, { allow409: true });
  }

  listTickets(
    filters: {
      status?: string;
      risk?: TicketRisk;
      mine?: boolean;
      limit?: number;
      offset?: number;
    } = {}
  ): Promise<TicketQueueResponse> {
    const params = new URLSearchParams();
    if (filters.status) params.set("status", filters.status);
    if (filters.risk) params.set("risk", filters.risk);
    if (filters.mine) params.set("mine", "true");
    params.set("limit", String(filters.limit ?? 50));
    if (filters.offset) params.set("offset", String(filters.offset));
    return this.get<TicketQueueResponse>(`/tickets?${params.toString()}`, {
      allow409: true,
    });
  }

  getTicket(ticketId: string): Promise<TicketDetail> {
    return this.get<TicketDetail & { ticket: TicketDetail }>(
      `/tickets/${encodeURIComponent(ticketId)}`
    ).then((body) => ({
      ...body.ticket,
      // 画像在响应里摆在 ticket 之外（那是客户的状态不是工单的状态），但对这一屏
      // 来说它是"能看见的事实"之一，所以并进来。缺字段按没有偏好处理，不是未知。
      customerPreferences: body.customerPreferences ?? [],
    }));
  }

  getTicketEvents(
    ticketId: string,
    limit = 500
  ): Promise<TicketEventsResponse> {
    return this.get<TicketEventsResponse>(
      `/tickets/${encodeURIComponent(ticketId)}/events?limit=${limit}`
    );
  }

  /** 驱动这张工单往前走一步（或从头跑）。可能返回 awaiting_approval 并挂起 */
  runTicket(ticketId: string): Promise<TicketRunResult> {
    return this.post<TicketRunResult>(
      `/tickets/${encodeURIComponent(ticketId)}/run`,
      undefined,
      { allow409: true }
    );
  }

  decideTicket(ticketId: string, body: DecisionRequest): Promise<TicketRunResult> {
    return this.post<TicketRunResult>(
      `/tickets/${encodeURIComponent(ticketId)}/decision`,
      body
    );
  }

  submitCsat(ticketId: string, score: number, comment = ""): Promise<CsatResponse> {
    return this.post<CsatResponse>(
      `/tickets/${encodeURIComponent(ticketId)}/csat`,
      { score, comment }
    );
  }

  closeTicket(
    ticketId: string,
    resolution: string,
    note = ""
  ): Promise<CloseTicketResponse> {
    return this.post<CloseTicketResponse>(
      `/tickets/${encodeURIComponent(ticketId)}/close`,
      { resolution, note }
    );
  }

  /**
   * 给这位客户记一条偏好（"不要打电话，发邮件"这类）。
   *
   * 请求体是 snake_case（仓库约定），响应是这位客户**当前**的偏好全集而不是刚加的那条
   * ——幂等去重在后端，界面要显示的是去重之后的结果。
   */
  addTicketPreference(
    ticketId: string,
    preference: string
  ): Promise<CustomerPreferenceResponse> {
    return this.post<CustomerPreferenceResponse>(
      `/tickets/${encodeURIComponent(ticketId)}/preference`,
      { preference }
    );
  }

  // ========== 工单：审批收件箱与治理 ==========

  /**
   * 待批列表。响应里的 `pending` 是编排层的 interrupt 载荷，**snake_case**：
   * 它不是 router 拼的 dict，而是检查点里原样透出来的那份。见 api.types 的说明。
   */
  getPendingApprovals(): Promise<PendingApprovalListResponse> {
    return this.get<PendingApprovalListResponse>("/tickets/pending", {
      allow409: true,
    });
  }

  ticketMetrics(days?: number): Promise<TicketMetrics> {
    const query = days ? `?days=${days}` : "";
    return this.get<TicketMetrics>(`/tickets/metrics${query}`, { allow409: true });
  }

  governorState(): Promise<GovernorState> {
    return this.get<GovernorState>("/tickets/governor/state", { allow409: true });
  }

  /** 一键暂停。**理由必填**：暂停是个要事后能回答"当时为什么按下去"的动作 */
  pauseGovernor(reason: string): Promise<GovernorPauseResponse> {
    return this.post<GovernorPauseResponse>("/tickets/governor/pause", { reason });
  }

  resumeGovernor(reason: string): Promise<GovernorPauseResponse> {
    return this.post<GovernorPauseResponse>("/tickets/governor/resume", { reason });
  }

  unreviewedOperations(days = 7, limit = 50): Promise<OperationListResponse> {
    const params = new URLSearchParams({ days: String(days), limit: String(limit) });
    return this.get<OperationListResponse>(
      `/tickets/operations/unreviewed?${params.toString()}`,
      { allow409: true }
    );
  }

  reviewOperation(
    operationId: string,
    body: OperationReviewRequest
  ): Promise<OperationRow> {
    return this.post<OperationRow>(
      `/tickets/operations/${encodeURIComponent(operationId)}/review`,
      body
    );
  }

  // ========== 工单：回复出口 ==========

  outboxQueue(status?: string, limit = 50): Promise<OutboxResponse> {
    const params = new URLSearchParams({ limit: String(limit) });
    if (status) params.set("status", status);
    return this.get<OutboxResponse>(`/tickets/outbox?${params.toString()}`, {
      allow409: true,
    });
  }

  /**
   * 催一次投递。没有接任何真实通道时后端不改状态，
   * 返回的计数会全是 0——那是"没有出口"，不是"发送失败"，界面要分开说。
   */
  drainOutbox(limit = 10): Promise<OutboxDrainResult> {
    return this.post<OutboxDrainResult>(`/tickets/outbox/drain?limit=${limit}`);
  }

  /** 抑制一条待发消息。理由必填，它进审计 */
  suppressOutbox(rowId: string, reason: string): Promise<OutboxResponse> {
    return this.post<OutboxResponse>(
      `/tickets/outbox/${encodeURIComponent(rowId)}/suppress`,
      { reason }
    );
  }

  // ========== 知识库 ==========

  getDocuments(): Promise<KnowledgeDocument[]> {
    return this.get<KnowledgeDocument[]>("/knowledge/documents");
  }

  /**
   * 上传。`visibility` 显式传而不靠后端默认——两个入口的默认值本来就不同。
   * 后端还会校验：非 admin 传 workspace 会被 403 挡掉，detail 直接透给调用方。
   */
  uploadDocument(
    file: File,
    visibility: DocumentVisibility = "workspace"
  ): Promise<UploadDocumentResponse> {
    const formData = new FormData();
    formData.append("file", file);
    formData.append("visibility", visibility);
    return this.request<UploadDocumentResponse>("/knowledge/documents/upload", {
      method: "POST",
      body: formData,
    });
  }

  /** 抓网页入知识库。出站由后端 egress 拦私网，前端不需要预校验 */
  addDocumentFromUrl(
    url: string,
    visibility: DocumentVisibility = "workspace"
  ): Promise<UploadDocumentResponse & { sourceUrl?: string }> {
    return this.post("/knowledge/documents/from-url", { url, visibility });
  }

  deleteDocument(docId: string): Promise<{ success: boolean }> {
    return this.request<{ success: boolean }>(
      `/knowledge/documents/${encodeURIComponent(docId)}`,
      { method: "DELETE" }
    );
  }

  queryKnowledge(query: string, topK = 5): Promise<KnowledgeQueryResult> {
    return this.post<KnowledgeQueryResult>("/knowledge/query", {
      query,
      // 请求体是 snake_case：整个透传 camelCase 对象会让 top_k 取默认值，
      // 于是"调 top_k"这个开关看起来生效、实际从未生效
      top_k: topK,
    });
  }

  getDocumentChunk(
    documentId: string,
    chunkIndex: number
  ): Promise<DocumentChunkView> {
    return this.get<DocumentChunkView>(
      `/knowledge/documents/${encodeURIComponent(documentId)}/chunks/${chunkIndex}`
    );
  }

  // ========== 工单附件 ==========

  /** 上传一张工单附件，返回 /uploads/... 相对路径。目前只存不进检索 */
  uploadAttachment(file: File): Promise<AttachmentUploadResult> {
    const formData = new FormData();
    formData.append("file", file);
    return this.request<AttachmentUploadResult>("/attachments/upload", {
      method: "POST",
      body: formData,
    });
  }

  // ========== 工作区 ==========

  getWorkspace(): Promise<WorkspaceInfo> {
    return this.get<WorkspaceInfo>("/workspace");
  }

  /**
   * 凭邀请码加入。加入是**换空间**，响应里的 leftBehindDocuments 必须提示给用户，
   * 否则他的第一反应是"资料丢了"。
   */
  joinWorkspace(inviteCode: string): Promise<JoinWorkspaceResponse> {
    return this.post<JoinWorkspaceResponse>("/workspace/join", {
      invite_code: inviteCode,
    });
  }

  setMemberRole(
    memberId: string,
    role: "admin" | "user"
  ): Promise<WorkspaceMemberMutationResponse> {
    return this.request<WorkspaceMemberMutationResponse>(
      `/workspace/members/${encodeURIComponent(memberId)}`,
      { method: "PATCH", body: JSON.stringify({ role }) }
    );
  }

  removeMember(memberId: string): Promise<WorkspaceMemberMutationResponse> {
    return this.request<WorkspaceMemberMutationResponse>(
      `/workspace/members/${encodeURIComponent(memberId)}`,
      { method: "DELETE" }
    );
  }

  regenerateInviteCode(): Promise<{ inviteCode: string }> {
    return this.post<{ inviteCode: string }>("/workspace/invite-code");
  }

  // ========== SOP 作业指导 ==========

  getSkills(): Promise<SkillsResponse> {
    return this.get<SkillsResponse>("/skills");
  }

  /**
   * upsert 一份工作区 SOP。PUT 是整体覆盖，所以 `requiredInputs` **必须传**——
   * 不传会把已有声明清成空串，而那不报错：规程从此少了几项必填材料，安静地松一档。
   */
  saveSkill(payload: {
    name: string;
    description: string;
    instructions: string;
    enabled?: boolean;
    requiredInputs?: string;
  }): Promise<
    Pick<
      import("../types/api.types").WorkspaceSkill,
      "id" | "name" | "description" | "enabled" | "requiredInputs" | "version"
    >
  > {
    return this.request("/skills", {
      method: "PUT",
      body: JSON.stringify({
        name: payload.name,
        description: payload.description,
        instructions: payload.instructions,
        enabled: payload.enabled,
        required_inputs: payload.requiredInputs ?? "",
      }),
    });
  }

  deleteSkill(skillId: string): Promise<void> {
    return this.request<void>(`/skills/${encodeURIComponent(skillId)}`, {
      method: "DELETE",
    });
  }

  // ========== 通知 ==========

  getNotifications(
    unreadOnly = false,
    limit = 50,
    offset = 0
  ): Promise<NotificationListResponse> {
    const params = new URLSearchParams({
      limit: String(limit),
      offset: String(offset),
    });
    if (unreadOnly) params.set("unread_only", "true");
    return this.get<NotificationListResponse>(`/notifications?${params.toString()}`);
  }

  /**
   * 未读数。这个端点在后端顺带当**线上健康的心跳**：前端轮询红点，于是没有调度器
   * 也能让告警评估按节流跑起来。所以轮询不只是刷新红点，别把它关掉。
   */
  async getUnreadCount(): Promise<number> {
    const body = await this.get<{ count?: number }>("/notifications/unread_count");
    return body?.count ?? 0;
  }

  markNotificationRead(notificationId: string): Promise<void> {
    return this.post<void>(
      `/notifications/${encodeURIComponent(notificationId)}/read`
    );
  }

  async markAllNotificationsRead(): Promise<number> {
    const body = await this.post<{ marked?: number }>("/notifications/read_all");
    return body?.marked ?? 0;
  }

  // ========== 审计 ==========

  getAuditEntries(limit = 100, offset = 0): Promise<AuditListResponse> {
    const params = new URLSearchParams({
      limit: String(limit),
      offset: String(offset),
    });
    return this.get<AuditListResponse>(`/audit?${params.toString()}`);
  }

  /** 哈希链校验：回答"这串记录没被改过"。firstBrokenSeq 指出断在哪一跳 */
  verifyAuditChain(): Promise<AuditVerifyResponse> {
    return this.get<AuditVerifyResponse>("/audit/verify");
  }

  // ========== 用量与埋点 ==========

  getUsage(days?: number): Promise<UsageSummary> {
    const query = days ? `?days=${days}` : "";
    return this.get<UsageSummary>(`/metrics/usage${query}`);
  }

  /** 全局线上健康。**管理员专属**，非 admin 拿 403（detail 已是中文说明）。
   *  顺带触发一次即时告警评估 */
  getHealth(): Promise<HealthResponse> {
    return this.get<HealthResponse>("/metrics/health");
  }

  listTraces(ticketId?: string, limit = 20): Promise<TraceSummary[]> {
    const params = new URLSearchParams({ limit: String(limit) });
    if (ticketId) params.set("ticket_id", ticketId);
    return this.get<TraceSummary[]>(`/metrics/traces?${params.toString()}`);
  }

  getTrace(traceId: string): Promise<TraceDetail> {
    return this.get<TraceDetail>(`/metrics/traces/${encodeURIComponent(traceId)}`);
  }

  /** 后端活着吗。用于启动时的连接提示，不用于业务分支 */
  async ping(): Promise<boolean> {
    try {
      await this.request("/");
      return true;
    } catch {
      return false;
    }
  }
}

export const apiClient = new ApiClient();

/**
 * 这是不是一个"能力没开"的答复。
 *
 * 后端在 `TICKET_AGENT_ENABLED=false` 时对写操作与执行类接口回 409，而这不是错误：
 * 界面要显示"这个能力没开"并说明怎么开，而不是抛一个看起来像网络故障的异常。
 * 判据集中在这里，页面就不必各自去猜响应体里那个 `conflict` 键。
 */
export interface ConflictResult {
  conflict: true;
  message?: string;
}

export const isConflictResponse = (value: unknown): value is ConflictResult =>
  typeof value === "object" &&
  value !== null &&
  (value as { conflict?: boolean }).conflict === true;
