import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { TicketTraceEvent } from "@/shared/types/api.types";

/**
 * 工单轨迹回放。
 *
 * 这一层的全部意义是"事后能看懂当时发生了什么"，所以钉的都是**读法**：
 *
 * 1. **顺序来自数组而不是时间戳。** 后端的 DATETIME 是 naive 且没有亚秒精度，
 *    同一步里连写两条会拿到同一个时间；按时间排就把两件事读成一件事。
 * 2. **筛选不改总步数。** 标题上那个数字说的是"这张单走了几步"，
 *    收起思考类步骤不该让它变小——那正好抹掉回放最需要的上下文。
 * 3. **子代理归属只认前缀。** 取不到就不标，而不是猜一个：把某一步错标给
 *    一个子代理，事后追责就追到错的人头上。
 */
const { TicketTimeline } = await import("./TicketTimeline");

function event(over: Partial<TicketTraceEvent> = {}): TicketTraceEvent {
  return {
    seq: 1,
    node: "reason",
    kind: "think",
    status: "ok",
    tool: null,
    argumentsDigest: null,
    argumentsPreview: null,
    result: null,
    message: "先看这张单在说什么",
    roundIndex: null,
    createdAt: "2026-10-05T10:00:00",
    ...over,
  };
}

afterEach(() => cleanup());

describe("TicketTimeline", () => {
  it("没有轨迹时给的是解释，不是一块空白", () => {
    render(<TicketTimeline events={[]} />);
    expect(screen.getByText("还没有轨迹。")).toBeTruthy();
    expect(screen.getByText(/工单一旦被驱动/)).toBeTruthy();
  });

  it("按工序排而不是按时间戳排", () => {
    /**
     * 时间戳要和数组顺序**互相矛盾**（1 在 2 之后、2 在 3 之前）：无论实现改成
     * 按时间升序还是降序，读出来的顺序都会变。之前给的是同向的时间戳，那条用例
     * 钉不住任何东西——它只是在复述实现恰好没排序。
     *
     * 后端 DATETIME 是 naive 且没有亚秒精度，同一步连写两条会拿到同一个时间，
     * 所以时间戳本来就不能作为排序依据（`trace.replay` 里记着同一件事）。
     */
    render(
      <TicketTimeline
        events={[
          event({ seq: 1, message: "第一步", createdAt: "2026-10-05T10:00:00" }),
          event({ seq: 2, message: "第二步", createdAt: "2026-10-05T09:00:00" }),
          event({ seq: 3, message: "第三步", createdAt: "2026-10-05T11:00:00" }),
        ]}
      />
    );

    const first = screen.getByText("第一步");
    const second = screen.getByText("第二步");
    const third = screen.getByText("第三步");
    const follows = (a: HTMLElement, b: HTMLElement) =>
      !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);

    expect(follows(first, second)).toBe(true);
    expect(follows(second, third)).toBe(true);
    expect(screen.getAllByText(/^\d+$/).slice(0, 3).map((node) => node.textContent)).toEqual([
      "1",
      "2",
      "3",
    ]);
  });

  it("筛选只看待处置与结果，但总步数说的是这张单", () => {
    render(
      <TicketTimeline
        events={[
          event({ seq: 1, kind: "think", message: "在想" }),
          event({ seq: 2, kind: "tool_call", tool: "lookup_order", message: "要查这单" }),
          event({ seq: 3, kind: "approval", status: "pending", message: "等人批" }),
          event({ seq: 4, kind: "retrieve", message: "查政策" }),
        ]}
      />
    );

    expect(screen.getByText(/PROCESS TRACE · 4 步/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /只看待处置与结果/ }));

    expect(screen.queryByText("在想")).toBeNull();
    expect(screen.queryByText("查政策")).toBeNull();
    expect(screen.getByText("等人批")).toBeTruthy();
    // 标题上仍然是 4：收起步骤不该把"这张单走了几步"一起改小
    expect(screen.getByText(/PROCESS TRACE · 4 步/)).toBeTruthy();
    expect(screen.getByRole("button", { name: /显示全部步骤/ }).getAttribute("aria-pressed")).toBe(
      "true"
    );
  });

  it("子代理归属只认那个前缀", () => {
    render(
      <TicketTimeline
        events={[
          event({ seq: 1, message: "子代理 policy 的第 2 轮：先查退换政策" }),
          event({ seq: 2, message: "这一步是主代理自己做的" }),
          event({ seq: 3, message: null }),
        ]}
      />
    );

    const badges = screen.getAllByText(/子代理/).filter((node) =>
      node.className.includes("chip")
    );
    // 只有那一条前缀对得上的才被标出来；把普通步骤错标给某个子代理，
    // 事后复盘就会追到错的人头上
    expect(badges).toHaveLength(1);
  });

  it("工具名与轮次一起显示，读得出是第几轮提的", () => {
    render(
      <TicketTimeline
        events={[event({ seq: 1, kind: "tool_call", tool: "create_refund", roundIndex: 3 })]}
      />
    );

    expect(screen.getByText(/第 3 轮/)).toBeTruthy();
    expect(screen.getByText(/发起退款/)).toBeTruthy();
  });

  it("未登记的状态原样显示而不是被当成成功", () => {
    /** 后端加了新 status 而这里还没跟上时，宁可看到原文，也不要它悄悄染成"没问题"。 */
    render(<TicketTimeline events={[event({ seq: 1, status: "cancelled_by_operator" })]} />);
    expect(screen.getByText("cancelled_by_operator")).toBeTruthy();
  });

  it("参数摘要带上指纹，那是和审批记录对账的那把钥匙", () => {
    render(
      <TicketTimeline
        events={[
          event({
            seq: 1,
            kind: "tool_call",
            argumentsDigest: "a1b2c3d4e5f60708",
            argumentsPreview: '{"amount": "350.00"}',
          }),
        ]}
      />
    );

    expect(screen.getByText("#a1b2c3d4")).toBeTruthy();
  });
});
