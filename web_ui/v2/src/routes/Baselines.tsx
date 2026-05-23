import {
  type Component,
  For,
  Show,
  createMemo,
  createSignal,
} from "solid-js";
import { createQuery } from "@tanstack/solid-query";

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

/* Schema: tests/baselines/sprint0_schema.json
 * GET /api/admin/baselines/latest returns {meta, rows[]}; meta is null
 * when no run has been recorded yet (UI shows the empty state). */

interface JudgeScore {
  correctness?: number;
  completeness?: number;
  uncertain?: boolean;
  rationale?: string;
  error?: string;
}

interface BaselineMetrics {
  wall_clock_ms: number;
  first_token_ms: number | null;
  prompt_tokens: number;
  completion_tokens: number;
  peak_vram_mb: number | null;
  tool_calls: number;
  retries: number;
  phase_timings_ms?: Record<string, number>;
}

interface BaselineRow {
  prompt_id: string;
  mode: "Build" | "Research" | "Thinking" | "Consortium" | "Sentinel" | "System";
  prompt: string;
  started_utc: string;
  finished_utc: string;
  session_id: string | null;
  status: "completed" | "failed" | "cancelled" | "timeout";
  error: string | null;
  metrics: BaselineMetrics;
  output: string;
  judge_score: JudgeScore | null;
}

interface BaselineMeta {
  schema_version: string;
  run_id: string;
  started_utc: string;
  finished_utc: string;
  backend: string;
  models_used: Record<string, string>;
  git_sha: string | null;
  host: string;
  judge?: {
    model_path?: string;
    method?: string;
    rubrics?: string[];
    position_swap?: boolean;
    base_url?: string;
  } | null;
  notes?: string;
}

interface BaselinePayload {
  meta: BaselineMeta | null;
  rows: BaselineRow[];
}

type SortKey =
  | "prompt_id"
  | "mode"
  | "wall_clock_ms"
  | "first_token_ms"
  | "tokens_total"
  | "peak_vram_mb"
  | "retries"
  | "judge_avg"
  | "status";

const STATUS_TO_PILL: Record<BaselineRow["status"], Status> = {
  completed: "healthy",
  failed: "failed",
  cancelled: "warning",
  timeout: "warning",
};

const formatMs = (ms: number | null | undefined): string => {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
};

const formatVram = (mb: number | null | undefined): string => {
  if (mb == null) return "—";
  if (mb < 1024) return `${mb} MB`;
  return `${(mb / 1024).toFixed(1)} GB`;
};

const judgeAvg = (j: JudgeScore | null | undefined): number | null => {
  if (!j || j.correctness == null || j.completeness == null) return null;
  return (j.correctness + j.completeness) / 2;
};

const compareNumber = (a: number | null, b: number | null): number => {
  if (a == null && b == null) return 0;
  if (a == null) return 1; // nulls last
  if (b == null) return -1;
  return a - b;
};

export const Baselines: Component = () => {
  const [sortKey, setSortKey] = createSignal<SortKey>("prompt_id");
  const [sortDir, setSortDir] = createSignal<"asc" | "desc">("asc");

  const q = createQuery<BaselinePayload>(() => ({
    queryKey: ["admin", "baselines", "latest"],
    queryFn: () => api.get<BaselinePayload>("/api/admin/baselines/latest"),
    refetchInterval: 30_000,
  }));

  const sortedRows = createMemo<BaselineRow[]>(() => {
    const rows = q.data?.rows ?? [];
    const k = sortKey();
    const dir = sortDir() === "asc" ? 1 : -1;
    const out = [...rows];
    out.sort((a, b) => {
      let cmp = 0;
      switch (k) {
        case "prompt_id":
          cmp = a.prompt_id.localeCompare(b.prompt_id);
          break;
        case "mode":
          cmp = a.mode.localeCompare(b.mode);
          break;
        case "wall_clock_ms":
          cmp = compareNumber(a.metrics.wall_clock_ms, b.metrics.wall_clock_ms);
          break;
        case "first_token_ms":
          cmp = compareNumber(a.metrics.first_token_ms, b.metrics.first_token_ms);
          break;
        case "tokens_total":
          cmp = compareNumber(
            a.metrics.prompt_tokens + a.metrics.completion_tokens,
            b.metrics.prompt_tokens + b.metrics.completion_tokens,
          );
          break;
        case "peak_vram_mb":
          cmp = compareNumber(a.metrics.peak_vram_mb, b.metrics.peak_vram_mb);
          break;
        case "retries":
          cmp = compareNumber(a.metrics.retries, b.metrics.retries);
          break;
        case "judge_avg":
          cmp = compareNumber(judgeAvg(a.judge_score), judgeAvg(b.judge_score));
          break;
        case "status":
          cmp = a.status.localeCompare(b.status);
          break;
      }
      return cmp * dir;
    });
    return out;
  });

  const summary = createMemo(() => {
    const rows = q.data?.rows ?? [];
    const completed = rows.filter((r) => r.status === "completed").length;
    const judged = rows.filter(
      (r) =>
        r.judge_score &&
        r.judge_score.correctness != null &&
        r.judge_score.completeness != null,
    );
    const judgeMean =
      judged.length === 0
        ? null
        : judged.reduce((acc, r) => acc + (judgeAvg(r.judge_score) ?? 0), 0) /
          judged.length;
    const totalWall = rows.reduce((acc, r) => acc + r.metrics.wall_clock_ms, 0);
    return {
      total: rows.length,
      completed,
      judged: judged.length,
      judge_mean: judgeMean,
      total_wall_ms: totalWall,
      uncertain: judged.filter((r) => r.judge_score?.uncertain).length,
    };
  });

  const toggleSort = (k: SortKey) => () => {
    if (sortKey() === k) {
      setSortDir(sortDir() === "asc" ? "desc" : "asc");
    } else {
      setSortKey(k);
      setSortDir("asc");
    }
  };

  const sortIndicator = (k: SortKey) => {
    if (sortKey() !== k) return "";
    return sortDir() === "asc" ? " ▲" : " ▼";
  };

  return (
    <div class="flex h-full flex-col">
      <TopBar
        title={t("baselines.title")}
        subtitle={t("baselines.subtitle")}
        actions={
          <>
            <Show
              when={q.data?.meta}
              fallback={<StatusPill status="warning" label={t("baselines.card.no_judge")} size="sm" />}
            >
              {(meta) => (
                <Badge>
                  {t("baselines.run_label", {
                    backend: meta().backend,
                    id: meta().run_id.slice(0, 8),
                  })}
                </Badge>
              )}
            </Show>
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
          when={!q.isLoading}
          fallback={
            <div class="flex h-64 items-center justify-center text-text-subtle">
              <Spinner size={20} />
            </div>
          }
        >
          <Show
            when={(q.data?.rows?.length ?? 0) > 0}
            fallback={
              <div class="rounded-lg border border-border-subtle bg-bg-elevated p-8 text-center">
                <p class="text-sm text-text-body">
                  No baseline recorded yet.
                </p>
                <p class="mt-2 text-xs text-text-subtle">
                  Run{" "}
                  <code class="rounded bg-bg-elevated-v25 px-1 py-0.5 font-mono">
                    python tools/run_sprint0_baseline.py
                  </code>{" "}
                  to capture the first snapshot.
                </p>
              </div>
            }
          >
            {/* Summary strip */}
            <div class="mb-6 grid grid-cols-2 gap-3 md:grid-cols-5">
              <SummaryCell
                label={t("baselines.card.prompts")}
                value={`${summary().completed}/${summary().total}`}
                hint={t("baselines.card.prompts_sub")}
              />
              <SummaryCell
                label={t("baselines.card.total_wall")}
                value={formatMs(summary().total_wall_ms)}
                hint={t("baselines.card.total_wall_sub")}
              />
              <SummaryCell
                label={t("baselines.card.judge_mean")}
                value={
                  summary().judge_mean == null
                    ? "—"
                    : (summary().judge_mean as number).toFixed(2)
                }
                hint={t("baselines.card.judge_mean_sub", {
                  judged: summary().judged,
                  uncertain: summary().uncertain,
                })}
              />
              <SummaryCell
                label={t("baselines.card.backend")}
                value={q.data?.meta?.backend ?? "—"}
                hint={q.data?.meta?.judge?.method ?? t("baselines.card.no_judge")}
              />
              <SummaryCell
                label={t("baselines.card.host")}
                value={q.data?.meta?.host ?? "—"}
                hint={q.data?.meta?.git_sha?.slice(0, 7) ?? t("baselines.card.no_git_sha")}
              />
            </div>

            {/* Table */}
            <div class="overflow-x-auto rounded-lg border border-border-subtle">
              <table class="min-w-full text-sm">
                <thead class="bg-bg-elevated-v25 text-text-subtle">
                  <tr>
                    <Th onClick={toggleSort("prompt_id")}>
                      {t("baselines.col.prompt")}{sortIndicator("prompt_id")}
                    </Th>
                    <Th onClick={toggleSort("mode")}>
                      {t("baselines.col.mode")}{sortIndicator("mode")}
                    </Th>
                    <Th onClick={toggleSort("status")} numeric>
                      {t("baselines.col.status")}{sortIndicator("status")}
                    </Th>
                    <Th onClick={toggleSort("wall_clock_ms")} numeric>
                      {t("baselines.col.wall")}{sortIndicator("wall_clock_ms")}
                    </Th>
                    <Th onClick={toggleSort("first_token_ms")} numeric>
                      {t("baselines.col.ftt")}{sortIndicator("first_token_ms")}
                    </Th>
                    <Th onClick={toggleSort("tokens_total")} numeric>
                      {t("baselines.col.tokens")}{sortIndicator("tokens_total")}
                    </Th>
                    <Th onClick={toggleSort("peak_vram_mb")} numeric>
                      {t("baselines.col.vram")}{sortIndicator("peak_vram_mb")}
                    </Th>
                    <Th onClick={toggleSort("retries")} numeric>
                      {t("baselines.col.retries")}{sortIndicator("retries")}
                    </Th>
                    <Th onClick={toggleSort("judge_avg")} numeric>
                      {t("baselines.col.judge")}{sortIndicator("judge_avg")}
                    </Th>
                  </tr>
                </thead>
                <tbody>
                  <For each={sortedRows()}>
                    {(row) => <Row row={row} />}
                  </For>
                </tbody>
              </table>
            </div>

            <p class="mt-4 text-xs text-text-subtle">
              {t("common.auto_refresh", { seconds: 30 })}.{" "}
              {t("common.sources")}:{" "}
              <code class="font-mono">data/baselines/sprint0_latest.json</code>
              .
            </p>
          </Show>
        </Show>
      </div>
    </div>
  );
};

const Th: Component<{
  children: any;
  onClick?: () => void;
  numeric?: boolean;
}> = (props) => (
  <th
    class={[
      "select-none border-b border-border-subtle px-3 py-2 text-left text-[0.7rem] font-medium uppercase tracking-wider",
      props.onClick ? "cursor-pointer hover:bg-bg-elevated-v25" : "",
      props.numeric ? "text-right tabular-nums" : "",
    ].join(" ")}
    onClick={props.onClick}
  >
    {props.children}
  </th>
);

const Row: Component<{ row: BaselineRow }> = (props) => {
  const r = () => props.row;
  return (
    <tr class="border-b border-border-subtle hover:bg-bg-elevated-v25">
      <td class="max-w-xs px-3 py-2">
        <span class="block font-mono text-xs">{r().prompt_id}</span>
        <span
          class="block max-w-[40ch] truncate text-[0.7rem] text-text-subtle"
          title={r().prompt}
        >
          {r().prompt}
        </span>
      </td>
      <td class="px-3 py-2">
        <Badge>{r().mode}</Badge>
      </td>
      <td class="px-3 py-2 text-right">
        <StatusPill
          status={STATUS_TO_PILL[r().status]}
          label={r().status}
          size="sm"
        />
        <Show when={r().error}>
          <span
            class="ml-2 inline-block max-w-[24ch] truncate text-[0.7rem] text-text-subtle"
            title={r().error ?? ""}
          >
            {r().error}
          </span>
        </Show>
      </td>
      <td class="px-3 py-2 text-right tabular-nums">
        {formatMs(r().metrics.wall_clock_ms)}
      </td>
      <td class="px-3 py-2 text-right tabular-nums text-text-body">
        {formatMs(r().metrics.first_token_ms)}
      </td>
      <td class="px-3 py-2 text-right tabular-nums">
        <span title={`prompt: ${r().metrics.prompt_tokens} · completion: ${r().metrics.completion_tokens}`}>
          {r().metrics.prompt_tokens + r().metrics.completion_tokens}
        </span>
      </td>
      <td class="px-3 py-2 text-right tabular-nums text-text-body">
        {formatVram(r().metrics.peak_vram_mb)}
      </td>
      <td class="px-3 py-2 text-right tabular-nums">{r().metrics.retries}</td>
      <td class="px-3 py-2 text-right">
        <JudgeBadge judge={r().judge_score} />
      </td>
    </tr>
  );
};

const SummaryCell: Component<{
  label: string;
  value: string;
  hint?: string;
}> = (props) => (
  <div class="rounded-md border border-border-subtle bg-bg-elevated px-3 py-2">
    <p class="text-[0.65rem] uppercase tracking-wider text-text-subtle">
      {props.label}
    </p>
    <p class="mt-1 truncate font-mono text-sm text-text-display">
      {props.value}
    </p>
    <Show when={props.hint}>
      <p class="mt-0.5 truncate text-[0.65rem] text-text-subtle">
        {props.hint}
      </p>
    </Show>
  </div>
);

const JudgeBadge: Component<{ judge: JudgeScore | null }> = (props) => {
  return (
    <Show
      when={props.judge}
      fallback={<span class="text-text-subtle">—</span>}
    >
      {(judge) => (
        <Show
          when={
            judge().correctness != null && judge().completeness != null
          }
          fallback={
            <span
              class="text-[0.7rem] text-text-subtle"
              title={judge().error ?? ""}
            >
              {t("baselines.judge.err")}
            </span>
          }
        >
          <span
            class={
              judge().uncertain ? "text-text-body" : "text-text-display"
            }
            title={judge().rationale ?? ""}
          >
            <span class="font-mono">
              {judge().correctness}/{judge().completeness}
            </span>
            <Show when={judge().uncertain}>
              <span class="ml-1 text-[0.7rem] text-text-subtle">?</span>
            </Show>
          </span>
        </Show>
      )}
    </Show>
  );
};
