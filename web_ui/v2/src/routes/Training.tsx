/**
 * Cycle C Sprint 6 Day 4 — admin Training UI.
 *
 * Surfaces the preference-pair pool + training-run history + the
 * promote button so an operator can drive the manual gate without
 * dropping into a shell.
 *
 * Sections (top-to-bottom):
 *   1. Pool stats — total / untrained / opt-in counts + a progress
 *      bar against the 200-pair training threshold.
 *   2. Sample list — last 50 pairs (mode + truncated text + opt-in
 *      flag); raw text only shows when ``opt_in_raw`` is true.
 *   3. Run button — disabled until 200 untrained pairs accumulate
 *      (matching the API's 409 gate).
 *   4. Run history — every training_runs row, with the eval delta
 *      summary + a Promote button when ``promote_ok`` is true.
 *
 * The actual GPU training happens out-of-band (operator runs
 * ``tools/training/orpo_qwen_coder.py``); this UI is the
 * persistence + promotion layer.
 */

import {
  type Component,
  For,
  Show,
  createMemo,
  createSignal,
} from "solid-js";
import { createQuery, useQueryClient } from "@tanstack/solid-query";

import { TopBar } from "../components/shell/TopBar";
import { Badge, Button, Spinner } from "../components/ui";
import { api } from "../lib/api";
import { t } from "../i18n";


interface PairsStats {
  total: number;
  untrained: number;
  opt_in_raw: number;
  by_mode: Record<string, number>;
  train_threshold: number;
  ready_to_train: boolean;
}

interface PairItem {
  id: string;
  chosen_turn_id: string | null;
  rejected_turn_id: string | null;
  code_hash: string;
  mode: string;
  opt_in_raw: boolean;
  prompt: string | null;
  chosen: string | null;
  rejected: string | null;
  backend: string;
  model_tag: string | null;
  created_at: string;
  trained_in: string | null;
}

interface PairsList {
  count: number;
  limit: number;
  items: PairItem[];
}

interface TrainingRun {
  id: string;
  started_at: string;
  finished_at: string | null;
  status: string;
  config: Record<string, unknown>;
  pair_count: number;
  peft_adapter_path: string | null;
  gguf_adapter_path: string | null;
  eval_summary: {
    promote_ok?: boolean;
    mean_judge_delta?: number | null;
    worst_judge_delta?: number | null;
    p50_latency_pct?: number | null;
  } | null;
  note: string | null;
}

interface RunsList {
  count: number;
  limit: number;
  items: TrainingRun[];
}


export const Training: Component = () => {
  const qc = useQueryClient();
  const [busyRunId, setBusyRunId] = createSignal<string | null>(null);
  const [errorMsg, setErrorMsg] = createSignal<string | null>(null);

  const stats = createQuery<PairsStats>(() => ({
    queryKey: ["training-stats"],
    queryFn: () => api.get<PairsStats>("/api/admin/training/pairs/stats"),
    refetchInterval: 30_000,
  }));
  const pairs = createQuery<PairsList>(() => ({
    queryKey: ["training-pairs"],
    queryFn: () =>
      api.get<PairsList>("/api/admin/training/pairs?limit=50&only_untrained=true"),
    refetchInterval: 60_000,
  }));
  const runs = createQuery<RunsList>(() => ({
    queryKey: ["training-runs"],
    queryFn: () => api.get<RunsList>("/api/admin/training/runs?limit=20"),
    refetchInterval: 30_000,
  }));

  const refresh = () => {
    void stats.refetch();
    void pairs.refetch();
    void runs.refetch();
  };

  const startRun = async (enforceThreshold: boolean) => {
    setErrorMsg(null);
    try {
      await api.post("/api/admin/training/run", {
        enforce_threshold: enforceThreshold,
      });
      void qc.invalidateQueries({ queryKey: ["training-runs"] });
      void qc.invalidateQueries({ queryKey: ["training-stats"] });
    } catch (err) {
      setErrorMsg(
        (err as { body?: { detail?: string }; message?: string })?.body?.detail
          ?? (err as Error).message
          ?? "failed to start run",
      );
    }
  };

  const promote = async (runId: string) => {
    setBusyRunId(runId);
    setErrorMsg(null);
    try {
      await api.post(`/api/admin/training/runs/${runId}/promote`, {
        adapter_id: 0,
        scale: 1.0,
      });
      void qc.invalidateQueries({ queryKey: ["training-runs"] });
    } catch (err) {
      setErrorMsg(
        (err as { body?: { detail?: string }; message?: string })?.body?.detail
          ?? (err as Error).message
          ?? "promote failed",
      );
    } finally {
      setBusyRunId(null);
    }
  };

  const progressPct = createMemo(() => {
    const s = stats.data;
    if (!s) return 0;
    return Math.min(100, Math.round((s.untrained / s.train_threshold) * 100));
  });

  return (
    <div data-mode="system" class="flex h-full flex-col">
      <TopBar
        title={t("training.title")}
        subtitle={t("training.subtitle")}
        actions={
          <>
            <Show when={stats.isFetching || pairs.isFetching || runs.isFetching}>
              <Spinner size={14} />
            </Show>
            <Button variant="secondary" size="sm" onClick={refresh}>
              {t("common.refresh")}
            </Button>
          </>
        }
      />

      <div class="flex-1 overflow-y-auto px-6 py-6">
        <div class="mx-auto max-w-5xl space-y-6">
          <Show
            when={stats.data}
            fallback={
              <div class="flex h-32 items-center justify-center text-sm text-text-subtle">
                <Spinner size={18} />
              </div>
            }
          >
            {(s) => (
              <section
                aria-labelledby="pool-heading"
                class="space-y-3 rounded-md border border-border-subtle bg-bg-elevated p-4"
              >
                <div class="flex items-baseline justify-between">
                  <h2
                    id="pool-heading"
                    class="text-sm font-semibold tracking-tight"
                  >
                    {t("training.pool.heading")}
                  </h2>
                  <span class="text-xs text-text-subtle tabular-nums">
                    {t("training.pool.untrained_of", {
                      n: s().untrained,
                      total: s().train_threshold,
                    })}
                  </span>
                </div>
                <div
                  class="h-2 w-full overflow-hidden rounded-full bg-bg-elevated-v25"
                  role="progressbar"
                  aria-valuemin={0}
                  aria-valuemax={s().train_threshold}
                  aria-valuenow={s().untrained}
                  aria-label={t("training.pool.progress_aria")}
                >
                  <div
                    class="h-full bg-text-display transition-[width]"
                    style={{ width: `${progressPct()}%` }}
                  />
                </div>
                <div class="grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
                  <Stat label={t("training.pool.stat.total")}     value={s().total} />
                  <Stat label={t("training.pool.stat.untrained")} value={s().untrained} />
                  <Stat label={t("training.pool.stat.opt_in")}    value={s().opt_in_raw} />
                  <Stat
                    label={t("training.pool.stat.ready")}
                    value={
                      s().ready_to_train
                        ? t("training.pool.stat.yes")
                        : t("training.pool.stat.no")
                    }
                  />
                </div>
                <Show when={Object.keys(s().by_mode).length > 0}>
                  <div class="flex flex-wrap gap-1.5 pt-1">
                    <For each={Object.entries(s().by_mode)}>
                      {([mode, n]) => (
                        <Badge>
                          {mode}: {n}
                        </Badge>
                      )}
                    </For>
                  </div>
                </Show>

                <div class="flex items-center gap-2 pt-2">
                  <Button
                    onClick={() => startRun(true)}
                    disabled={!s().ready_to_train}
                    title={
                      s().ready_to_train
                        ? t("training.pool.train_ready")
                        : t("training.pool.train_disabled", {
                            n: s().train_threshold - s().untrained,
                          })
                    }
                  >
                    {t("training.pool.train_button")}
                  </Button>
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => startRun(false)}
                    title={t("training.pool.smoke_title")}
                  >
                    {t("training.pool.smoke_button")}
                  </Button>
                </div>
              </section>
            )}
          </Show>

          <Show when={errorMsg()}>
            <div
              role="alert"
              class="rounded-md border border-status-error/40 bg-status-error/10 px-3 py-2 text-xs text-status-error"
            >
              {errorMsg()}
            </div>
          </Show>

          <section
            aria-labelledby="samples-heading"
            class="space-y-2"
          >
            <div class="flex items-baseline justify-between">
              <h2
                id="samples-heading"
                class="text-sm font-semibold tracking-tight"
              >
                {t("training.samples.heading")}
              </h2>
              <span class="text-xs text-text-subtle">
                {t("training.samples.count", { n: pairs.data?.count ?? 0 })}
              </span>
            </div>
            <Show
              when={(pairs.data?.items.length ?? 0) > 0}
              fallback={
                <p class="rounded-md border border-border-subtle bg-bg-elevated px-4 py-6 text-center text-xs text-text-subtle">
                  {t("training.samples.empty")}
                </p>
              }
            >
              <ul class="overflow-hidden rounded-md border border-border-subtle bg-bg-elevated">
                <For each={pairs.data?.items ?? []}>
                  {(p) => (
                    <li class="border-b border-border-subtle p-3 text-xs last:border-b-0">
                      <div class="mb-1 flex items-center gap-2">
                        <Badge>{p.mode}</Badge>
                        <Show when={p.opt_in_raw}>
                          <Badge>{t("training.samples.opt_in_badge")}</Badge>
                        </Show>
                        <span class="text-text-subtle tabular-nums">
                          {new Date(p.created_at).toLocaleString()}
                        </span>
                        <code class="ml-auto truncate text-[0.65rem] text-text-subtle">
                          {p.code_hash.slice(0, 12)}…
                        </code>
                      </div>
                      <Show
                        when={p.opt_in_raw && (p.prompt || p.chosen)}
                        fallback={
                          <p class="text-text-subtle italic">
                            {t("training.samples.hash_only")}
                          </p>
                        }
                      >
                        <p class="line-clamp-2 text-text-body">
                          <span class="text-text-subtle">{t("training.samples.prompt")}</span>
                          {p.prompt}
                        </p>
                        <Show when={p.chosen}>
                          <p class="mt-1 line-clamp-2 text-text-body">
                            <span class="text-text-subtle">{t("training.samples.chosen")}</span>
                            {p.chosen}
                          </p>
                        </Show>
                        <Show when={p.rejected}>
                          <p class="mt-1 line-clamp-2 text-text-body">
                            <span class="text-text-subtle">{t("training.samples.rejected")}</span>
                            {p.rejected}
                          </p>
                        </Show>
                      </Show>
                    </li>
                  )}
                </For>
              </ul>
            </Show>
          </section>

          <section
            aria-labelledby="runs-heading"
            class="space-y-2"
          >
            <div class="flex items-baseline justify-between">
              <h2
                id="runs-heading"
                class="text-sm font-semibold tracking-tight"
              >
                {t("training.runs.heading")}
              </h2>
              <span class="text-xs text-text-subtle">
                {t("training.runs.count", { n: runs.data?.count ?? 0 })}
              </span>
            </div>
            <Show
              when={(runs.data?.items.length ?? 0) > 0}
              fallback={
                <p class="rounded-md border border-border-subtle bg-bg-elevated px-4 py-6 text-center text-xs text-text-subtle">
                  {t("training.runs.empty")}
                </p>
              }
            >
              <ul class="space-y-2">
                <For each={runs.data?.items ?? []}>
                  {(run) => (
                    <li class="space-y-2 rounded-md border border-border-subtle bg-bg-elevated p-3 text-xs">
                      <div class="flex items-baseline justify-between gap-2">
                        <div class="flex items-baseline gap-2">
                          <Badge>{run.status}</Badge>
                          <code class="text-text-subtle">
                            #{run.id.slice(0, 8)}
                          </code>
                          <span class="text-text-subtle">
                            {new Date(run.started_at).toLocaleString()}
                          </span>
                        </div>
                        <span class="text-text-subtle tabular-nums">
                          {t("training.runs.pair_count", { n: run.pair_count })}
                        </span>
                      </div>
                      <Show when={run.eval_summary}>
                        {(s) => (
                          <div class="grid grid-cols-2 gap-2 text-[0.7rem] sm:grid-cols-4">
                            <Stat
                              label={t("training.runs.stat.mean")}
                              value={
                                s().mean_judge_delta != null
                                  ? Number(s().mean_judge_delta).toFixed(2)
                                  : "—"
                              }
                            />
                            <Stat
                              label={t("training.runs.stat.worst")}
                              value={
                                s().worst_judge_delta != null
                                  ? Number(s().worst_judge_delta).toFixed(2)
                                  : "—"
                              }
                            />
                            <Stat
                              label={t("training.runs.stat.p50")}
                              value={
                                s().p50_latency_pct != null
                                  ? `${Number(s().p50_latency_pct).toFixed(1)}%`
                                  : "—"
                              }
                            />
                            <Stat
                              label={t("training.runs.stat.gate")}
                              value={
                                s().promote_ok
                                  ? t("training.runs.gate_ok")
                                  : t("training.runs.gate_blocked")
                              }
                            />
                          </div>
                        )}
                      </Show>
                      <Show when={run.note}>
                        <p class="text-text-subtle">{run.note}</p>
                      </Show>
                      <Show when={run.status === "evaluated"}>
                        <Button
                          size="sm"
                          onClick={() => promote(run.id)}
                          disabled={
                            busyRunId() === run.id
                            || !run.eval_summary?.promote_ok
                          }
                          title={
                            run.eval_summary?.promote_ok
                              ? t("training.runs.promote_ok_title")
                              : t("training.runs.promote_blocked_title")
                          }
                        >
                          {busyRunId() === run.id
                            ? t("training.runs.promoting")
                            : t("training.runs.promote")}
                        </Button>
                      </Show>
                    </li>
                  )}
                </For>
              </ul>
            </Show>
          </section>
        </div>
      </div>
    </div>
  );
};


const Stat: Component<{
  label: string;
  value: string | number;
}> = (props) => (
  <div class="rounded border border-border-subtle bg-bg-elevated-v25 px-2 py-1">
    <span class="block text-[0.6rem] uppercase tracking-wide text-text-subtle">
      {props.label}
    </span>
    <span class="block font-mono text-sm text-text-display">
      {props.value}
    </span>
  </div>
);
