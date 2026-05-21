import { type Component, For, Show } from "solid-js";
import { createQuery } from "@tanstack/solid-query";
import { TopBar } from "../components/shell/TopBar";
import {
  Button,
  Spinner,
  StatusPill,
  type Status,
} from "../components/ui";
import { api } from "../lib/api";
import { t } from "../i18n";

interface Diagnostics {
  ts: string;
  backend: { kind: string; url: string; class: string };
  backend_health: { healthy: boolean };
  models: {
    installed: string[];
    role_assignment: Record<string, string>;
    distinct_count: number;
    vram_usage_estimate_gb: number;
  };
  sandbox: {
    workdir_root?: string;
    named_volume?: string;
    cold_start_p50_ms: number | null;
    cold_start_p95_ms: number | null;
    samples: number;
    recent_failures: Array<{ ts: string; where: string; detail: string }>;
    docker_available?: boolean | null;
    /** Cycle C Sprint 5 Ã¢â‚¬â€ sandbox hardening posture. */
    security?: {
      docker_host: string;
      via_proxy: boolean;
      flags_active: Record<string, unknown>;
      score: number;
      level: "baseline" | "hardened" | "max";
    };
  };
  rag: {
    embedder: string;
    hybrid_enabled: boolean;
    reranker_enabled: boolean;
  };
  ledger: {
    intact: boolean;
    entries: number;
    tail_hash: string;
  };
  phase16_facade: {
    openai_compat_enabled: boolean;
    mcp_server_enabled: boolean;
    llm_backend: string;
  };
  recent_sessions: Array<{
    sid?: string;
    session_id?: string;
    status?: string;
    duration_ms?: number;
    models_used?: Record<string, string>;
  }>;
  recent_failures: Array<{ where: string; detail: string }>;
}

export const Diagnostics: Component = () => {
  const q = createQuery<Diagnostics>(() => ({
    queryKey: ["diagnostics"],
    queryFn: () => api.get<Diagnostics>("/api/code/diagnostics"),
    refetchInterval: 30_000,
    refetchOnWindowFocus: false,
  }));

  const refresh = () => void q.refetch();

  return (
    <div data-mode="system" class="flex h-full flex-col">
      <TopBar
        title={t("diagnostics.title")}
        subtitle={t("diagnostics.subtitle")}
        actions={
          <>
            <Show when={q.isFetching}>
              <Spinner size={14} />
            </Show>
            <Button variant="secondary" size="sm" onClick={refresh}>
              {t("common.refresh")}
            </Button>
          </>
        }
      />
      <div class="flex-1 overflow-y-auto px-6 py-6">
        <Show
          when={q.data}
          fallback={
            <div class="flex h-full items-center justify-center text-sm text-text-subtle">
              <Show when={q.isError} fallback={<Spinner size={20} />}>
                <p>
                  {t("diagnostics.failed_to_load", {
                    detail: String(
                      (q.error as { body?: { detail?: string } } | null)
                        ?.body?.detail ?? q.error,
                    ),
                  })}
                </p>
              </Show>
            </div>
          }
        >
          {(d) => (
            <div class="mx-auto max-w-5xl space-y-6">
              <p class="text-xs text-text-subtle">
                {t("diagnostics.last_refreshed", {
                  ts: new Date(d().ts).toLocaleTimeString(),
                })}
              </p>

              {/* Card grid */}
              <div class="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
                <Card
                  status={d().backend_health.healthy ? "healthy" : "failed"}
                  title={t("diagnostics.card.backend")}
                  primary={d().backend.kind}
                  secondary={d().backend.url}
                />
                <Card
                  status={
                    d().sandbox.cold_start_p50_ms !== null
                      ? "healthy"
                      : "warming"
                  }
                  title={t("diagnostics.card.sandbox")}
                  primary={
                    d().sandbox.cold_start_p50_ms !== null
                      ? `${d().sandbox.cold_start_p50_ms} ms p50`
                      : t("diagnostics.value.no_samples")
                  }
                  secondary={
                    d().sandbox.cold_start_p95_ms !== null
                      ? `${d().sandbox.cold_start_p95_ms} ms p95 Ã‚Â· ${d().sandbox.samples} runs`
                      : t("diagnostics.value.run_to_populate")
                  }
                />
                <Card
                  status={d().ledger.intact ? "healthy" : "failed"}
                  title={t("diagnostics.card.ledger")}
                  primary={d().ledger.intact ? t("diagnostics.value.intact") : "broken"}
                  secondary={t("diagnostics.value.tail", {
                    tail: d().ledger.tail_hash.slice(0, 12) + "Ã¢â‚¬Â¦",
                    n: d().ledger.entries,
                  })}
                />
                <Card
                  status={
                    d().models.distinct_count >= 2 ? "healthy" : "warming"
                  }
                  title={t("diagnostics.card.models")}
                  primary={t("diagnostics.value.installed", {
                    n: d().models.installed.length,
                  })}
                  secondary={t("diagnostics.value.role_plan", {
                    n: d().models.distinct_count,
                  })}
                />
                <Card
                  status={d().rag.hybrid_enabled ? "healthy" : "warming"}
                  title={t("diagnostics.card.rag")}
                  primary={d().rag.embedder.split("/").pop() ?? "embedder"}
                  secondary={[
                    d().rag.hybrid_enabled ? t("diagnostics.value.hybrid") : "vector-only",
                    d().rag.reranker_enabled ? "reranker on" : null,
                  ]
                    .filter(Boolean)
                    .join(" Ã‚Â· ")}
                />
                <Card
                  status="healthy"
                  title={t("diagnostics.card.openai")}
                  primary={
                    d().phase16_facade.openai_compat_enabled
                      ? t("diagnostics.value.on")
                      : t("diagnostics.value.off")
                  }
                  secondary={`backend: ${d().phase16_facade.llm_backend}`}
                />
                <Card
                  status={
                    d().phase16_facade.mcp_server_enabled
                      ? "healthy"
                      : "warming"
                  }
                  title={t("diagnostics.card.mcp")}
                  primary={
                    d().phase16_facade.mcp_server_enabled
                      ? t("diagnostics.value.on")
                      : t("diagnostics.value.off")
                  }
                  secondary={
                    d().phase16_facade.mcp_server_enabled
                      ? "tools/list, tools/call"
                      : t("diagnostics.value.set_flag", { flag: "enable_mcp_server" })
                  }
                />
                <Card
                  status={
                    d().recent_failures.length > 0 ? "warning" : "healthy"
                  }
                  title={t("diagnostics.card.failures")}
                  primary={`${d().recent_failures.length}`}
                  secondary={t("diagnostics.value.last_n", { n: 30 })}
                />
                <Show when={d().sandbox.security}>
                  {(sec) => (
                    <Card
                      status={
                        sec().level === "max"
                          ? "healthy"
                          : sec().level === "hardened"
                            ? "healthy"
                            : "warning"
                      }
                      title={t("diagnostics.card.security")}
                      primary={`${sec().level} (${sec().score}/10)`}
                      secondary={
                        sec().via_proxy
                          ? t("diagnostics.value.socket_proxy")
                          : t("diagnostics.value.direct_socket")
                      }
                    />
                  )}
                </Show>
              </div>

              {/* Role assignment table */}
              <section>
                <h2 class="mb-2 text-sm font-semibold tracking-tight">
                  {t("diagnostics.role_assignments")}
                </h2>
                <div class="overflow-hidden rounded-md border border-border-subtle bg-bg-elevated">
                  <For each={Object.entries(d().models.role_assignment)}>
                    {([role, tag]) => (
                      <div class="flex items-center justify-between border-b border-border-subtle px-4 py-2 text-sm last:border-b-0">
                        <span class="font-medium">{role}</span>
                        <code class="text-xs text-text-body">{tag}</code>
                      </div>
                    )}
                  </For>
                </div>
              </section>

              {/* Recent sessions */}
              <Show when={d().recent_sessions.length > 0}>
                <section>
                  <h2 class="mb-2 text-sm font-semibold tracking-tight">
                    {t("diagnostics.recent_sessions")}
                  </h2>
                  <div class="overflow-hidden rounded-md border border-border-subtle bg-bg-elevated">
                    <For each={d().recent_sessions.slice(0, 8)}>
                      {(s) => (
                        <div class="flex items-center justify-between gap-3 border-b border-border-subtle px-4 py-2 text-sm last:border-b-0">
                          <code class="text-xs text-text-subtle">
                            #{(s.session_id ?? s.sid ?? "?").slice(0, 8)}
                          </code>
                          <span class="flex-1 text-text-body">
                            {s.status ?? "Ã¢â‚¬â€"}
                          </span>
                          <span class="text-xs text-text-subtle">
                            {s.duration_ms
                              ? `${Math.round(s.duration_ms / 1000)} s`
                              : ""}
                          </span>
                        </div>
                      )}
                    </For>
                  </div>
                </section>
              </Show>

              {/* Recent failures */}
              <Show when={d().recent_failures.length > 0}>
                <section>
                  <h2 class="mb-2 text-sm font-semibold tracking-tight">
                    Recent failures
                  </h2>
                  <div class="overflow-hidden rounded-md border border-border-subtle bg-bg-elevated">
                    <For each={d().recent_failures.slice(0, 8)}>
                      {(f) => (
                        <div class="border-b border-border-subtle px-4 py-2 text-sm last:border-b-0">
                          <p class="text-xs text-text-subtle">{f.where}</p>
                          <p class="mt-0.5 text-text-body">{f.detail}</p>
                        </div>
                      )}
                    </For>
                  </div>
                </section>
              </Show>
            </div>
          )}
        </Show>
      </div>
    </div>
  );
};

const Card: Component<{
  status: Status;
  title: string;
  primary: string;
  secondary?: string;
}> = (props) => (
  <div class="rounded-lg border border-border-subtle bg-bg-elevated p-4">
    <div class="flex items-center justify-between">
      <span class="text-xs font-medium uppercase tracking-wide text-text-subtle">
        {props.title}
      </span>
      <StatusPill status={props.status} size="sm" label="" />
    </div>
    <p class="mt-2 text-base font-semibold tracking-tight">{props.primary}</p>
    <Show when={props.secondary}>
      <p class="mt-0.5 text-xs text-text-subtle">{props.secondary}</p>
    </Show>
  </div>
);
