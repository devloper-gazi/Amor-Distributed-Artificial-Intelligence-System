/**
 * Cycle C Sprint 7 Day 3 — admin Memory viewer.
 *
 * Surfaces the Mem0 OSS adapter's stored memories with a status
 * banner that explains how to flip the backend on when it's
 * unavailable.  Search box + per-row delete + "Add memory" textarea
 * cover the operator workflow:
 *
 *   * Diagnose: status card shows ``backend / vector / llm``
 *   * Inspect: list view shows the most recent ``limit`` records
 *   * Search: hybrid retrieval over the user's namespace
 *   * Curate: delete unwanted memories, add manual ones
 */

import {
  type Component,
  For,
  Show,
  createSignal,
} from "solid-js";
import { createQuery, useQueryClient } from "@tanstack/solid-query";

import { TopBar } from "../components/shell/TopBar";
import { Badge, Button, Input, Spinner } from "../components/ui";
import { api } from "../lib/api";
import { t } from "../i18n";


interface MemoryStatus {
  backend: "mem0" | "native";
  available: boolean;
  vector_store: string;
  history_db: string;
  llm_base_url: string | null;
  llm_model: string | null;
  graph_enabled: boolean;
  user_namespace: string;
}

interface MemoryItem {
  id: string;
  user_id: string;
  text: string;
  score: number | null;
  metadata: Record<string, unknown>;
  created_at: string | null;
  updated_at: string | null;
}

interface MemoryList {
  count: number;
  available: boolean;
  items: MemoryItem[];
}


export const Memory: Component = () => {
  const qc = useQueryClient();
  const [query, setQuery] = createSignal("");
  const [activeQuery, setActiveQuery] = createSignal("");
  const [draftText, setDraftText] = createSignal("");
  const [errorMsg, setErrorMsg] = createSignal<string | null>(null);

  const status = createQuery<MemoryStatus>(() => ({
    queryKey: ["memory-status"],
    queryFn: () => api.get<MemoryStatus>("/api/admin/memory/status"),
    refetchInterval: 60_000,
  }));

  const listAll = createQuery<MemoryList>(() => ({
    queryKey: ["memory-all"],
    queryFn: () => api.get<MemoryList>("/api/admin/memory/all?limit=100"),
    refetchInterval: 60_000,
    enabled: !activeQuery(),
  }));

  const searchHits = createQuery<MemoryList & { q: string }>(() => ({
    queryKey: ["memory-search", activeQuery()],
    queryFn: () =>
      api.get<MemoryList & { q: string }>(
        `/api/admin/memory/search?q=${encodeURIComponent(activeQuery())}&limit=20`,
      ),
    enabled: !!activeQuery(),
  }));

  const visible = (): MemoryList | undefined => {
    return activeQuery() ? searchHits.data : listAll.data;
  };

  const onSearch = (e: SubmitEvent) => {
    e.preventDefault();
    setErrorMsg(null);
    setActiveQuery(query().trim());
  };

  const clearSearch = () => {
    setQuery("");
    setActiveQuery("");
  };

  const onAdd = async (e: SubmitEvent) => {
    e.preventDefault();
    setErrorMsg(null);
    const text = draftText().trim();
    if (!text) return;
    try {
      await api.post("/api/admin/memory/add", { text });
      setDraftText("");
      void qc.invalidateQueries({ queryKey: ["memory-all"] });
      void qc.invalidateQueries({ queryKey: ["memory-search"] });
    } catch (err) {
      setErrorMsg(
        (err as { body?: { detail?: string }; message?: string })?.body?.detail
          ?? (err as Error).message
          ?? "add failed",
      );
    }
  };

  const onDelete = async (id: string) => {
    setErrorMsg(null);
    try {
      await api.del(`/api/admin/memory/${encodeURIComponent(id)}`);
      void qc.invalidateQueries({ queryKey: ["memory-all"] });
      void qc.invalidateQueries({ queryKey: ["memory-search"] });
    } catch (err) {
      setErrorMsg(
        (err as { body?: { detail?: string }; message?: string })?.body?.detail
          ?? (err as Error).message
          ?? "delete failed",
      );
    }
  };

  const refresh = () => {
    void status.refetch();
    void listAll.refetch();
    if (activeQuery()) void searchHits.refetch();
  };

  return (
    <div data-mode="system" class="flex h-full flex-col">
      <TopBar
        title={t("memory.title")}
        subtitle={t("memory.subtitle")}
        actions={
          <>
            <Show when={status.isFetching || listAll.isFetching || searchHits.isFetching}>
              <Spinner size={14} />
            </Show>
            <Button variant="secondary" size="sm" onClick={refresh}>
              {t("common.refresh")}
            </Button>
          </>
        }
      />

      <div class="flex-1 overflow-y-auto px-6 py-6">
        <div class="mx-auto max-w-4xl space-y-5">
          <Show
            when={status.data}
            fallback={
              <div class="flex h-32 items-center justify-center text-sm text-text-subtle">
                <Spinner size={18} />
              </div>
            }
          >
            {(s) => (
              <section class="rounded-md border border-border-subtle bg-bg-elevated p-4">
                <div class="flex items-baseline justify-between">
                  <h2 class="text-sm font-semibold tracking-tight">
                    {t("memory.status.heading")}
                  </h2>
                  <Badge>
                    {s().backend}{" · "}
                    {s().available
                      ? t("memory.status.ready")
                      : t("memory.status.disabled")}
                  </Badge>
                </div>
                <Show
                  when={!s().available}
                  fallback={
                    <p class="mt-2 text-xs text-text-subtle">
                      {t("memory.status.detail", {
                        vector: s().vector_store,
                        llm: s().llm_model ?? "—",
                        ns: s().user_namespace,
                        graph: s().graph_enabled ? "on" : "off",
                      })}
                    </p>
                  }
                >
                  <p class="mt-2 text-xs text-text-subtle">
                    {t("memory.status.disabled_hint")}
                  </p>
                </Show>
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

          <section class="space-y-2">
            <form onSubmit={onSearch} class="flex items-center gap-2">
              <Input
                value={query()}
                onInput={(e) => setQuery(e.currentTarget.value)}
                placeholder={t("memory.search.placeholder")}
                aria-label={t("memory.search.aria")}
                class="flex-1"
              />
              <Button type="submit" disabled={!query().trim()}>
                {t("memory.search.button")}
              </Button>
              <Show when={activeQuery()}>
                <Button variant="secondary" onClick={clearSearch} type="button">
                  {t("memory.search.clear")}
                </Button>
              </Show>
            </form>
            <Show when={activeQuery()}>
              <p class="text-xs text-text-subtle">
                {t("memory.search.showing", { q: activeQuery() })}
              </p>
            </Show>
          </section>

          <section
            class="overflow-hidden rounded-md border border-border-subtle bg-bg-elevated"
            aria-label={t("memory.list.aria")}
          >
            <Show
              when={visible() && (visible()!.count > 0)}
              fallback={
                <p class="px-4 py-6 text-center text-xs text-text-subtle">
                  <Show
                    when={visible()?.available}
                    fallback={t("memory.list.empty_disabled")}
                  >
                    <Show
                      when={activeQuery()}
                      fallback={t("memory.list.empty")}
                    >
                      {t("memory.list.empty_no_match")}
                    </Show>
                  </Show>
                </p>
              }
            >
              <ul>
                <For each={visible()!.items}>
                  {(m) => (
                    <li class="space-y-1 border-b border-border-subtle p-3 text-xs last:border-b-0">
                      <div class="flex items-baseline justify-between gap-2">
                        <code class="text-[0.65rem] text-text-subtle">
                          {m.id.slice(0, 12)}
                        </code>
                        <Show when={m.score !== null}>
                          <span class="text-[0.65rem] text-text-subtle tabular-nums">
                            {t("memory.list.score", { n: Number(m.score).toFixed(3) })}
                          </span>
                        </Show>
                        <Show when={m.created_at}>
                          <span class="text-[0.65rem] text-text-subtle">
                            {new Date(m.created_at!).toLocaleString()}
                          </span>
                        </Show>
                        <Button
                          variant="secondary"
                          size="sm"
                          onClick={() => onDelete(m.id)}
                          aria-label={t("memory.list.delete_aria", { id: m.id })}
                        >
                          {t("memory.list.delete_button")}
                        </Button>
                      </div>
                      <p class="text-text-body">{m.text}</p>
                    </li>
                  )}
                </For>
              </ul>
            </Show>
          </section>

          <section class="space-y-2">
            <h2 class="text-sm font-semibold tracking-tight">
              {t("memory.add.heading")}
            </h2>
            <form onSubmit={onAdd} class="flex flex-col gap-2">
              <textarea
                value={draftText()}
                onInput={(e) => setDraftText(e.currentTarget.value)}
                placeholder={t("memory.add.placeholder")}
                rows={2}
                class="w-full rounded-md border border-border-subtle bg-bg-elevated px-3 py-2 text-sm focus:outline-2 focus:outline-offset-2"
                aria-label={t("memory.add.aria")}
              />
              <Button type="submit" disabled={!draftText().trim()}>
                {t("memory.add.button")}
              </Button>
            </form>
          </section>
        </div>
      </div>
    </div>
  );
};
