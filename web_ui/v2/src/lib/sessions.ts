/**
 * Chat session API client.  Backend lives at ``/api/sessions/*``
 * (see ``document_processor/api/chat_sessions_routes.py``).
 *
 * Notes
 * -----
 * * The cross-mode listing endpoint is ``GET /api/sessions/all`` —
 *   ``GET /api/sessions`` requires a ``mode`` query param (422
 *   otherwise).
 * * Every endpoint requires the ``X-Client-Id`` header.  We keep one
 *   per browser in ``localStorage["amor.client_id"]``; first call
 *   generates a UUID v4 and stashes it.
 */

import { api } from "./api";

// X-Client-Id is now attached automatically by ``api`` for every
// request (see ``api.ts:call``), so this module no longer needs
// its own header helpers.

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
  async list(opts: { offset?: number; limit?: number; archived?: boolean } = {}) {
    const qs = new URLSearchParams();
    if (opts.offset !== undefined) qs.set("offset", String(opts.offset));
    if (opts.limit !== undefined) qs.set("limit", String(opts.limit));
    if (opts.archived !== undefined)
      qs.set("include_archived", opts.archived ? "true" : "false");
    const path = `/api/sessions/all${qs.toString() ? "?" + qs.toString() : ""}`;
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
    // Backend uses PATCH, not PUT (see ``chat_sessions_routes.py:365``);
    // a PUT request returns 405 Method Not Allowed.
    return api.patch(`/api/sessions/${id}`, patch);
  },

  async remove(id: string) {
    return api.del(`/api/sessions/${id}`);
  },
};
