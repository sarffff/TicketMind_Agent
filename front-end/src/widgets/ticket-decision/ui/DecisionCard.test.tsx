import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { PendingInterrupt, TicketRunResult, ToolTier } from "@/shared/types/api.types";

/**
 * 审批卡片。这张卡片是整个系统里唯一一处"人替模型决定要不要动钱"的界面，
 * 所以这里钉的全是**它有没有把要执行的东西如实摆出来**。
 *
 * 后端那条对应关系是"批准的参数就是执行的参数"（`approval.validate_edit`），
 * 界面对应的责任是：让人看到的键 = 会被送出去的键，改动看得见，
 * 而缺信息时按最重的一档显示（宁重不轻）。这三条一旦破了，症状不是报错，
 * 而是"我以为批的是 350，实际执行的是别的什么"。
 */
const toasts = { error: vi.fn(), success: vi.fn(), info: vi.fn() };
vi.mock("@/shared/ui/Toast", () => ({ useToast: () => toasts }));
vi.mock("@/shared/api/client", () => ({
  apiClient: { decideTicket: vi.fn() },
}));

const { apiClient } = await import("@/shared/api/client");
const { DecisionCard } = await import("./DecisionCard");

const decideTicket = vi.mocked(apiClient.decideTicket);

function pending(overrides: Partial<PendingInterrupt> = {}): PendingInterrupt {
  return {
    ticket_id: "t-1",
    workspace_id: "w-1",
    intent: "refund",
    risk_level: "high",
    reason: "要发起一笔退款，需要你确认",
    round_index: 2,
    calls: [
      {
        id: "call-1",
        name: "create_refund",
        arguments: JSON.stringify({
          order_no: "ORD-9001",
          amount: "350.00",
          reason_code: "quality",
        }),
      },
    ],
    ...overrides,
  };
}

const TIERS: Record<string, ToolTier> = { create_refund: "fund" };

function renderCard(pend: PendingInterrupt = pending(), tiers = TIERS) {
  render(
    <DecisionCard ticketId="t-1" pending={pend} tiers={tiers} onResolved={vi.fn()} />
  );
}

const approveButton = () => button(/批准执行/);
const rejectButton = () => button(/拒绝并转人工/);
function button(name: RegExp): HTMLButtonElement {
  return screen.getByRole("button", { name }) as HTMLButtonElement;
}
/** 处置说明那个 textarea。`<input>` 的角色也是 textbox，所以必须按名字收窄 */
const noteBox = () => screen.getByRole("textbox", { name: /处置说明/ });

/** 最近一次送出去的请求体。断言"发出去的就是屏幕上看到的"全靠它 */
function lastBody() {
  const call = decideTicket.mock.calls.at(-1);
  expect(call, "decideTicket 没有被调用").toBeDefined();
  return call![1];
}

const resolved: TicketRunResult = {
  outcome: "resolved",
  reply: "已办理",
  escalationReason: null,
  plan: [],
  pending: null,
  rounds: 2,
  // 这两个字段的存在本身就是一个语义：costKnown=false 时那个 0 要读成"不知道"
  costKnown: true,
  costUsed: 0,
};

beforeEach(() => {
  toasts.error.mockClear();
  toasts.success.mockClear();
  decideTicket.mockReset().mockResolvedValue(resolved);
});

afterEach(() => cleanup());

describe("DecisionCard", () => {
  it("没写理由就按不下批准", () => {
    renderCard();
    fireEvent.click(approveButton());

    expect(decideTicket).not.toHaveBeenCalled();
    expect(toasts.error).toHaveBeenCalledWith(expect.stringContaining("理由"));
  });

  it("屏幕上改过的值，就是送出去的那个值", async () => {
    renderCard();
    fireEvent.change(screen.getByDisplayValue("350.00"), { target: { value: "120.00" } });
    fireEvent.change(noteBox(), { target: { value: "凭证齐全" } });

    fireEvent.click(approveButton());
    await vi.waitFor(() => expect(decideTicket).toHaveBeenCalled());

    const [ticketId, body] = decideTicket.mock.calls[0];
    expect(ticketId).toBe("t-1");
    expect(body.edited).toEqual({ "call-1": { amount: "120.00" } });
    expect(body.note).toBe("凭证齐全");
    expect(body.approved).toBe(true);
  });

  it("没改的键不会凭空出现在送出的修改里", async () => {
    renderCard();
    fireEvent.change(screen.getByDisplayValue("350.00"), { target: { value: "120.00" } });
    fireEvent.change(noteBox(), { target: { value: "同意" } });
    fireEvent.click(approveButton());
    await vi.waitFor(() => expect(decideTicket).toHaveBeenCalled());

    expect(Object.keys(lastBody().edited?.["call-1"] ?? {})).toEqual(["amount"]);
  });

  it("只写理由时不携带任何参数修改", async () => {
    renderCard();
    fireEvent.change(noteBox(), { target: { value: "按政策全额退" } });
    fireEvent.click(approveButton());
    await vi.waitFor(() => expect(decideTicket).toHaveBeenCalled());

    expect(lastBody().edited).toEqual({});
  });

  it("拒绝时不把自己改过的参数带出去", async () => {
    /**
     * 拒绝走的是"转人工"，人接手时看到的是模型原本提了什么。把界面上的改动
     * 一起发过去，等于让一次拒绝顺带改写了那份即将被人工接手的提案。
     */
    renderCard();
    fireEvent.change(screen.getByDisplayValue("350.00"), { target: { value: "1.00" } });
    fireEvent.change(noteBox(), { target: { value: "证据不足" } });

    fireEvent.click(rejectButton());
    await vi.waitFor(() => expect(decideTicket).toHaveBeenCalled());

    const body = lastBody();
    expect(body.approved).toBe(false);
    expect(body.edited).toEqual({});
  });

  it("改过的键会标出「已改」", () => {
    renderCard();
    expect(screen.queryByText("已改")).toBeNull();
    fireEvent.change(screen.getByDisplayValue("350.00"), { target: { value: "999" } });
    expect(screen.getByText("已改")).toBeTruthy();
  });

  it("改回原值不再标「已改」", () => {
    renderCard();
    const amount = screen.getByDisplayValue("350.00");
    fireEvent.change(amount, { target: { value: "1" } });
    fireEvent.change(screen.getByDisplayValue("1"), { target: { value: "350.00" } });
    expect(screen.queryByText("已改")).toBeNull();
  });

  it("数字类型的值不可编辑，也不给一个能改坏类型的框", () => {
    /**
     * 金额在这个系统里一路是字符串（"350"）：JSON 数字走 float，在钱上迟早咬人。
     * 这里让坐席把 "350.00" 改成数字，改的就不是一个值而是整份参数的类型。
     */
    renderCard(
      pending({
        calls: [
          { id: "call-2", name: "create_refund", arguments: JSON.stringify({ amount: 350, notify: true }) },
        ],
      })
    );

    expect(screen.queryByDisplayValue("350")).toBeNull();
    expect(screen.getAllByText(/不可改型/)).toHaveLength(2);
  });

  it("档位查不到时按资金类显示，宁重不轻", () => {
    /** 与后端 `tier_of` 的 fail-closed 同一个方向：漏登记的工具不能被显示成"读一类"。 */
    renderCard(pending(), {});
    expect(document.querySelector("[data-tier]")?.getAttribute("data-tier")).toBe("fund");
  });

  it("参数不是合法 JSON 时卡片仍然渲染得出来，而且不给一个注定被拒的编辑框", () => {
    /**
     * 旧快照与半截落库都会给出一句不是 JSON 的原文。整张卡片崩掉等于这张单没人能批；
     * 而把它渲染成编辑框更坏：改出来的键叫 `raw`，后端只接受修改已有的键，
     * 症状是"按了批准却什么都没执行"。
     */
    renderCard(pending({ calls: [{ id: "call-3", name: "create_refund", arguments: "半截的参数" }] }));

    expect(screen.getByText("半截的参数")).toBeTruthy();
    expect(screen.queryByDisplayValue("半截的参数")).toBeNull();
    expect(screen.getByText(/只能整体批准或拒绝/)).toBeTruthy();
    expect(approveButton().disabled).toBe(false);
  });

  it("数组形态的参数不被当成对象拆开", () => {
    renderCard(pending({ calls: [{ id: "call-4", name: "update_order_address", arguments: "[1,2,3]" }] }));
    expect(screen.getByText("[1,2,3]")).toBeTruthy();
    expect(screen.queryByDisplayValue("[1,2,3]")).toBeNull();
  });

  it("拒绝不需要理由，按得动", async () => {
    renderCard();
    fireEvent.click(rejectButton());
    await vi.waitFor(() => expect(decideTicket).toHaveBeenCalled());
    expect(lastBody().note).toBe("");
  });

  it("提交期间两个按钮都锁住，避免同一张单批两次", async () => {
    let release!: (value: unknown) => void;
    decideTicket.mockReturnValue(
      new Promise((resolve) => {
        release = resolve;
      }) as never
    );

    renderCard();
    fireEvent.change(noteBox(), { target: { value: "同意" } });
    fireEvent.click(approveButton());

    await vi.waitFor(() => {
      expect(approveButton().disabled).toBe(true);
    });
    expect(rejectButton().disabled).toBe(true);
    // 锁住之外还要确认真的只发出去一次，否则双 disabled 可能只是没接上
    expect(decideTicket).toHaveBeenCalledTimes(1);

    release(resolved);
  });
});
