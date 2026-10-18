import React, { useMemo, useState } from "react";
import { Check, Loader2, X } from "lucide-react";
import { apiClient } from "@/shared/api/client";
import { toolLabel, tierLabel } from "@/shared/lib/format";
import { useToast } from "@/shared/ui/Toast";
import { TierMark } from "@/shared/ui/TierMark";
import type {
  PendingCall,
  PendingInterrupt,
  TicketRunResult,
  ToolTier,
} from "@/shared/types/api.types";

interface DecisionCardProps {
  ticketId: string;
  pending: PendingInterrupt;
  /** 工具名 → 档位，来自队列接口，前端不另立一份名单 */
  tiers: Record<string, ToolTier>;
  onResolved: (result: TicketRunResult) => void;
}

/**
 * 把 arguments 的 JSON 字符串解析成可逐键展示的条目；坏数据不让整张卡片崩。
 *
 * `editable` 与 `hint` 在这里定下来而不是在渲染处猜：落库参数不是合法 JSON 时
 * （旧快照、半截写入）只能整条原文展示，**不能给一个编辑框**——那个框改出来的
 * 键叫 `raw`，而后端只接受修改已有的键（`approval.validate_edit`）。给出去的话
 * 症状是"按了批准却什么都没执行"，而卡片上看着完全正常。
 */
type ArgumentEntry = { key: string; value: unknown; editable: boolean; hint: string | null };

function readArguments(raw: string, tierName: string | undefined): ArgumentEntry[] {
  try {
    const parsed = JSON.parse(raw || "{}");
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return Object.entries(parsed as Record<string, unknown>).map(([key, value]) => ({
        key,
        value,
        editable: typeof value === "string",
        hint:
          typeof value === "string"
            ? null
            : `${tierLabel(tierName)}类参数不可改型`,
      }));
    }
  } catch {
    /* 落库的参数不是合法 JSON：按原文一条展示 */
  }
  return [{ key: "raw", value: raw, editable: false, hint: "参数不是结构化 JSON，只能整体批准或拒绝" }];
}

/**
 * 审批卡片：把"你批准的就是会被执行的"这句话变成界面形状。
 *
 * 三个刻意的做法：
 *
 * 1. **参数按已有的键逐个列出**，不是给一个 JSON 编辑框。后端只允许改已有键
 *    （`approval.validate_edit`），而一个自由文本框让人恰好犯那条错——
 *    加一个新键之后整份修改被静默驳回，卡片上看不出为什么没按自己写的执行。
 * 2. **只有字符串值可改。** 金额在这里是字符串（`"350"`）而不是数字，因为
 *    JSON 数字走 float，在钱上迟早咬人；把数字类型的键改成交付字符串会改变
 *    它的类型，那已经不是"改个值"而是"换一份参数"。
 * 3. **理由必填。** 一次批准的退款在审计里只有这一句话能说清"人当时看到了什么"。
 */
export const DecisionCard: React.FC<DecisionCardProps> = ({
  ticketId,
  pending,
  tiers,
  onResolved,
}) => {
  const toast = useToast();
  const [note, setNote] = useState("");
  const [edits, setEdits] = useState<Record<string, Record<string, string>>>({});
  const [busy, setBusy] = useState<null | "approve" | "reject">(null);

  const calls = useMemo(
    () =>
      pending.calls.map((call: PendingCall) => ({
        call,
        entries: readArguments(call.arguments, tiers[call.name]),
      })),
    [pending.calls, tiers]
  );

  const decide = async (approved: boolean) => {
    if (approved && !note.trim()) {
      toast.error("批准的理由要写。三个月后没人记得当时为什么点头。");
      return;
    }
    setBusy(approved ? "approve" : "reject");
    try {
      const result = await apiClient.decideTicket(ticketId, {
        approved,
        note: note.trim(),
        edited: approved ? edits : {},
      });
      onResolved(result);
      toast.success(approved ? "已批准，Agent 继续往下办" : "已拒绝，这张单转人工处理");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "处置失败");
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="card-surface rounded-2xl p-4 flex flex-col gap-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="label-eyebrow mb-1">AWAITING YOUR DECISION</div>
          <h3 className="text-[14px] font-semibold text-ink">{pending.reason}</h3>
          <p className="text-[11.5px] text-ink-faint mt-1">
            第 {pending.round_index} 轮提出 · 批准后才会真的执行
          </p>
        </div>
        <span className="chip chip-accent shrink-0">资金类</span>
      </div>

      {calls.map(({ call, entries }) => (
        <div key={call.id} className="rounded-xl border border-line p-3">
          <div className="flex items-center justify-between gap-2 mb-2">
            <span className="text-[13px] font-semibold text-ink">
              {toolLabel(call.name)}
            </span>
            <TierMark tier={tiers[call.name] ?? "fund"} withHint />
          </div>

          <div className="flex flex-col gap-2">
            {entries.map(({ key, value, editable, hint }) => {
              const current = editable ? (edits[call.id]?.[key] ?? (value as string)) : String(value);
              const changed = editable && edits[call.id]?.[key] !== undefined && edits[call.id]![key] !== value;
              return (
                <label key={key} className="flex items-center gap-2 text-[12px]">
                  <span className="w-[120px] shrink-0 text-ink-soft">{key}</span>
                  {editable ? (
                    <input
                      className="input-field input-mono py-1"
                      value={current}
                      onChange={(event) =>
                        setEdits((previous) => ({
                          ...previous,
                          [call.id]: { ...(previous[call.id] ?? {}), [key]: event.target.value },
                        }))
                      }
                    />
                  ) : (
                    <span className="input-field input-mono py-1 flex items-center" style={{ background: "var(--c-mantle)", color: "var(--c-ink-soft)" }}>
                      {current}
                    </span>
                  )}
                  {changed && (
                    <span className="text-[10.5px] text-state-hold shrink-0">已改</span>
                  )}
                  {hint && (
                    <span className="text-[10px] text-ink-faint shrink-0">{hint}</span>
                  )}
                </label>
              );
            })}
          </div>
        </div>
      ))}

      <label className="flex flex-col gap-1.5">
        <span className="label-eyebrow">处置说明（会进审计与轨迹）</span>
        <textarea
          className="input-field"
          rows={2}
          maxLength={500}
          value={note}
          placeholder="例：凭证齐全，客户是钻石档且首次申请，按政策全额退"
          onChange={(event) => setNote(event.target.value)}
        />
      </label>

      <div className="flex items-center gap-2 justify-end">
        <button
          type="button"
          onClick={() => void decide(false)}
          disabled={busy !== null}
          className="btn-alarm"
        >
          {busy === "reject" ? (
            <Loader2 className="w-3.5 h-3.5 animate-spin" />
          ) : (
            <X className="w-3.5 h-3.5" />
          )}
          拒绝并转人工
        </button>
        <button
          type="button"
          onClick={() => void decide(true)}
          disabled={busy !== null}
          className="btn-accent px-4 py-2 rounded-[10px] text-[12px] font-semibold"
        >
          {busy === "approve" ? (
            <Loader2 className="w-3.5 h-3.5 animate-spin" />
          ) : (
            <Check className="w-3.5 h-3.5" />
          )}
          批准执行
        </button>
      </div>
    </div>
  );
};
