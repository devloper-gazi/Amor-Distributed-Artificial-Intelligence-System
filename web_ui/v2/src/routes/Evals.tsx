import {
  type Component,
  For,
  Show,
  createMemo,
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

interface ManifestEntry {
  name: string;
  title: string;
  description: string;
  expected_minutes: number;
  implemented: boolean;
  summary_keys: string[];
}

interface EvalRun {
  id: string;
  name: string;
  started_at: string | null;
  finished_at: string | null;
  status: "pending" | "running" | "done" | "failed" | "cancelled";
  backend: string | null;
  git_sha: string | null;
  summary: Record<string, unknown>;
  note: string | null;
}

const STATUS_TO_PILL: Record<EvalRun["status"], Status> = {
  pending: "warming",
  running: "warming",
  done: "healthy",
  failed: "failed",
  cancelled: "warning",
};

const formatTime = (iso: string | null): string => {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
};

const formatDuration = (start: string | null, end: string | null): string => {
  if (!start || !end) return "—";
  try {
    const ms = new Date(end).getTime() - new Date(start).getTime();
    if (ms < 0 || !Number.isFinite(ms)) return "—";
    if (ms < 60_000) return `${(ms / 1000).toFixed(0)}s`;
    return `${(ms / 60_000).toFixed(1)}m`;
  } catch {
    return "—";
  }
};

const formatSummary = (summary: Record<string, unknown>): string => {
  if (!summary || Object.keys(summary).length === 0) return "—";
  if (typeof summary.error === "string") return `err: ${summary.error.slice(0, 60)}`;
  // Pull the most-meaningful key.
  if ("pass_at_1" in summary) {
    const p = (summary.pass_at_1 as number) ?? 0;
    return `pass@1 ${(p * 100).toFixed(1)}%`;
  }
  if ("resolved_rate" in summary) {
    return `resolved ${((summary.resolved_rate as number) * 100).toFixed(0)}%`;
  }
  if ("faithfulness" in summary) {
    const f = summary.faithfulness as number;
    return `faith ${f?.toFixed(2)}`;
  }
  if ("completed" in summary && "total" in summary) {
    return `${summary.completed}/${summary.total}`;
  }
  return JSON.stringify(summary).slice(0, 40);
};

export const Evals: Component = () => {
  const [pendingKick, setPendingKick] = createSignal<string | null>(null);

  const manifest = createQuery<ManifestEntry[]>(() => ({
    queryKey: ["admin", "evals", "manifest"],
    queryFn: () => api.get<ManifestEntry[]>("/api/admin/evals/manifest"),
    staleTime: 60_000,
  }));

  const runs = createQuery<EvalRun[]>(() => ({
    queryKey: ["admin", "evals", "runs"],
    queryFn: () =>
      api.get<EvalRun[]>("/api/admin/evals/runs?limit=50"),
    refetchInterval: 5_000,
  }));

  const kickMutation = createMutation(() => ({
    mutationFn: (name: string) =>
      api.post<{ run_id: string; status: string }>(
        `/api/admin/evals/run/${encodeURIComponent(name)}`,
      ),
    onSettled: () => {
      setPendingKick(null);
      runs.refetch();
    },
  }));

  const triggerRun = (name: string) => {
    setPendingKick(name);
    kickMutation.mutate(name);
  };

  const summary = createMemo(() => {
    const list = runs.data ?? [];
    const total = list.length;
    const succeeded = list.filter((r) => r.status === "done").length;
    const running = list.filter(
      (r) => r.status === "running" || r.status === "pending",
    ).length;
    return { total, succeeded, running };
  });

  return (
    <div class="flex h-full flex-col">
      <TopBar
        title={t("evals.title")}
        subtitle={t("evals.subtitle")}
        actions={
          <>
            <Show when={runs.data}>
              <Badge>
                {t("evals.x_of_y_done", {
                  done: summary().succeeded,
                  total: summary().total,
                })}
              </Badge>
            </Show>
            <Show when={summary().running > 0}>
              <StatusPill status="warming" label={`${summary().running} ${t("common.status.running")}`} size="sm" />
            </Show>
            <Button
              size="sm"
              variant="secondary"
              onClick={() => runs.refetch()}
              disabled={runs.isFetching}
            >
              <Show when={runs.isFetching} fallback={<>{t("common.refresh")}</>}>
                <Spinner size={14} />
              </Show>
            </Button>
          </>
        }
      />

      <div class="flex-1 overflow-auto px-6 py-6">
        {/* Available evals */}
        <h2 class="mb-3 text-sm font-medium uppercase tracking-wider text-text-tertiary">
          {t("evals.section.available")}
        </h2>
        <Show
          when={manifest.data}
          fallback={
            <div class="flex h-32 items-center justify-center text-text-tertiary">
              <Spinner size={20} />
            </div>
          }
        >
          {(items) => (
            <div class="mb-8 grid grid-cols-1 gap-3 md:grid-cols-2">
              <For each={items()}>
                {(entry) => (
                  <div class="rounded-lg border border-border-subtle bg-bg-elevated p-4">
                    <div class="flex items-start justify-between gap-3">
                      <div class="min-w-0 flex-1">
                        <p class="font-medium text-text-primary">
                          {entry.title}
                        </p>
                        <p class="mt-1 text-xs text-text-secondary">
                          {entry.description}
                        </p>
                        <div class="mt-2 flex flex-wrap gap-2 text-[0.7rem] text-text-tertiary">
                          <span>{t("evals.duration", { n: entry.expected_minutes })}</span>
                          <Show when={entry.implemented}>
                            <Badge>{t("common.status.ready")}</Badge>
                          </Show>
                          <Show when={!entry.implemented}>
                            <span class="rounded-full border border-border-subtle px-2 py-0.5 text-text-tertiary">
                              {t("common.status.scaffolded")}
                            </span>
                          </Show>
                        </div>
                      </div>
                      <Button
                        size="sm"
                        variant={entry.implemented ? "primary" : "ghost"}
                        onClick={() => triggerRun(entry.name)}
                        disabled={
                          !entry.implemented ||
                          (kickMutation.isPending && pendingKick() === entry.name)
                        }
                      >
                        <Show
                          when={
                            kickMutation.isPending && pendingKick() === entry.name
                          }
                          fallback={<>{entry.implemented ? t("common.run") : "—"}</>}
                        >
                          <Spinner size={12} />
                        </Show>
                      </Button>
                    </div>
                  </div>
                )}
              </For>
            </div>
          )}
        </Show>

        {/* Run history */}
        <h2 class="mb-3 text-sm font-medium uppercase tracking-wider text-text-tertiary">
          {t("evals.section.recent_runs")}
        </h2>
        <Show
          when={runs.data && (runs.data?.length ?? 0) > 0}
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
                    {t("evals.col.eval")}
                  </th>
                  <th class="px-3 py-2 text-right text-[0.7rem] font-medium uppercase tracking-wider">
                    {t("evals.col.status")}
                  </th>
                  <th class="px-3 py-2 text-right text-[0.7rem] font-medium uppercase tracking-wider">
                    {t("evals.col.started")}
                  </th>
                  <th class="px-3 py-2 text-right text-[0.7rem] font-medium uppercase tracking-wider">
                    {t("evals.col.wall")}
                  </th>
                  <th class="px-3 py-2 text-right text-[0.7rem] font-medium uppercase tracking-wider">
                    {t("evals.col.backend")}
                  </th>
                  <th class="px-3 py-2 text-right text-[0.7rem] font-medium uppercase tracking-wider">
                    {t("evals.col.summary")}
                  </th>
                </tr>
              </thead>
              <tbody>
                <For each={runs.data}>
                  {(row) => (
                    <tr class="border-b border-border-subtle hover:bg-bg-secondary">
                      <td class="px-3 py-2">
                        <span class="font-mono text-xs">{row.name}</span>
                      </td>
                      <td class="px-3 py-2 text-right">
                        <StatusPill
                          status={STATUS_TO_PILL[row.status]}
                          label={row.status}
                          size="sm"
                        />
                      </td>
                      <td class="px-3 py-2 text-right text-text-tertiary text-xs tabular-nums">
                        {formatTime(row.started_at)}
                      </td>
                      <td class="px-3 py-2 text-right tabular-nums">
                        {formatDuration(row.started_at, row.finished_at)}
                      </td>
                      <td class="px-3 py-2 text-right text-text-tertiary text-xs">
                        {row.backend ?? "—"}
                      </td>
                      <td class="px-3 py-2 text-right">
                        <span
                          class="font-mono text-xs"
                          title={JSON.stringify(row.summary, null, 2)}
                        >
                          {formatSummary(row.summary)}
                        </span>
                      </td>
                    </tr>
                  )}
                </For>
              </tbody>
            </table>
          </div>
        </Show>

        <p class="mt-4 text-xs text-text-tertiary">
          {t("common.auto_refresh", { seconds: 5 })}.{" "}
          {t("common.sources")}:{" "}
          <code class="font-mono">/api/admin/evals/manifest</code> +{" "}
          <code class="font-mono">/api/admin/evals/runs</code>.
        </p>
      </div>
    </div>
  );
};
