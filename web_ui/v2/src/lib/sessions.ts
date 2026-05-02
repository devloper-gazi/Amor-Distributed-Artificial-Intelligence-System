/**
 * Chat session API client.  Backend lives at ``/api/sessions/*``
 * (see ``document_processor/api/chat_sessions_routes.py``).  This
 * module is a thin wrapper that returns Promises — the consuming
 * components invoke it via TanStack Solid Query so the cache + retry
 * + refetch logic comes for free.
 */

import { api } from "./api";

export interface ChatSession {
  id: string;
  session_id?: string;
  mode?: string;
  title?: string;
  created_at?: string;
  updated_at?: string;
  archived?: boolean;
  pinned?: boolean;
  folder_id?: string | null;
  /** Some backends use ``message_count``; keep the type flexible. */
  message_count?: number;
}

interface ListResp {
  sessions: ChatSession[];
  total?: number;
}

export const sessions = {
  async list(opts: { skip?: number; limit?: number; archived?: boolean } = {}) {
    const qs = new URLSearchParams();
    if (opts.skip !== undefined) qs.set("skip", String(opts.skip));
    if (opts.limit !== undefined) qs.set("limit", String(opts.limit));
    if (opts.archived !== undefined)
      qs.set("archived", opts.archived ? "true" : "false");
    const path = `/api/sessions${qs.toString() ? "?" + qs.toString() : ""}`;
    return api.get<ListResp>(path);
  },

  async get(id: string) {
    return api.get<ChatSession & { messages?: unknown[] }>(
      `/api/sessions/${id}`,
    );
  },

  async update(
    id: string,
    patch: {
      title?: string;
      archived?: boolean;
      pinned?: boolean;
      folder_id?: string | null;
      mode?: string;
    },
  ) {
    return api.put(`/api/sessions/${id}`, patch);
  },

  async remove(id: string) {
    return api.del(`/api/sessions/${id}`);
  },
};
