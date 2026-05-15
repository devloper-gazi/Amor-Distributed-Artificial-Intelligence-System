import {
  type Component,
  For,
  Show,
  createSignal,
} from "solid-js";
import { createQuery, createMutation } from "@tanstack/solid-query";

import { TopBar } from "../components/shell/TopBar";
import {
  Badge,
  Button,
  Spinner,
  StatusPill,
  type Status,
} from "../components/ui";
import { api } from "../lib/api";
import { t } from "../i18n";

interface ResidentModel {
  id?: string;
  name?: string;
  size_vram?: number;
}

interface DeclaredModel {
  id?: string;
  name?: string;
}

interface CompletionsRecent {
  samples: number;
  p50_ms: number | null;
  p95_ms: number | null;
  completion_tokens_p50: number | null;
  cache_reuse_hits: number;
  cache_reuse_hits_total: number;
}

interface SwapEvent {
  from?: string | null;
  to: string;
  cold_load_ms: number;
  ts: number;
}

interface LLMState {
  backend: string;
  configured_kind: string;
  base_url: string | null;
  healthy: boolean | null;
  declared_models: DeclaredModel[];
  resident_models: ResidentModel[];
  probe_error: string | null;
  completions_recent: CompletionsRecent;
  swap_events_recent: SwapEvent[];
}

const formatMs = (ms: number | null | undefined): string => {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms.toFixed(0)} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
};

const formatTime = (ts: number): string => {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString();
};

export const LLMDashboard: Component = () => {
  const [pendingSwap, setPendingSwap] = createSignal<string | null>(null);

  const q = createQuery<LLMState>(() => ({
    queryKey: ["admin", "llm"],
    queryFn: () => api.get<LLMState>("/api/admin/llm"),
    refetchInterval: 15_000,
  }));

  const swapMutation = createMutation(() => ({
    mutationFn: (modelId: string) =>
      api.post<{ model_id: string; cold_load_ms: number; ok: boolean }>(
        `/api/admin/llm/swap-to/${encodeURIComponent(modelId)}`,
      ),
    onSettled: () => {
      setPendingSwap(null);
      q.refetch();
    },
  }));

  const headerStatus = (): Status => {
    if (q.isLoading) return "warming";
    if (!q.data) return "failed";
    if (q.data.healthy === false) return "failed";
    if (q.data.healthy === null) return "warming";
    return "healthy";
  };

  const triggerSwap = (modelId: string) => {
    setPendingSwap(modelId);
    swapMutation.mutate(modelId);
  };

  const isResident = (modelId: string | undefined): boolean => {
    if (!modelId) return false;
    return (q.data?.resident_models ?? []).some(
      (r) => r.id === modelId || r.name === modelId,
    );
  };

  return (
    <div class="flex h-full flex-col">
      <TopBar
        title={t("llm.title")}
        subtitle={t("llm.subtitle")}
        actions={
          <>
            <StatusPill status={headerStatus()} size="sm" />
            <Button
              size="sm"
              variant="secondary"
              onClick={() => q.refetch()}
              disabled={q.isFetching}
            >
              <Show when={q.isFetching} fallback={<>{t("common.refresh")}</>}>
                <Spinner size={14} />
              </Show>
            </Button>
          </>
        }
      />

      <div class="flex-1 overflow-auto px-6 py-6">
        <Show
          when={!q.isLoading && q.data}
          fallback={
            <div class="flex h-64 items-center justify-center text-text-tertiary">
              <Spinner size={20} />
            </div>
          }
        >
          {(state) => (
            <>
              {/* Top strip: backend metadata */}
              <div class="mb-6 grid grid-cols-2 gap-3 md:grid-cols-4">
                <SummaryCell
                  label={t("llm.card.backend")}
                  value={state().backend}
                  hint={t("llm.card.backend_sub", {
                    backend: state().configured_kind || "ollama",
                  })}
                />
                <SummaryCell
                  label={t("llm.card.endpoint")}
                  value={state().base_url || "—"}
                  hint={state().healthy ? t("llm.card.endpoint_sub") : t("llm.card.endpoint_unreachable")}
                />
                <SummaryCell
                  label={t("llm.card.resident")}
                  value={`${state().resident_models.length}/${state().declared_models.length}`}
                  hint={t("llm.card.resident_sub")}
                />
                <SummaryCell
                  label={t("llm.card.cache_hits")}
                  value={String(state().completions_recent.cache_reuse_hits)}
                  hint={t("llm.card.cache_hits_sub", {
                    total: state().completions_recent.cache_reuse_hits_total,
                  })}
                />
              </div>

              {/* Latency strip */}
              <div class="mb-6 grid grid-cols-2 gap-3 md:grid-cols-3">
                <SummaryCell
                  label={t("llm.card.p50_latency")}
                  value={formatMs(state().completions_recent.p50_ms)}
                  hint={t("llm.card.p50_sub", {
                    n: state().completions_recent.samples,
                  })}
                />
                <SummaryCell
                  label={t("llm.card.p95_latency")}
                  value={formatMs(state().completions_recent.p95_ms)}
                  hint={t("llm.card.p95_sub", { n: 100 })}
                />
                <SummaryCell
                  label={t("llm.card.output_tokens")}
                  value={
                    state().completions_recent.completion_tokens_p50 == null
                      ? "—"
                      : (state().completions_recent.completion_tokens_p50 as number).toFixed(0)
                  }
                  hint={t("llm.card.output_tokens_sub")}
                />
              </div>

              {/* Probe error banner */}
              <Show when={state().probe_error}>
                <div class="mb-4 rounded-md border border-border-subtle bg-bg-elevated p-3 text-sm">
                  <span class="text-status-failed">Probe error:</span>{" "}
                  <span class="text-text-secondary">
                    {state().probe_error}
                  </span>
                </div>
              </Show>

              {/* Models table */}
              <h2 class="mb-3 text-sm font-medium uppercase tracking-wider text-text-tertiary">
                {t("llm.section.models")}
              </h2>
              <Show
                when={state().declared_models.length > 0}
                fallback={
                  <p class="rounded-md border border-border-subtle bg-bg-elevated p-4 text-sm text-text-tertiary">
                    {t("common.no_data")}
                  </p>
                }
              >
                <div class="overflow-x-auto rounded-lg border border-border-subtle">
                  <table class="min-w-full text-sm">
                    <thead class="bg-bg-secondary text-text-tertiary">
                      <tr>
                        <th class="px-3 py-2 text-left text-[0.7rem] font-medium uppercase tracking-wider">
                          {t("llm.col.model_id")}
                        </th>
                        <th class="px-3 py-2 text-left text-[0.7rem] font-medium uppercase tracking-wider">
                          {t("llm.col.name")}
                        </th>
                        <th class="px-3 py-2 text-right text-[0.7rem] font-medium uppercase tracking-wider">
                          {t("llm.col.state")}
                        </th>
                        <th class="px-3 py-2 text-right text-[0.7rem] font-medium uppercase tracking-wider">
                          {t("llm.col.action")}
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      <For each={state().declared_models}>
                        {(m) => (
                          <tr class="border-b border-border-subtle hover:bg-bg-secondary">
                            <td class="px-3 py-2 font-mono text-xs">
                              {m.id ?? "—"}
                            </td>
                            <td class="px-3 py-2">{m.name ?? "—"}</td>
                            <td class="px-3 py-2 text-right">
                              <Show
                                when={isResident(m.id)}
                                fallback={
                                  <span class="text-text-tertiary">{t("common.status.on_demand")}</span>
                                }
                              >
                                <Badge>{t("llm.card.resident").toLowerCase()}</Badge>
                              </Show>
                            </td>
                            <td class="px-3 py-2 text-right">
                              <Show when={m.id}>
                                <Button
                                  size="sm"
                                  variant="ghost"
                                  onClick={() => triggerSwap(m.id as string)}
                                  disabled={
                                    swapMutation.isPending &&
                                    pendingSwap() === m.id
                                  }
                                >
                                  <Show
                                    when={
                                      swapMutation.isPending &&
                                      pendingSwap() === m.id
                                    }
                                    fallback={<>{t("common.load")}</>}
                                  >
                                    <Spinner size={12} />
                                  </Show>
                                </Button>
                              </Show>
                            </td>
                          </tr>
                        )}
                      </For>
                    </tbody>
                  </table>
                </div>
              </Show>

              {/* Recent swaps */}
              <Show when={state().swap_events_recent.length > 0}>
                <h2 class="mb-3 mt-6 text-sm font-medium uppercase tracking-wider text-text-tertiary">
                  Recent swap events
                </h2>
                <ul class="space-y-1.5">
                  <For each={state().swap_events_recent.slice().reverse()}>
                    {(ev) => (
                      <li class="flex items-center justify-between rounded-md border border-border-subtle bg-bg-elevated px-3 py-2 text-xs">
                        <span class="font-mono">
                          {ev.from ? `${ev.from} → ` : "→ "}
                          <span class="text-text-primary">{ev.to}</span>
                        </span>
                        <span class="text-text-tertiary">
                          cold {formatMs(ev.cold_load_ms)}
                          <span class="ml-2 tabular-nums">
                            {formatTime(ev.ts)}
                          </span>
                        </span>
                      </li>
                    )}
                  </For>
                </ul>
              </Show>

              <p class="mt-4 text-xs text-text-tertiary">
                {t("common.auto_refresh", { seconds: 15 })}.{" "}
                {t("common.source")}:{" "}
                <code class="font-mono">/api/admin/llm</code>.
              </p>
            </>
          )}
        </Show>
      </div>
    </div>
  );
};

const SummaryCell: Component<{
  label: string;
  value: string;
  hint?: string;
}> = (props) => (
  <div class="rounded-md border border-border-subtle bg-bg-elevated px-3 py-2">
    <p class="text-[0.65rem] uppercase tracking-wider text-text-tertiary">
      {props.label}
    </p>
    <p class="mt-1 truncate font-mono text-sm text-text-primary">
      {props.value}
    </p>
    <Show when={props.hint}>
      <p class="mt-0.5 truncate text-[0.65rem] text-text-tertiary">
        {props.hint}
      </p>
    </Show>
  </div>
);
