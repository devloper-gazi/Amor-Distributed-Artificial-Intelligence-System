// Chat Controller - Mode-Agnostic Chat Interface
// Supports: Research, Thinking, Coding modes

const API_ENDPOINTS = {
    research: {
        local: '/api/local-ai/research',
        claude: '/api/chat/research'
    },
    thinking: {
        local: '/api/local-ai/thinking',
        claude: '/api/chat/thinking'
    },
    coding: {
        local: '/api/local-ai/coding',
        claude: '/api/chat/coding'
    }
};

class ChatController {
    constructor(mode = 'research') {
        this.mode = mode;
        this.messagesArea = document.getElementById('messagesArea');
        this.chatInput = document.getElementById('chatInput');
        this.sendButton = document.getElementById('sendButton');
        this.useClaudeAPI = document.getElementById('useClaudeAPI');
        this.characterCount = document.getElementById('characterCount');
        this.aiIndicator = document.getElementById('aiModeText');
        this.progressModal = document.getElementById('progressModal');

        this.currentSessionId = null;
        // MongoDB-backed chat session id (separate from Local AI "research session_id")
        this.chatSessionId = null;
        this.isProcessing = false;
        this.messageHistory = [];

        // Phase C2/E — visible Stop button + per-query state used by
        // cancel + persistence helpers.
        this.stopButton = document.getElementById('stopButton');
        this._currentAbortController = null;
        this._currentQueryRecordId = null;
        this._currentUserMsgKey = null;
        this._currentAssistantMsgKey = null;
        // Backend pipeline session ids — distinct from `chatSessionId`
        // (Mongo) and from `query_record_id` (cross-replica logical id).
        // The mode-specific cancel routes need these to actually halt
        // the worker; the resume banner uses the matching id from the
        // query_record itself.
        this._currentThinkingBackendId = null;
        this._currentResearchBackendId = null;

        // Expose open/close handlers for settings panel (wired during init)
        this.openResearchSettingsPanel = null;
        this.closeResearchSettingsPanel = null;

        this.init();
    }

    init() {
        // Event listeners
        this.sendButton?.addEventListener('click', () => this.sendMessage());
        // Phase C2/E — Stop button cancels the in-flight query end-to-end.
        this.stopButton?.addEventListener('click', () => this.cancelCurrentQuery());
        this.chatInput?.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                this.sendMessage();
            }
        });
        this.chatInput?.addEventListener('input', () => this.updateCharacterCount());
        this.useClaudeAPI?.addEventListener('change', () => this.updateAIMode());

        // Auto-resize textarea
        this.chatInput?.addEventListener('input', () => {
            this.chatInput.style.height = 'auto';
            this.chatInput.style.height = this.chatInput.scrollHeight + 'px';
        });

        // Research settings panel toggle
        this.initResearchSettingsPanel();
        // Research depth button (Basic/Medium/Deep/Expert/Ultra)
        this.initResearchDepthButton();

        // Handle page visibility changes
        document.addEventListener('visibilitychange', () => {
            if (document.hidden && this.isProcessing) {
                console.log(`Page hidden during ${this.mode} processing - maintaining connection`);
            }
        });

        console.log(`✅ ChatController initialized - Mode: ${this.mode}`);
    }

    initResearchSettingsPanel() {
        const settingsPanel = document.getElementById('researchSettingsPanel');
        const closeBtn = document.getElementById('closeSettingsPanel');
        const depthSelect = document.getElementById('researchDepth');
        const translationToggle = document.getElementById('useTranslation');
        const targetLangSelect = document.getElementById('targetLanguage');

        if (!settingsPanel) return;

        const settingsBtn = document.getElementById('researchSettingsBtn'); // legacy/optional

        const openPanel = () => {
            settingsPanel.style.display = 'block';
            settingsBtn?.classList.add('active');
        };
        const closePanel = () => {
            settingsPanel.style.display = 'none';
            settingsBtn?.classList.remove('active');
        };

        this.openResearchSettingsPanel = openPanel;
        this.closeResearchSettingsPanel = closePanel;

        // Toggle panel visibility (if the legacy button exists)
        settingsBtn?.addEventListener('click', (e) => {
            e.stopPropagation();
            const isVisible = settingsPanel.style.display !== 'none';
            if (isVisible) closePanel();
            else openPanel();
        });

        // Close button
        closeBtn?.addEventListener('click', () => {
            closePanel();
        });

        // Close when clicking outside
        document.addEventListener('click', (e) => {
            const isVisible = settingsPanel.style.display !== 'none';
            if (!isVisible) return;

            const depthBtn = document.getElementById('researchDepthBtn');
            const depthMenu = document.getElementById('researchDepthMenu');

            const clickedInsidePanel = settingsPanel.contains(e.target);
            const clickedOnLegacyBtn = settingsBtn ? settingsBtn.contains(e.target) : false;
            const clickedOnDepthBtn = depthBtn ? depthBtn.contains(e.target) : false;
            const clickedInsideDepthMenu = depthMenu ? depthMenu.contains(e.target) : false;

            if (!clickedInsidePanel && !clickedOnLegacyBtn && !clickedOnDepthBtn && !clickedInsideDepthMenu) {
                closePanel();
            }
        });

        // Update summary when settings change
        const updateSummary = () => {
            const depth = depthSelect?.value || 'medium';
            const useTranslation = translationToggle?.checked ?? true;
            const targetLang = targetLangSelect?.value || 'en';

            const depthLabels = {
                basic: 'Basic',
                medium: 'Medium',
                deep: 'Deep',
                expert: 'Expert',
                ultra: 'Ultra',
                // legacy aliases
                quick: 'Basic',
                standard: 'Medium',
            };
            const langLabels = {
                en: 'English', es: 'Spanish', fr: 'French', de: 'German',
                zh: 'Chinese', ja: 'Japanese', ko: 'Korean', ar: 'Arabic',
                ru: 'Russian', pt: 'Portuguese'
            };

            const summary = document.getElementById('settingsSummary');
            if (summary) {
                const translationStatus = useTranslation
                    ? `Translation → ${langLabels[targetLang] || targetLang}`
                    : 'Translation OFF';
                summary.textContent = `${depthLabels[depth] || 'Medium'} depth, ${translationStatus}`;
            }

            // Show badge if non-default settings (default = medium)
            const badge = document.getElementById('settingsBadge');
            if (badge) {
                const isNonDefault = depth !== 'medium' || !useTranslation || targetLang !== 'en';
                badge.style.display = isNonDefault ? 'flex' : 'none';
            }

            // Also show a subtle dot badge on the depth button when non-default depth
            const depthBadge = document.getElementById('depthBadge');
            if (depthBadge) {
                depthBadge.style.display = depth !== 'medium' ? 'flex' : 'none';
            }
        };

        depthSelect?.addEventListener('change', updateSummary);
        translationToggle?.addEventListener('change', updateSummary);
        targetLangSelect?.addEventListener('change', updateSummary);

        // Initial summary update
        updateSummary();
        console.log('✅ Research settings panel initialized');
    }

    initResearchDepthButton() {
        const btn = document.getElementById('researchDepthBtn');
        const menu = document.getElementById('researchDepthMenu');
        const label = document.getElementById('researchDepthLabel');
        const depthSelect = document.getElementById('researchDepth');
        const openMoreSettingsBtn = document.getElementById('openResearchSettingsFromDepth');

        if (!btn || !menu || !depthSelect) return;

        // Canonical tier names are basic/medium/deep/expert/ultra.
        // Legacy aliases kept for backward compat (quick→basic, standard→medium).
        const depthToCount = {
            basic: '8',
            medium: '25',
            deep: '80',
            expert: '250',
            ultra: '1k',
            quick: '8',
            standard: '25',
        };
        const depthToLabel = {
            basic: 'Basic (8 sources · ~5 min)',
            medium: 'Medium (25 sources · ~20 min)',
            deep: 'Deep (80 sources · ~75 min)',
            expert: 'Expert (250 sources · ~4 hrs)',
            ultra: 'Ultra (up to 1000 sources · ~10 hrs)',
            quick: 'Basic (8 sources · ~5 min)',
            standard: 'Medium (25 sources · ~20 min)',
        };

        const setMenuVisible = (visible) => {
            menu.style.display = visible ? 'block' : 'none';
            btn.classList.toggle('active', visible);
            btn.setAttribute('aria-expanded', visible ? 'true' : 'false');
        };

        const updateUI = () => {
            const depth = depthSelect.value || 'medium';
            if (label) label.textContent = depthToCount[depth] || '25';

            // Update aria states for menuitemradio options
            menu.querySelectorAll('.depth-option[data-depth]').forEach((opt) => {
                const optDepth = opt.getAttribute('data-depth');
                opt.setAttribute('aria-checked', optDepth === depth ? 'true' : 'false');
            });

            btn.setAttribute('aria-label', `Research depth: ${depthToLabel[depth] || depthToLabel.medium}`);
        };

        // Toggle menu on button click
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            const isVisible = menu.style.display !== 'none';
            setMenuVisible(!isVisible);
        });

        // Option click handlers
        menu.querySelectorAll('.depth-option[data-depth]').forEach((opt) => {
            opt.addEventListener('click', (e) => {
                e.stopPropagation();
                const nextDepth = opt.getAttribute('data-depth') || 'medium';
                depthSelect.value = nextDepth;
                depthSelect.dispatchEvent(new Event('change', { bubbles: true }));
                updateUI();
                setMenuVisible(false);
                btn.focus();
            });
        });

        // "More settings" button opens the full settings panel
        openMoreSettingsBtn?.addEventListener('click', (e) => {
            e.stopPropagation();
            setMenuVisible(false);
            if (typeof this.openResearchSettingsPanel === 'function') {
                this.openResearchSettingsPanel();
            }
        });

        // Close when clicking outside
        document.addEventListener('click', (e) => {
            const isVisible = menu.style.display !== 'none';
            if (!isVisible) return;
            if (!menu.contains(e.target) && !btn.contains(e.target)) {
                setMenuVisible(false);
            }
        });

        // Escape key closes
        document.addEventListener('keydown', (e) => {
            if (e.key !== 'Escape') return;
            const isVisible = menu.style.display !== 'none';
            if (!isVisible) return;
            setMenuVisible(false);
            btn.focus();
        });

        // Keep in sync when the select changes elsewhere
        depthSelect.addEventListener('change', updateUI);

        updateUI();
        console.log('✅ Research depth button initialized');
    }

    setMode(newMode) {
        if (['research', 'thinking', 'coding'].includes(newMode)) {
            console.log(`🔄 ChatController mode changed: ${this.mode} → ${newMode}`);
            this.mode = newMode;
            this.updateAIMode();
        } else {
            console.warn(`⚠️ Invalid mode: ${newMode}`);
        }
    }

    setChatSessionId(sessionId) {
        this.chatSessionId = sessionId;
        // Phase D3 — when the user navigates into a chat session,
        // check whether it has an in-progress query and surface a
        // resume banner. Fire-and-forget; failure is non-fatal.
        if (sessionId) {
            this._checkAndResumeActiveQuery(sessionId).catch(() => {});
        }
    }

    // ── P0.2: Active research persistence ──────────────────────────────────
    //
    // We stash the in-progress research session_id in localStorage so that a
    // page reload (F5, browser crash, navigation) can re-attach to the live
    // run instead of showing a permanent "Waiting for research…" spinner.
    //
    // Key shape: { sessionId, mode, ts }   ts = Date.now() at start.
    // Entries older than 4 hours are dropped on resume — runs that long are
    // either done or genuinely abandoned.

    _persistActiveResearch(sessionId, mode = this.mode) {
        try {
            localStorage.setItem('amor.activeResearch', JSON.stringify({
                sessionId,
                mode,
                ts: Date.now(),
            }));
        } catch (_) { /* private mode / quota — non-fatal */ }
    }

    _clearActiveResearch() {
        try { localStorage.removeItem('amor.activeResearch'); } catch (_) {}
    }

    _readActiveResearch() {
        try {
            const raw = localStorage.getItem('amor.activeResearch');
            if (!raw) return null;
            const saved = JSON.parse(raw);
            if (!saved?.sessionId) return null;
            // 4h staleness window: anything older was definitely abandoned.
            if (Date.now() - (saved.ts || 0) > 4 * 3600 * 1000) {
                this._clearActiveResearch();
                return null;
            }
            return saved;
        } catch (_) { return null; }
    }

    /**
     * P0.2: Resume an in-flight research session after page reload.
     *
     * Called once from DOMContentLoaded. If localStorage has a recent
     * `amor.activeResearch` entry, fetch its /status:
     *   - completed → render the report (no re-run); clear localStorage.
     *   - failed   → leave a brief error trace (best effort); clear.
     *   - running  → mount the card, snapshot it, re-attach SSE.
     */
    async _resumeActiveResearchIfAny() {
        const saved = this._readActiveResearch();
        if (!saved) return;

        try {
            const resp = await this._authFetch(
                `/api/local-ai/research/${encodeURIComponent(saved.sessionId)}/status`
            );
            if (resp.status === 404) {
                // Session evicted (Redis flushed, etc.) — nothing to resume.
                this._clearActiveResearch();
                return;
            }
            if (!resp.ok) {
                console.warn('resume-research: /status returned', resp.status);
                return; // keep entry; user may retry
            }
            const status = await resp.json();

            if (status.status === 'completed') {
                // Render the persisted final result; no SSE needed.
                try { await this.displayResearchResults(status); } catch (e) {
                    console.warn('resume-research: displayResearchResults failed', e);
                }
                this._clearActiveResearch();
                return;
            }
            if (status.status === 'failed') {
                console.info('resume-research: prior run failed —', status.error);
                this._clearActiveResearch();
                return;
            }

            // Still running: mount the live card, hydrate from snapshot, re-attach.
            if (typeof ResearchView !== 'function') {
                console.warn('resume-research: ResearchView not loaded yet');
                return;
            }
            // Real query text from the running session, with a graceful
            // fallback that no longer shows the awkward "(restored)" tag.
            const restoredTopic = status.topic || status.query || 'Resumed research';
            const view = new ResearchView(restoredTopic, status.depth || 'medium');
            this._mountResearchCard(view);
            // Seed from snapshot before live events kick in.
            try { view.handleEvent({ type: 'snapshot', ...status }); } catch (_) {}

            // Re-attach the live stream. Same fallback to polling as the
            // primary flow.
            try {
                try {
                    await this._streamResearch(saved.sessionId, view);
                } catch (sseErr) {
                    console.warn('resume-research: SSE failed, polling…', sseErr);
                    await this._pollResearchInto(saved.sessionId, view);
                }
            } catch (err) {
                try { view.handleEvent({ type: 'error', message: err.message || 'Research failed' }); } catch (_) {}
            } finally {
                this._clearActiveResearch();
            }
        } catch (e) {
            console.warn('resume-research: unexpected error', e);
        }
    }

    // Auth-aware fetch — routes through window.amorAuth.fetch so the JWT is
    // included on every request, with automatic refresh+retry on 401.
    // Falls back to raw fetch only if the auth layer isn't mounted yet (e.g.
    // during the very first bootstrap tick).
    _authFetch(path, init = {}) {
        if (window.amorAuth && typeof window.amorAuth.fetch === 'function') {
            return window.amorAuth.fetch(path, init);
        }
        const headers = Object.assign(
            {},
            init.headers || {},
            window.getChatHeaders ? window.getChatHeaders() : {}
        );
        return fetch(path, { credentials: 'include', ...init, headers });
    }

    async persistChatMessage(msg) {
        if (!this.chatSessionId) return;
        try {
            const response = await this._authFetch(`/api/sessions/${encodeURIComponent(this.chatSessionId)}/messages`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    role: msg.role,
                    content: msg.content,
                    format: msg.format || 'text',
                    aiType: msg.aiType || null,
                    extras: msg.extras || {},
                    // Phase C — defense-in-depth dedupe. The backend AI
                    // handler also writes this same message with the
                    // same key; the unique sparse index on
                    // chat_messages.idempotency_key collapses to one row.
                    idempotency_key: msg.idempotency_key || null,
                })
            });
            if (!response.ok) {
                console.warn('Failed to persist message:', response.status, response.statusText);
            }
        } catch (e) {
            console.warn('Failed to persist message:', e);
        }
    }

    // ─── Phase C/D/E/F shared helpers ──────────────────────────────────

    /**
     * Generate a fresh UUID4 string. Uses crypto.randomUUID() when
     * available (modern browsers + secure contexts) and falls back
     * to a short Math.random hex otherwise so older / non-HTTPS
     * setups still work.
     */
    _newUuid() {
        try {
            if (window.crypto?.randomUUID) return window.crypto.randomUUID();
        } catch (_) {}
        // Fallback — not RFC4122 but unique-enough for idempotency keys.
        return 'fb-' + Date.now().toString(36) + '-' +
               Math.random().toString(36).slice(2, 10);
    }

    /**
     * Phase F — structured error classification.
     * Maps a raw exception to {type, userMsg, recoverable} so the
     * caller can render a meaningful bubble (and queue an automatic
     * retry on rate-limit).
     */
    _classifyError(err) {
        const msg = ((err?.message) || String(err || '')).toLowerCase();
        if (err?.name === 'AbortError')
            return { type: 'cancelled',   userMsg: 'Query cancelled.',                                       recoverable: false };
        if (msg.includes(' 401') || msg.includes('unauthorized'))
            return { type: 'auth',        userMsg: 'Session expired. Please log in again.',                  recoverable: false };
        if (msg.includes(' 503') || msg.includes('unavailable'))
            return { type: 'unavailable', userMsg: 'AI service is temporarily unavailable. Try again shortly.', recoverable: true };
        if (msg.includes(' 429') || msg.includes('rate limit'))
            return { type: 'rate_limit',  userMsg: 'Rate limit reached. Auto-retrying in 30s…',              recoverable: true };
        if (msg.includes('network') || msg.includes('failed to fetch'))
            return { type: 'network',     userMsg: 'Network error. Check your connection.',                   recoverable: true };
        if (msg.includes('timeout') || msg.includes('timed out'))
            return { type: 'timeout',     userMsg: 'The query timed out. Try a shorter prompt or lower effort.', recoverable: false };
        return { type: 'unknown',         userMsg: `Error: ${err?.message || 'unknown'}`,                    recoverable: false };
    }

    /**
     * Phase B/C — create a query record on the server. Returns the
     * record id (or null on failure — caller proceeds without
     * persistence linkage). Idempotency key prevents duplicate
     * records on retries.
     */
    async _createQueryRecord({ prompt, mode, provider, effort, idempotencyKey }) {
        if (!this.chatSessionId) return null;
        try {
            const resp = await this._authFetch('/api/query-records', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    chat_session_id: this.chatSessionId,
                    mode,
                    query_text: prompt.slice(0, 8000),
                    provider,
                    effort: effort || null,
                    idempotency_key: idempotencyKey,
                }),
            });
            if (!resp.ok) {
                console.warn('Query record create failed:', resp.status);
                return null;
            }
            const data = await resp.json();
            return data.id;
        } catch (e) {
            console.warn('Query record create failed:', e);
            return null;
        }
    }

    /**
     * Predict the title the server will pick. Mirrors the Python helper
     * `_generate_title_from_query` so the optimistic update lines up with
     * what eventually persists in Mongo.
     */
    _predictTitle(query, maxChars = 60) {
        let text = String(query || '').replace(/<[^>]+>/g, '');
        text = text.replace(/[*_`#~>\[\]]+/g, '').replace(/\s+/g, ' ').trim();
        if (!text) return 'New Chat';
        if (text.length <= maxChars) return text[0].toUpperCase() + text.slice(1);
        let cut = text.slice(0, maxChars);
        const lastSpace = cut.lastIndexOf(' ');
        if (lastSpace > maxChars * 0.66) cut = cut.slice(0, lastSpace);
        return (cut[0].toUpperCase() + cut.slice(1)).replace(/[.,;:]+$/, '') + '…';
    }

    /**
     * Phase B + UX polish — auto-title with OPTIMISTIC client-side update.
     *
     * We immediately render a predicted title in the sidebar (matching the
     * server's algorithm) so the user sees their query reflected the moment
     * they hit Send — no waiting on a round-trip. The server response then
     * confirms / corrects the prediction; if the server skipped the update
     * (user already renamed) the cached state still wins.
     */
    _autoTitleSession(sessionId, prompt) {
        if (!sessionId || !prompt) return;
        const predicted = this._predictTitle(prompt);

        // Optimistic local update — patch the in-memory session list so the
        // next render shows the new title. Falls back to a full re-render
        // when the in-memory index isn't there yet.
        try {
            const idx = window.appState?._historyIndex;
            const session = idx?.get?.(sessionId);
            if (session && (!session.title ||
                session.title === 'Untitled Chat' ||
                session.title === 'New Chat')) {
                session.title = predicted;
            }
            if (typeof window.renderChatHistory === 'function') {
                window.renderChatHistory().catch(() => {});
            }
            // Topbar title (current chat name shown above the chat area).
            const topTitle = document.getElementById('chatTitle')
                || document.querySelector('[data-chat-title]');
            if (topTitle) topTitle.textContent = predicted;
            // Browser tab title — secondary signal that something useful
            // is in flight.
            try { document.title = `${predicted} — Amor`; } catch (_) {}
        } catch (_) {}

        this._authFetch(`/api/sessions/${encodeURIComponent(sessionId)}/auto-title`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ query: prompt.slice(0, 4000) }),
        }).then(async (resp) => {
            if (!resp.ok) return;
            const data = await resp.json();
            // Reconcile with the server's chosen title. If the server
            // skipped (user-renamed), our optimistic update is wrong —
            // the next renderChatHistory() will pull the canonical value.
            if (typeof window.renderChatHistory === 'function') {
                try { window.renderChatHistory(); } catch (_) {}
            }
            if (data.title) {
                const topTitle = document.getElementById('chatTitle')
                    || document.querySelector('[data-chat-title]');
                if (topTitle) topTitle.textContent = data.title;
                try { document.title = `${data.title} — Amor`; } catch (_) {}
            }
        }).catch(() => { /* best-effort */ });
    }

    /**
     * Phase D3 — on session reload, surface a banner if the session
     * has an in-progress query, with options to resume watching SSE
     * or cancel outright.
     */
    async _checkAndResumeActiveQuery(chatSessionId) {
        if (!chatSessionId) return;
        try {
            const resp = await this._authFetch(
                `/api/sessions/${encodeURIComponent(chatSessionId)}/active-query`
            );
            if (!resp.ok) return;
            const data = await resp.json();
            if (!data.active || !data.record) return;
            this._renderResumeBanner(data.record);
        } catch (e) {
            console.warn('active-query check failed:', e);
        }
    }

    /**
     * Build the resume banner DOM and insert it at the top of the
     * messages area. Wires Resume + Cancel buttons.
     */
    _renderResumeBanner(record) {
        if (!this.messagesArea) return;
        // De-dupe: if a banner for this record already exists, refresh
        // instead of stacking.
        const existing = this.messagesArea.querySelector(
            `.query-resume-banner[data-record-id="${CSS.escape(record.id)}"]`
        );
        if (existing) { existing.remove(); }

        const elapsedMs = Date.now() - (new Date(record.started_at).getTime() || Date.now());
        const elapsed = this._formatDuration(Math.max(0, elapsedMs / 1000));
        const banner = document.createElement('div');
        banner.className = 'query-resume-banner';
        banner.dataset.recordId = record.id;
        const safeQuery = (record.query_text || '').slice(0, 80);
        banner.innerHTML = `
            <div class="banner-text">
              <strong>Query in progress</strong>
              <div class="query-resume-text"></div>
              <div class="query-resume-meta">
                <span>${this.escapeHtml(record.current_phase || 'starting')}</span>
                ·
                <span class="query-resume-pct">${Math.max(0, Math.min(100, Math.round(record.progress || 0)))}%</span>
                ·
                <span>${this.escapeHtml(elapsed)} elapsed</span>
              </div>
            </div>
            <button class="query-resume-btn" type="button">Resume watching</button>
            <button class="query-cancel-btn" type="button">Cancel</button>
        `;
        // Use textContent for the query text so we don't HTML-inject.
        banner.querySelector('.query-resume-text').textContent = safeQuery;
        this.messagesArea.prepend(banner);

        banner.querySelector('.query-resume-btn').addEventListener('click', async () => {
            banner.remove();
            await this._resumeFromRecord(record);
        });
        banner.querySelector('.query-cancel-btn').addEventListener('click', async () => {
            try {
                await this._authFetch(
                    `/api/query-records/${encodeURIComponent(record.id)}/cancel`,
                    { method: 'POST', headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify({ reason: 'Cancelled by user from resume banner' }) }
                );
            } catch (_) {}
            banner.remove();
        });
    }

    _formatDuration(seconds) {
        const s = Math.max(0, Math.floor(seconds));
        const h = Math.floor(s / 3600);
        const m = Math.floor((s % 3600) / 60);
        const sec = s % 60;
        if (h > 0) return `${h}h ${m}m`;
        if (m > 0) return `${m}m ${sec}s`;
        return `${sec}s`;
    }

    /**
     * Phase D3 — resume watching an in-progress query. Mounts the
     * appropriate view + reconnects SSE.
     */
    async _resumeFromRecord(record) {
        // Wire per-query state so the Stop button / cancel routes can
        // halt the resumed pipeline. Cleared in finally below.
        this._currentQueryRecordId = record.id;
        this._currentAbortController = new AbortController();
        if (record.mode === 'thinking') {
            this._currentThinkingBackendId = record.thinking_session_id || null;
        } else if (record.mode === 'research') {
            this._currentResearchBackendId = record.research_session_id || null;
        }
        try {
            if (record.mode === 'thinking' && record.thinking_session_id) {
                if (typeof ThinkingView !== 'function') return;
                const view = new ThinkingView({
                    prompt: record.query_text,
                    effort: record.effort || 'medium',
                    provider: record.provider || 'local',
                });
                this._mountThinkingCard(view);
                view.loadFromSnapshot({
                    phases: record.phases || [],
                    current_phase: record.current_phase,
                    progress: record.progress,
                    deliverable_markdown: record.result_markdown,
                });
                this.isProcessing = true;
                if (this.sendButton) this.sendButton.disabled = true;
                if (this.stopButton) this.stopButton.classList.add('is-active');
                try {
                    await this._streamThinking(record.thinking_session_id, view);
                } finally {
                    this.isProcessing = false;
                    if (this.sendButton) this.sendButton.disabled = false;
                    if (this.stopButton) this.stopButton.classList.remove('is-active');
                }
            } else if (record.mode === 'research' && record.research_session_id) {
                if (typeof ResearchView !== 'function') return;
                const view = new ResearchView(record.query_text, record.effort || 'medium');
                this._mountResearchCard(view);
                try { view.handleEvent({ type: 'snapshot',
                    phases: record.phases || [],
                    current_phase: record.current_phase,
                    progress: record.progress,
                    report_markdown: record.result_markdown,
                    citations: record.sources || [],
                }); } catch (_) {}
                this.isProcessing = true;
                if (this.sendButton) this.sendButton.disabled = true;
                if (this.stopButton) this.stopButton.classList.add('is-active');
                try {
                    try {
                        await this._streamResearch(record.research_session_id, view);
                    } catch (_) {
                        await this._pollResearchInto(record.research_session_id, view);
                    }
                } finally {
                    this.isProcessing = false;
                    if (this.sendButton) this.sendButton.disabled = false;
                    if (this.stopButton) this.stopButton.classList.remove('is-active');
                }
            }
        } catch (e) {
            console.warn('resume-from-record failed:', e);
        } finally {
            this._currentAbortController = null;
            this._currentQueryRecordId = null;
            this._currentThinkingBackendId = null;
            this._currentResearchBackendId = null;
        }
    }

    getEndpointForMode(useClaudeAPI) {
        const endpoints = API_ENDPOINTS[this.mode];
        if (!endpoints) {
            console.error(`❌ No endpoints defined for mode: ${this.mode}`);
            return null;
        }
        return useClaudeAPI ? endpoints.claude : endpoints.local;
    }

    getModeIcon() {
        const icons = {
            research: '🔍',
            thinking: '🧠',
            coding: '💻'
        };
        return icons[this.mode] || '🤖';
    }

    getModeName() {
        const names = {
            research: 'Research Assistant',
            thinking: 'Thinking Assistant',
            coding: 'Coding Assistant'
        };
        return names[this.mode] || 'AI Assistant';
    }

    getProviderName() {
        return this.useClaudeAPI?.checked ? 'Claude API' : 'Local AI';
    }

    formatErrorMessage(error, providerOverride = null) {
        const provider = providerOverride || this.getProviderName();
        const raw = (error && error.message) ? String(error.message) : String(error || 'Unknown error');
        const lower = raw.toLowerCase();

        // Claude-specific failures
        if (provider === 'Claude API') {
            if (lower.includes('anthropic_api_key') || lower.includes('claude api not configured')) {
                return 'Claude API is not configured. Please set the ANTHROPIC_API_KEY environment variable on the server or turn off "Use Claude API" in Settings.';
            }
            if (lower.includes('503') && lower.includes('service unavailable')) {
                return 'Claude API is temporarily unavailable. Please check your internet connection and Anthropic account status, or try again later.';
            }
        }

        // Local AI / Ollama-specific failures
        if (provider === 'Local AI') {
            if (lower.includes('ollama service not available')) {
                return 'Local AI is unavailable: please ensure the amor-ollama container is running and healthy.';
            }
            if (lower.includes("ollama model") && lower.includes("not installed")) {
                return 'Local AI model is not installed. In a terminal run: docker exec amor-ollama ollama pull qwen2.5:7b (or your configured model), then try again.';
            }
            if (lower.includes('failed to start research')) {
                return `Local AI research could not be started. Details from server: ${raw}`;
            }
            if (lower.includes('research timeout')) {
                return 'Local AI research timed out. Try again with Basic depth or a narrower topic.';
            }
        }

        // Generic fallback
        return `${provider} error: ${raw}`;
    }

    updateCharacterCount() {
        const count = this.chatInput?.value.length || 0;
        if (this.characterCount) {
            this.characterCount.textContent = `${count} characters`;
        }
    }

    updateAIMode() {
        const isClaudeAPI = this.useClaudeAPI?.checked || false;
        if (this.aiIndicator) {
            this.aiIndicator.textContent = isClaudeAPI ? 'Claude API' : 'Local AI';
        }
    }

    async sendMessage() {
        const message = this.chatInput?.value.trim();
        if (!message || this.isProcessing) return;

        // Lock immediately — before any await — so rapid double-clicks cannot
        // slip past the guard during persistChatMessage / addTypingIndicator.
        this.isProcessing = true;
        if (this.sendButton) this.sendButton.disabled = true;
        // Phase C2/E — show the Stop button so the user can cancel
        // mid-flight. CSS handles the slide-in transition.
        if (this.stopButton) this.stopButton.classList.add('is-active');

        // Lazy session creation: page load and mode-card clicks no longer
        // pre-create a chat session (that flooded history with empty
        // "Untitled Chat" entries). Create one now — first real message —
        // so persistChatMessage has somewhere to write.
        if (!this.chatSessionId &&
            typeof window.ensureCurrentServerSession === 'function') {
            try { await window.ensureCurrentServerSession(); }
            catch (e) { console.warn('lazy session-create failed:', e); }
        }

        // Phase C+E — generate per-query idempotency keys + create the
        // server-side query record so the AI handler can stamp it with
        // status/progress and the resume banner has something to show
        // on page reload. All of these are stored on `this` so the
        // process methods + cancel button can read them.
        const userMsgKey = this._newUuid();
        const assistantMsgKey = this._newUuid();
        const queryRecordIdempotencyKey = this._newUuid();
        const queryProvider = this.useClaudeAPI?.checked ? 'claude' : 'local-ai';
        const queryEffort = this.mode === 'thinking'
            ? (document.getElementById('thinkingEffortHidden')?.value || 'medium')
            : (document.getElementById('researchDepth')?.value || null);
        this._currentQueryRecordId = await this._createQueryRecord({
            prompt: message,
            mode: this.mode,
            provider: queryProvider,
            effort: queryEffort,
            idempotencyKey: queryRecordIdempotencyKey,
        });
        this._currentUserMsgKey = userMsgKey;
        this._currentAssistantMsgKey = assistantMsgKey;
        this._currentAbortController = new AbortController();

        // Phase B3 — fire-and-forget auto-title (server skips if user
        // already renamed). Sidebar updates optimistically when this
        // returns.
        if (this.chatSessionId) {
            this._autoTitleSession(this.chatSessionId, message);
        }

        // Add user message (with idempotency_key so the persist can dedupe)
        this.addMessage('user', message);
        const userMsg = {
            role: 'user', content: message, format: 'text',
            idempotency_key: userMsgKey,
        };
        this.messageHistory.push(userMsg);
        await this.persistChatMessage(userMsg);

        this.chatInput.value = '';
        this.chatInput.style.height = 'auto';
        this.updateCharacterCount();

        // Remove welcome message if exists
        const welcomeContainer = document.getElementById('welcomeContainer');
        if (welcomeContainer) {
            welcomeContainer.style.display = 'none';
        }

        // Show typing indicator
        const typingId = this.addTypingIndicator();

        try {
            const useClaudeAPI = this.useClaudeAPI?.checked || false;

            // Thinking mode uses its own multi-phase pipeline regardless of
            // provider — the backend picks local vs claude from the body.
            if (this.mode === 'thinking') {
                await this.thinkingWithLocalAI(message, typingId, useClaudeAPI ? 'claude' : 'local');
            } else if (useClaudeAPI) {
                await this.processWithClaude(message, typingId);
            } else {
                await this.processWithLocalAI(message, typingId);
            }
        } catch (error) {
            console.error(`${this.mode} processing error:`, error);
            this.removeTypingIndicator(typingId);
            // Phase F — structured error classification. Prefer the
            // user-facing message from the classifier; fall back to
            // existing formatErrorMessage if needed.
            const cls = this._classifyError(error);
            const friendly = cls.userMsg || this.formatErrorMessage(error);
            this.addMessage('assistant', friendly, 'error');
            const errMsg = {
                role: 'assistant',
                content: friendly,
                aiType: 'error',
                format: 'text',
                extras: { error: error.message || String(error), error_type: cls.type },
            };
            this.messageHistory.push(errMsg);
            try { await this.persistChatMessage(errMsg); } catch (_) {}
        } finally {
            this.isProcessing = false;
            if (this.sendButton) this.sendButton.disabled = false;
            // Phase C2/E — hide the Stop button when the query terminates
            // (success, failure, or cancel).
            if (this.stopButton) this.stopButton.classList.remove('is-active');
            this._currentAbortController = null;
            this._currentQueryRecordId = null;
            this._currentUserMsgKey = null;
            this._currentAssistantMsgKey = null;
            this._currentThinkingBackendId = null;
            this._currentResearchBackendId = null;
        }
    }

    /**
     * Phase C2 — cancel the in-flight query (called by the Stop button).
     * Aborts the client-side fetch AND tells the backend to halt the
     * matching pipeline. Both signals are useful: AbortController
     * frees the network slot immediately; the backend cancel saves
     * the LLM compute budget.
     */
    async cancelCurrentQuery() {
        // Client-side cancel — fires immediately even if the backend
        // is unreachable.
        try { this._currentAbortController?.abort(); } catch (_) {}

        const recordId = this._currentQueryRecordId;
        const mode = this.mode;
        const useClaudeAPI = this.useClaudeAPI?.checked || false;
        const calls = [];

        // Mode-specific pipeline cancel — halts the actual worker on the
        // backend so we don't burn LLM compute after the user clicked Stop.
        if (mode === 'thinking' && this._currentThinkingBackendId) {
            calls.push(this._authFetch(
                `/api/thinking/${encodeURIComponent(this._currentThinkingBackendId)}/cancel`,
                { method: 'POST' }
            ));
        } else if (mode === 'research' && !useClaudeAPI && this._currentResearchBackendId) {
            calls.push(this._authFetch(
                `/api/local-ai/research/${encodeURIComponent(this._currentResearchBackendId)}/cancel`,
                { method: 'POST' }
            ));
        } else if ((mode === 'research' || mode === 'coding') && useClaudeAPI && recordId) {
            // Claude API tasks are tracked by query_record_id in
            // chat_research_routes._ACTIVE_TASKS.
            calls.push(this._authFetch(
                `/api/chat/cancel/${encodeURIComponent(recordId)}`,
                { method: 'POST' }
            ));
        }

        // Always flip the query_record terminal so other replicas + the
        // resume banner observe the cancelled state regardless of which
        // pipeline owned the work.
        if (recordId) {
            calls.push(this._authFetch(
                `/api/query-records/${encodeURIComponent(recordId)}/cancel`,
                { method: 'POST', headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({ reason: 'Cancelled by user.' }) }
            ));
        }

        try {
            // Run cancels in parallel — order doesn't matter, each is
            // best-effort and the worker also polls the query_record
            // status on its next phase boundary.
            await Promise.allSettled(calls);
        } catch (e) {
            console.warn('cancelCurrentQuery server-side failed:', e);
        }
    }

    async processWithClaude(message, typingId) {
        try {
            const endpoint = this.getEndpointForMode(true);
            if (!endpoint) {
                throw new Error(`No Claude API endpoint for ${this.mode} mode`);
            }

            // Build request body with mode-specific settings
            const requestBody = {
                prompt: message,
                mode: this.mode,
                history: this.messageHistory,
                // Phase C — server-side persistence + cancellation linkage
                chat_session_id: this.chatSessionId || null,
                query_record_id: this._currentQueryRecordId || null,
                user_message_idempotency_key: this._currentUserMsgKey || null,
                assistant_message_idempotency_key: this._currentAssistantMsgKey || null,
            };

            // Add research settings when in research mode
            if (this.mode === 'research') {
                const depthSelect = document.getElementById('researchDepth');
                const translationToggle = document.getElementById('useTranslation');
                const targetLangSelect = document.getElementById('targetLanguage');

                requestBody.depth = depthSelect?.value || 'medium';
                requestBody.use_translation = translationToggle?.checked ?? true;
                requestBody.target_language = targetLangSelect?.value || 'en';
                requestBody.use_research = true;
            }

            const response = await this._authFetch(endpoint, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify(requestBody),
                // Phase C2 — wire AbortController so the Stop button can
                // cancel the in-flight HTTP request immediately.
                signal: this._currentAbortController?.signal,
            });

            if (!response.ok) {
                let detail = '';
                try {
                    const errBody = await response.json();
                    detail = errBody.detail || '';
                } catch (_) {
                    // ignore JSON parse errors
                }
                const statusInfo = `${response.status} ${response.statusText}`.trim();
                const extra = detail ? ` - ${detail}` : '';
                throw new Error(`Claude API error (${statusInfo})${extra}`);
            }

            const data = await response.json();
            this.removeTypingIndicator(typingId);

            const content = data.response || data.content || data.text;
            this.addMessage('assistant', content, 'claude', {
                sources: data.sources,
                metadata: data.metadata
            });

            const assistantMsg = {
                role: 'assistant',
                content,
                aiType: 'claude',
                format: 'text',
                extras: {
                    sources: data.sources,
                    metadata: data.metadata
                }
            };
            this.messageHistory.push(assistantMsg);
            await this.persistChatMessage(assistantMsg);

        } catch (error) {
            throw new Error(`Claude API failed: ${error.message}`);
        }
    }

    async processWithLocalAI(message, typingId) {
        // Note: isProcessing / sendButton locking is handled in sendMessage() so
        // it covers ALL code paths (Claude, local, thinking) uniformly and
        // activates *before* the first await in sendMessage.
        const endpoint = this.getEndpointForMode(false);
        if (!endpoint) {
            throw new Error(`No Local AI endpoint for ${this.mode} mode`);
        }

        // For research mode, use the existing workflow with progress modal
        if (this.mode === 'research') {
            await this.researchWithLocalAI(message, typingId);
        } else if (this.mode === 'thinking') {
            // Human-in-the-loop multi-phase reasoning (v2)
            await this.thinkingWithLocalAI(message, typingId, 'local');
        } else {
            // Coding mode still uses simple request-response for now.
            await this.simpleLocalAIRequest(endpoint, message, typingId);
        }
    }

    async researchWithLocalAI(message, typingId) {
        // Get research settings from UI
        const depthSelect = document.getElementById('researchDepth');
        const translationToggle = document.getElementById('useTranslation');
        const targetLangSelect = document.getElementById('targetLanguage');

        const depth = depthSelect?.value || 'medium';
        const useTranslation = translationToggle?.checked ?? true;
        const targetLanguage = targetLangSelect?.value || 'en';

        // Start research
        const startResponse = await this._authFetch(`${API_ENDPOINTS.research.local}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                topic: message,
                depth,
                use_translation: useTranslation,
                target_language: targetLanguage,
                save_to_knowledge: true,
                // Phase C — server-side persistence + cancellation linkage
                chat_session_id: this.chatSessionId || null,
                query_record_id: this._currentQueryRecordId || null,
                user_message_idempotency_key: this._currentUserMsgKey || null,
                assistant_message_idempotency_key: this._currentAssistantMsgKey || null,
            }),
            signal: this._currentAbortController?.signal,
        });

        if (!startResponse.ok) {
            let detail = '';
            try { detail = (await startResponse.json()).detail || ''; } catch (_) {}
            const extra = detail ? ` - ${detail}` : '';
            throw new Error(`Failed to start research (${startResponse.status} ${startResponse.statusText})${extra}`);
        }

        const { session_id } = await startResponse.json();
        this.currentSessionId = session_id;
        // Phase C2 — let the Stop button reach the mode-specific cancel
        // endpoint with the right backend session id.
        this._currentResearchBackendId = session_id;

        // P0.2: stash the active session_id so a page reload can re-attach
        // and avoid the "stuck on spinner" symptom.
        this._persistActiveResearch(session_id);

        // Remove typing indicator — the research card replaces it
        this.removeTypingIndicator(typingId);

        // Mount a live research card into the messages area
        if (typeof ResearchView !== 'function') {
            throw new Error('ResearchView component is not loaded');
        }
        const view = new ResearchView(message, depth);
        this._mountResearchCard(view);

        // Stream events; fall back to polling if SSE is unavailable. Whatever
        // happens — success, partial failure, full failure — we always persist
        // the card's current snapshot so chat history keeps it and we always
        // leave the card in a terminal state (never a permanent spinner).
        let runError = null;
        try {
            try {
                await this._streamResearch(session_id, view);
            } catch (sseErr) {
                console.warn('SSE stream failed, falling back to polling:', sseErr);
                await this._pollResearchInto(session_id, view);
            }
        } catch (err) {
            runError = err;
            // Tell the view so the card stops spinning and shows the error.
            try { view.handleEvent({ type: 'error', message: err.message || 'Research failed' }); } catch (_) {}
        } finally {
            // P0.2: regardless of outcome, this run is no longer "active" —
            // clear the localStorage marker so reloads don't try to resume
            // a session that's already terminal.
            this._clearActiveResearch();
        }

        // Persist final snapshot (success OR partial/error) so chat history can restore it.
        const snap = view.toSnapshot();
        const assistantMsg = {
            role: 'assistant',
            content: runError ? `Research failed: ${runError.message}` : '',
            aiType: 'local-ai-research',
            format: 'research',
            extras: { research: snap, error: runError ? runError.message : undefined },
        };
        this.messageHistory.push(assistantMsg);
        try { await this.persistChatMessage(assistantMsg); } catch (persistErr) {
            console.warn('Failed to persist research snapshot:', persistErr);
        }

        if (runError) throw runError;
    }

    _mountResearchCard(view) {
        const wrap = document.createElement('div');
        wrap.className = 'message assistant local-ai-research';
        const time = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        wrap.innerHTML = `
            <div class="message-bubble research-bubble">
                <div class="message-header">
                    <div class="message-avatar">🔍</div>
                    <span class="message-name">Research</span>
                    <span class="message-time">${time}</span>
                </div>
                <div class="message-content research-content"></div>
            </div>
        `;
        wrap.querySelector('.research-content').appendChild(view.getElement());
        this.messagesArea?.appendChild(wrap);
        this.scrollToBottom();
    }

    // ────────────────────────────────────────────────────────── Thinking Mode
    //
    // Flow:
    //   1. POST /api/thinking/analyze  → may return clarifying questions
    //   2. Mount a ThinkingView card:
    //        - if questions: show the form, wait for user answers or "Skip"
    //        - else: skip straight to step 3
    //   3. POST /api/thinking/think    → returns session_id
    //   4. Stream SSE → hand events to the ThinkingView
    //   5. Persist the final snapshot into chat history

    _mountThinkingCard(view) {
        const wrap = document.createElement('div');
        wrap.className = 'message assistant local-ai-thinking';
        const time = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        wrap.innerHTML = `
            <div class="message-bubble research-bubble">
                <div class="message-header">
                    <div class="message-avatar">🧠</div>
                    <span class="message-name">Thinking</span>
                    <span class="message-time">${time}</span>
                </div>
                <div class="message-content research-content"></div>
            </div>
        `;
        wrap.querySelector('.research-content').appendChild(view.getElement());
        this.messagesArea?.appendChild(wrap);
        this.scrollToBottom();
    }

    async thinkingWithLocalAI(message, typingId, provider = 'local') {
        if (typeof ThinkingView !== 'function') {
            throw new Error('ThinkingView component is not loaded');
        }

        // Effort tier piggy-backs on the research depth selector so users
        // don't need yet-another control. If the selector is absent or set
        // to "basic/medium/deep/expert/ultra", we map through.
        const depthSelect = document.getElementById('researchDepth');
        const effort = depthSelect?.value || 'medium';

        const view = new ThinkingView({ prompt: message, effort, provider });

        // Swap the "typing" dots for the live thinking card early so the
        // user sees immediate feedback while /analyze runs.
        this.removeTypingIndicator(typingId);
        this._mountThinkingCard(view);

        // 1. Analyze
        let analysis;
        try {
            const res = await this._authFetch('/api/thinking/analyze', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ prompt: message, deliverable: 'auto' }),
            });
            if (!res.ok) {
                let detail = '';
                try { detail = (await res.json()).detail || ''; } catch (_) {}
                throw new Error(`Analyze failed (${res.status})${detail ? ' - ' + detail : ''}`);
            }
            analysis = await res.json();
        } catch (err) {
            // Fall back to thinking directly with no clarifications.
            console.warn('Thinking analyze failed, proceeding directly:', err);
            analysis = {
                needs_clarification: false,
                complexity: 'moderate',
                rationale: 'Analyzer unavailable — going straight to reasoning.',
                detected_deliverable: 'explanation',
                questions: [],
            };
        }

        // 2. Gather clarifications (possibly via user input)
        const clarifications = await this._askClarifications(view, analysis);

        // 3. Start the pipeline
        let session;
        try {
            const res = await this._authFetch('/api/thinking/think', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    prompt: message,
                    clarifications,
                    detected_deliverable: analysis.detected_deliverable || 'auto',
                    provider,
                    effort,
                    // Phase C — server-side persistence + cancellation linkage
                    chat_session_id: this.chatSessionId || null,
                    query_record_id: this._currentQueryRecordId || null,
                    user_message_idempotency_key: this._currentUserMsgKey || null,
                    assistant_message_idempotency_key: this._currentAssistantMsgKey || null,
                }),
                signal: this._currentAbortController?.signal,
            });
            if (!res.ok) {
                let detail = '';
                try { detail = (await res.json()).detail || ''; } catch (_) {}
                throw new Error(`Failed to start thinking (${res.status} ${res.statusText})${detail ? ' - ' + detail : ''}`);
            }
            session = await res.json();
        } catch (err) {
            view.handleEvent({ type: 'error', message: err.message });
            throw err;
        }

        // Phase C2 — let the Stop button reach /api/thinking/{sid}/cancel.
        this._currentThinkingBackendId = session.session_id;

        view.showTimeline({ session_id: session.session_id, phases: [] });

        // 4. Stream events — always persist whatever state we reached and always
        // leave the card in a terminal (completed/failed) state.
        let runError = null;
        try {
            try {
                await this._streamThinking(session.session_id, view);
            } catch (sseErr) {
                console.warn('Thinking SSE failed, polling fallback:', sseErr);
                await this._pollThinking(session.session_id, view);
            }
        } catch (err) {
            runError = err;
            try { view.handleEvent({ type: 'error', message: err.message || 'Thinking failed' }); } catch (_) {}
        }

        // 5. Persist final snapshot so history re-mounts it later.
        const snap = view.toSnapshot();
        const assistantMsg = {
            role: 'assistant',
            content: runError ? `Thinking failed: ${runError.message}` : '',
            aiType: 'local-ai-thinking',
            format: 'thinking',
            extras: { thinking: snap, error: runError ? runError.message : undefined },
        };
        this.messageHistory.push(assistantMsg);
        try { await this.persistChatMessage(assistantMsg); } catch (persistErr) {
            console.warn('Failed to persist thinking snapshot:', persistErr);
        }

        if (runError) throw runError;
    }

    _askClarifications(view, analysis) {
        return new Promise((resolve) => {
            if (!analysis?.needs_clarification || !Array.isArray(analysis.questions) || !analysis.questions.length) {
                resolve({});
                return;
            }
            view.showQuestions(analysis);
            view.onSubmitAnswers = (answers) => {
                view.showTimeline({ phases: [] });
                resolve(answers || {});
            };
            view.onProceedWithoutAnswers = () => {
                view.showTimeline({ phases: [] });
                resolve({});
            };
        });
    }

    _streamThinking(sessionId, view) {
        // P0.3: same self-healing SSE wrapper as _streamResearch.
        return this._sseLoop({
            url: (token) => `/api/thinking/${sessionId}/events${
                token ? `?access_token=${encodeURIComponent(token)}` : ''
            }`,
            view,
            failureMessage: 'Thinking failed',
        });
    }

    /**
     * P0.3: Self-healing SSE loop.
     *
     * EventSource doesn't support custom headers, so we encode the JWT in
     * the query string. The server validates it once at connection open;
     * after that the token is irrelevant — but if the connection drops we
     * need a *fresh* token to reopen, otherwise we get caught in a loop
     * of 401s after the 15-minute access-token expiry.
     *
     * Strategy:
     *   1. Open EventSource with current token.
     *   2. On message → forward to view; on terminal events finish().
     *   3. On error (and not yet completed):
     *        a. close ES
     *        b. attempt window.amorAuth.refresh()
     *        c. wait `min(reconnects, 5)` seconds (linear backoff)
     *        d. reopen with the fresh token
     *      Cap at 5 retries to avoid hammering a dead server.
     *   4. amor:auth-changed listener → proactively close+reopen so we
     *      don't race a near-expired token against an in-flight read.
     *
     * Returns a Promise that resolves on `done` or rejects on terminal
     * error (5 reconnect failures, or `error` event with a message).
     */
    _sseLoop({ url, view, failureMessage = 'Stream failed' }) {
        return new Promise((resolve, reject) => {
            let es = null;
            let completed = false;
            let reconnects = 0;
            const MAX_RECONNECTS = 5;

            const finish = (err) => {
                if (completed) return;
                completed = true;
                document.removeEventListener('amor:auth-changed', onAuthChanged);
                try { es?.close(); } catch (_) {}
                if (err) reject(err); else resolve();
            };

            const open = () => {
                if (completed) return;
                const token = window.amorAuth?.accessToken || '';
                try {
                    es = new EventSource(url(token));
                } catch (e) {
                    return finish(e);
                }
                es.onmessage = (e) => {
                    if (!e.data) return;
                    let evt;
                    try { evt = JSON.parse(e.data); } catch (_) { return; }
                    // Reset retry counter on any successful message — we're alive.
                    reconnects = 0;
                    try { view.handleEvent(evt); } catch (handlerErr) {
                        console.warn('view.handleEvent threw:', handlerErr);
                    }
                    if (evt.type === 'done') finish();
                    if (evt.type === 'error') finish(new Error(evt.message || failureMessage));
                    if (evt.type === 'cancelled') {
                        // Phase C2 — backend signalled the pipeline was
                        // cancelled (either by /cancel endpoint on this
                        // replica or via cross-replica pub/sub). Treat
                        // as a clean terminal state, not an error.
                        const cancelErr = new Error('Query cancelled.');
                        cancelErr.name = 'AbortError';
                        finish(cancelErr);
                    }
                };
                es.onerror = async () => {
                    if (completed) return;
                    try { es.close(); } catch (_) {}
                    if (reconnects >= MAX_RECONNECTS) {
                        return finish(new Error('SSE connection error (max reconnects reached)'));
                    }
                    reconnects += 1;
                    // Refresh the token before re-opening — covers JWT expiry,
                    // which is the dominant cause of long-lived SSE failures.
                    try {
                        if (typeof window.amorAuth?.refresh === 'function') {
                            await window.amorAuth.refresh();
                        }
                    } catch (refreshErr) {
                        console.warn('SSE: token refresh failed', refreshErr);
                        // continue anyway — maybe the server is just slow
                    }
                    const backoffMs = Math.min(reconnects, 5) * 1000;
                    setTimeout(open, backoffMs);
                };
            };

            // Proactive reconnect: when auth refreshes mid-stream we want
            // to swap the connection over to the new token *before* the old
            // one is rejected by the server.
            const onAuthChanged = () => {
                if (completed || !es) return;
                try { es.close(); } catch (_) {}
                // Don't bump reconnects here — this isn't a failure path.
                open();
            };
            document.addEventListener('amor:auth-changed', onAuthChanged);

            open();
        });
    }

    async _pollThinking(sessionId, view) {
        const pollInterval = 2000;
        const maxAttempts = 600; // 20 min
        let attempts = 0;
        while (attempts < maxAttempts) {
            const resp = await this._authFetch(`/api/thinking/${sessionId}/status`);
            if (!resp.ok) throw new Error(`Status fetch failed: ${resp.status}`);
            const s = await resp.json();
            view.handleEvent({ type: 'snapshot', ...s });
            if (s.status === 'completed') return;
            if (s.status === 'failed') throw new Error(s.error || 'Thinking failed');
            await new Promise(r => setTimeout(r, pollInterval));
            attempts++;
        }
        throw new Error('Thinking timed out');
    }

    _streamResearch(sessionId, view) {
        // P0.3: Long-running research can outlive a 15-min JWT. EventSource
        // can't refresh credentials mid-stream, so we wrap the stream in a
        // self-healing loop:
        //   • on transient error → refresh token, reopen with backoff
        //   • on `amor:auth-changed` → proactively reopen with fresh token
        //   • cap reconnects to avoid tight loops if the server is down
        return this._sseLoop({
            url: (token) => `/api/local-ai/research/${sessionId}/events${
                token ? `?access_token=${encodeURIComponent(token)}` : ''
            }`,
            view,
            failureMessage: 'Research failed',
        });
    }

    async _pollResearchInto(sessionId, view) {
        const pollInterval = 2000;
        const maxAttempts = 900;
        let attempts = 0;
        while (attempts < maxAttempts) {
            const resp = await this._authFetch(`/api/local-ai/research/${sessionId}/status`);
            if (!resp.ok) throw new Error(`Status fetch failed: ${resp.status}`);
            const s = await resp.json();
            view.handleEvent({ type: 'snapshot', ...s });
            if (s.status === 'completed') return;
            if (s.status === 'failed') throw new Error(s.error || 'Research failed');
            await new Promise(r => setTimeout(r, pollInterval));
            attempts++;
        }
        throw new Error('Research timed out');
    }

    async simpleLocalAIRequest(endpoint, message, typingId) {
        try {
            const response = await this._authFetch(endpoint, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    prompt: message,
                    mode: this.mode,
                    history: this.messageHistory
                })
            });

            if (!response.ok) {
                let detail = '';
                try {
                    const errBody = await response.json();
                    detail = errBody.detail || '';
                } catch (_) {
                    // ignore JSON parse errors
                }
                const statusInfo = `${response.status} ${response.statusText}`.trim();
                const extra = detail ? ` - ${detail}` : '';
                throw new Error(`Local AI error (${statusInfo})${extra}`);
            }

            const data = await response.json();
            this.removeTypingIndicator(typingId);

            const content = data.response || data.content || data.text;
            this.addMessage('assistant', content, 'local-ai', {
                metadata: data.metadata
            });

            const assistantMsg = {
                role: 'assistant',
                content,
                aiType: 'local-ai',
                format: 'text',
                extras: {
                    metadata: data.metadata
                }
            };
            this.messageHistory.push(assistantMsg);
            await this.persistChatMessage(assistantMsg);

        } catch (error) {
            throw new Error(`Local AI failed: ${error.message}`);
        }
    }

    async pollResearchStatus(sessionId, typingId) {
        const pollInterval = 2000; // 2 seconds
        // Local research can take longer on slower hardware; keep the UI polling longer
        // instead of failing early with "Research timeout".
        const maxAttempts = 900; // 30 minutes max
        let attempts = 0;

        while (attempts < maxAttempts) {
            try {
                const response = await this._authFetch(`/api/local-ai/research/${sessionId}/status`);

                if (!response.ok) {
                    throw new Error('Failed to fetch status');
                }

                const status = await response.json();

                // Update progress
                this.updateProgress(status);

                if (status.status === 'completed') {
                    this.removeTypingIndicator(typingId);
                    await this.displayResearchResults(status);
                    return;
                }

                if (status.status === 'failed') {
                    throw new Error(status.error || 'Research failed');
                }

                await new Promise(resolve => setTimeout(resolve, pollInterval));
                attempts++;

            } catch (error) {
                console.error('Polling error:', error);
                throw error;
            }
        }

        throw new Error('Research timeout - please try again');
    }

    showProgressModal() {
        if (this.progressModal) {
            this.progressModal.style.display = 'flex';
        }
    }

    hideProgressModal() {
        if (this.progressModal) {
            this.progressModal.style.display = 'none';
        }
    }

    updateProgress(status) {
        const progressBar = document.getElementById('progressBar');
        const progressStatus = document.getElementById('progressStatus');

        if (progressBar) {
            progressBar.style.width = `${status.progress || 0}%`;
        }

        if (progressStatus && status.current_task) {
            progressStatus.textContent = status.current_task;
        }

        // Update agent status
        const agentItems = document.querySelectorAll('.agent-item');
        agentItems.forEach(item => {
            item.classList.remove('active');
        });

        if (status.current_agent) {
            const activeAgent = Array.from(agentItems).find(item =>
                item.querySelector('.agent-name')?.textContent.includes(status.current_agent)
            );
            if (activeAgent) {
                activeAgent.classList.add('active');
                const agentStatus = activeAgent.querySelector('.agent-status');
                if (agentStatus) {
                    agentStatus.textContent = 'Working...';
                }
            }
        }
    }

    async displayResearchResults(status) {
        const content = this.formatResearchResults(status);
        this.addMessage('assistant', content, 'local-ai', {
            sources: status.sources,
            confidence: status.confidence
        });

        // Persist the full research result so history can be restored exactly.
        const assistantMsg = {
            role: 'assistant',
            content,
            aiType: 'local-ai',
            format: 'html',
            extras: {
                research: status
            }
        };
        this.messageHistory.push(assistantMsg);
        await this.persistChatMessage(assistantMsg);
    }

    formatResearchResults(status) {
        let html = '<div class="research-result">';

        // Research metadata header
        const sourceCount = status.sources?.length || 0;
        const depthLabel = {
            'basic': 'Basic',
            'medium': 'Medium',
            'deep': 'Deep',
            'expert': 'Expert',
            'ultra': 'Ultra',
            // legacy aliases
            'quick': 'Basic',
            'standard': 'Medium',
        }[status.depth] || 'Medium';
        
        html += `
            <div class="research-meta">
                <span class="meta-item" title="Research depth">
                    <i class="fas fa-layer-group"></i> ${depthLabel}
                </span>
                <span class="meta-item" title="Number of sources">
                    <i class="fas fa-globe"></i> ${sourceCount} sources
                </span>
                <span class="meta-item confidence-badge ${this.getConfidenceClass(status.confidence)}" title="Confidence level">
                    <i class="fas fa-chart-line"></i> ${status.confidence || 'N/A'}%
                </span>
                ${status.translated ? `
                    <span class="meta-item translated-badge" title="Some sources were auto-translated">
                        <i class="fas fa-language"></i> Translated
                    </span>
                ` : ''}
            </div>
        `;

        if (status.summary) {
            html += `
                <div class="research-section">
                    <h4><i class="fas fa-file-alt"></i> Summary</h4>
                    <div class="section-content">
                        <p>${this.escapeHtml(status.summary)}</p>
                    </div>
                </div>
            `;
        }

        if (status.findings && status.findings.length > 0) {
            html += `
                <div class="research-section">
                    <h4><i class="fas fa-lightbulb"></i> Key Findings</h4>
                    <div class="section-content">
                        <ul class="findings-list">
                            ${status.findings.map((f, i) => `
                                <li>
                                    <span class="finding-number">${i + 1}</span>
                                    <span class="finding-text">${this.escapeHtml(f)}</span>
                                </li>
                            `).join('')}
                        </ul>
                    </div>
                </div>
            `;
        }

        if (status.analysis) {
            const analysisPreview = status.analysis.length > 500 
                ? status.analysis.substring(0, 500) + '...' 
                : status.analysis;
            const needsExpand = status.analysis.length > 500;
            
            html += `
                <div class="research-section analysis-section">
                    <h4><i class="fas fa-microscope"></i> Analysis</h4>
                    <div class="section-content ${needsExpand ? 'expandable' : ''}">
                        <div class="analysis-preview">${this.formatAnalysisText(analysisPreview)}</div>
                        ${needsExpand ? `
                            <div class="analysis-full" style="display: none;">${this.formatAnalysisText(status.analysis)}</div>
                            <button class="expand-btn" onclick="this.parentElement.classList.toggle('expanded'); this.textContent = this.textContent === 'Show more' ? 'Show less' : 'Show more'; this.previousElementSibling.style.display = this.previousElementSibling.style.display === 'none' ? 'block' : 'none'; this.previousElementSibling.previousElementSibling.style.display = this.previousElementSibling.previousElementSibling.style.display === 'none' ? 'block' : 'none';">Show more</button>
                        ` : ''}
                    </div>
                </div>
            `;
        }

        if (status.sources && status.sources.length > 0) {
            html += `
                <div class="research-section sources-section">
                    <h4><i class="fas fa-book"></i> Sources (${sourceCount})</h4>
                    <div class="section-content">
                        <div class="sources-list">
                            ${status.sources.map((s, i) => `
                                <div class="source-item ${s.translated ? 'translated' : ''}">
                                    <span class="source-number">${i + 1}</span>
                                    <div class="source-info">
                                        <a href="${s.url}" target="_blank" rel="noopener noreferrer">
                                            ${this.escapeHtml(s.title || new URL(s.url).hostname)}
                                        </a>
                                        ${s.translated ? `
                                            <span class="source-lang-badge" title="Translated from ${s.original_language}">
                                                <i class="fas fa-language"></i> ${s.original_language?.toUpperCase() || 'AUTO'}
                                            </span>
                                        ` : ''}
                                    </div>
                                </div>
                            `).join('')}
                        </div>
                    </div>
                </div>
            `;
        }

        html += '</div>';
        return html;
    }

    getConfidenceClass(confidence) {
        if (!confidence) return 'low';
        if (confidence >= 80) return 'high';
        if (confidence >= 50) return 'medium';
        return 'low';
    }

    formatAnalysisText(text) {
        // Convert line breaks to paragraphs and preserve formatting
        return text
            .split('\n\n')
            .filter(p => p.trim())
            .map(p => `<p>${this.escapeHtml(p.trim())}</p>`)
            .join('');
    }

    addMessage(role, content, aiType = null, extras = {}) {
        const messageDiv = document.createElement('div');
        messageDiv.className = `message ${role}`;
        if (aiType) messageDiv.classList.add(aiType);

        const now = new Date();
        const timeStr = now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

        let avatar, name;
        if (role === 'user') {
            avatar = '👤';
            name = 'You';
        } else {
            if (aiType === 'claude') {
                avatar = '🤖';
                name = 'Claude';
            } else {
                avatar = this.getModeIcon();
                name = this.getModeName();
            }
        }

        messageDiv.innerHTML = `
            <div class="message-bubble">
                <div class="message-header">
                    <div class="message-avatar">${avatar}</div>
                    <span class="message-name">${name}</span>
                    <span class="message-time">${timeStr}</span>
                </div>
                <div class="message-content">${content}</div>
            </div>
        `;

        this.messagesArea?.appendChild(messageDiv);
        this.scrollToBottom();
    }

    addTypingIndicator() {
        const typingDiv = document.createElement('div');
        const typingId = 'typing-' + Date.now();
        typingDiv.id = typingId;
        typingDiv.className = 'message assistant';

        const useClaudeAPI = this.useClaudeAPI?.checked || false;
        const avatar = useClaudeAPI ? '🤖' : this.getModeIcon();
        const name = useClaudeAPI ? 'Claude' : this.getModeName();

        typingDiv.innerHTML = `
            <div class="message-bubble">
                <div class="message-header">
                    <div class="message-avatar">${avatar}</div>
                    <span class="message-name">${name}</span>
                </div>
                <div class="message-content">
                    <div class="typing-indicator">
                        <div class="typing-dot"></div>
                        <div class="typing-dot"></div>
                        <div class="typing-dot"></div>
                    </div>
                </div>
            </div>
        `;
        this.messagesArea?.appendChild(typingDiv);
        this.scrollToBottom();
        return typingId;
    }

    removeTypingIndicator(typingId) {
        const typingDiv = document.getElementById(typingId);
        if (typingDiv) {
            typingDiv.remove();
        }
    }

    scrollToBottom() {
        if (this.messagesArea) {
            this.messagesArea.scrollTop = this.messagesArea.scrollHeight;
        }
    }

    clearMessages() {
        if (this.messagesArea) {
            // Remove all messages except welcome container
            const messages = this.messagesArea.querySelectorAll('.message');
            messages.forEach(msg => msg.remove());
        }
        this.messageHistory = [];

        // Show welcome container if it exists
        const welcomeContainer = document.getElementById('welcomeContainer');
        if (welcomeContainer) {
            // Keep original layout (welcome container is not a flex layout).
            welcomeContainer.style.display = 'block';
        }
    }

    loadMessages(messages) {
        this.clearMessages();
        messages.forEach(msg => {
            if (msg.format === 'research' && msg.extras?.research && typeof ResearchView === 'function') {
                try {
                    const view = ResearchView.fromSnapshot(msg.extras.research);
                    this._mountResearchCard(view);
                    return;
                } catch (e) {
                    console.warn('Failed to restore research snapshot:', e);
                }
            }
            if (msg.format === 'thinking' && msg.extras?.thinking && typeof ThinkingView === 'function') {
                try {
                    const view = ThinkingView.fromSnapshot(msg.extras.thinking);
                    this._mountThinkingCard(view);
                    return;
                } catch (e) {
                    console.warn('Failed to restore thinking snapshot:', e);
                }
            }
            this.addMessage(msg.role, msg.content, msg.aiType, msg.extras || {});
        });
        // Keep full message metadata so history restores properly.
        this.messageHistory = (messages || []).map(m => ({
            role: m.role,
            content: m.content,
            aiType: m.aiType,
            format: m.format || 'text',
            extras: m.extras || {}
        }));
    }

    getMessages() {
        return this.messageHistory;
    }

    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
}

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    // Only initialize if chat elements exist
    if (document.getElementById('messagesArea')) {
        // Get initial mode from app state if available
        const initialMode = window.appState?.currentMode || 'research';
        window.chatController = new ChatController(initialMode);
        // Attach current server session id if it already exists
        if (window.appState?.currentSessionId && window.chatController.setChatSessionId) {
            window.chatController.setChatSessionId(window.appState.currentSessionId);
        }
        console.log(`✅ Chat Controller initialized - Mode: ${initialMode}`);

        // P0.2: After auth has had a chance to settle, try to re-attach to
        // any in-flight research session. We wait for the auth layer to
        // boot (it dispatches 'amor:auth-changed' on the initial silent
        // refresh) so the /status fetch goes out with a valid token.
        const tryResume = () => {
            try { window.chatController?._resumeActiveResearchIfAny?.(); }
            catch (e) { console.warn('resume-research bootstrap failed:', e); }
        };
        if (window.amorAuth?.isAuthenticated?.()) {
            tryResume();
        } else {
            // Wait once for the next auth-state change, then try.
            const onAuth = () => {
                document.removeEventListener('amor:auth-changed', onAuth);
                tryResume();
            };
            document.addEventListener('amor:auth-changed', onAuth);
            // Safety net: if no auth event fires within 4s, bail out — a
            // logged-out reload should not keep listening forever.
            setTimeout(() => document.removeEventListener('amor:auth-changed', onAuth), 4000);
        }
    }
});
