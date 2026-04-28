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
        this._currentCodeBackendId = null;

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
        // v4 — debounced model recommendation as the user types.
        // Idempotent (the method handles short prompts + dismissed state).
        this.chatInput?.addEventListener('input', () => {
            try { this._refreshRecommendation?.(this.chatInput.value || ''); }
            catch (_) {}
        });
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
            // Lazy-init the AI Model picker on first open. Idempotent.
            this._initModelPicker?.().catch(err => {
                console.warn('model picker init failed:', err);
            });
        };
        const closePanel = () => {
            settingsPanel.style.display = 'none';
            settingsBtn?.classList.remove('active');
        };

        this.openResearchSettingsPanel = openPanel;
        this.closeResearchSettingsPanel = closePanel;

        // Render the topbar chip on boot in case a previous session
        // saved a tag — must reflect the current state immediately,
        // not only after the panel is opened.
        try { this._renderModelChip?.(); } catch (_) {}

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
            this._currentCodeBackendId = null;
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
            } else if (this.mode === 'code') {
                // Code Intelligence is local-only by design (zero-API
                // multi-agent engine). The Claude toggle is ignored here.
                await this._runCodeIntelligence(message, typingId);
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
            this._currentCodeBackendId = null;
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
        } else if (mode === 'code' && this._currentCodeBackendId) {
            calls.push(this._authFetch(
                `/api/code/${encodeURIComponent(this._currentCodeBackendId)}/cancel`,
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
                // Optional Ollama tag override (More settings → AI Model)
                preferred_model: this._readPreferredModel() || null,
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
                    // Optional Ollama tag override (More settings → AI Model)
                    preferred_model: this._readPreferredModel() || null,
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

    // ────────────────────────────────────────────────────── Code Intelligence Mode
    //
    // Flow:
    //   1. POST /api/code/triage         → fast classification
    //   2. POST /api/code/start          → returns session_id
    //   3. SSE /api/code/{sid}/events    → live phase + sandbox events
    //   4. Final snapshot persisted into the chat history
    //
    // 100% local — no Claude path. The mode-button-selected provider is
    // ignored for code intelligence by design (zero-API engine).

    async _runCodeIntelligence(message, typingId) {
        if (typeof CodeView !== 'function') {
            throw new Error('CodeView component is not loaded');
        }

        // Effort piggybacks on the same depth selector used by Thinking.
        const depthSelect = document.getElementById('researchDepth');
        const effort = depthSelect?.value || 'medium';

        const view = new CodeView(message, effort, null);
        this.removeTypingIndicator(typingId);
        this._mountCodeCard(view);

        // 1. Quick triage (best effort — failure doesn't block start).
        try {
            const triageRes = await this._authFetch('/api/code/triage', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ prompt: message }),
                signal: this._currentAbortController?.signal,
            });
            if (triageRes.ok) {
                const triage = await triageRes.json();
                view.handleEvent({ type: 'phase_complete',
                    phase: 'triage', label: 'Triage', detail: triage });
                if (triage?.language) view.language = triage.language;
            }
        } catch (e) {
            console.warn('code triage failed (non-fatal):', e);
        }

        // 2. Start the pipeline.
        let session;
        try {
            const res = await this._authFetch('/api/code/start', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    prompt: message,
                    code_context: null,
                    language: null,
                    effort,
                    provider: 'local',
                    enable_execution: true,
                    enable_static_analysis: true,
                    enable_testing: true,
                    chat_session_id: this.chatSessionId || null,
                    query_record_id: this._currentQueryRecordId || null,
                    user_message_idempotency_key: this._currentUserMsgKey || null,
                    assistant_message_idempotency_key: this._currentAssistantMsgKey || null,
                    // Optional Ollama tag override (More settings → AI Model).
                    // When null, the engine auto-selects per role+effort.
                    preferred_model: this._readPreferredModel() || null,
                }),
                signal: this._currentAbortController?.signal,
            });
            if (!res.ok) {
                let detail = '';
                try { detail = (await res.json()).detail || ''; } catch (_) {}
                throw new Error(`Failed to start code intelligence (${res.status})${detail ? ' - ' + detail : ''}`);
            }
            session = await res.json();
        } catch (err) {
            view.handleEvent({ type: 'error', message: err.message });
            throw err;
        }

        // Stash the backend session id so the Stop button can hit
        // /api/code/{sid}/cancel directly.
        this._currentCodeBackendId = session.session_id;
        this._persistActiveCode(session.session_id);

        view.showTimeline({ session_id: session.session_id, phases: [] });

        // 3. Stream — SSE first, polling fallback.
        let runError = null;
        try {
            try {
                await this._streamCode(session.session_id, view);
            } catch (sseErr) {
                console.warn('Code SSE failed, polling fallback:', sseErr);
                await this._pollCode(session.session_id, view);
            }
        } catch (err) {
            runError = err;
            try { view.handleEvent({ type: 'error',
                message: err.message || 'Code intelligence failed' }); } catch (_) {}
        } finally {
            this._clearActiveCode();
        }

        // 4. Persist snapshot — chat history reload re-mounts the rich card.
        const snap = view.toSnapshot();
        const assistantMsg = {
            role: 'assistant',
            content: runError
                ? `Code intelligence failed: ${runError.message}`
                : '',
            aiType: 'local-code',
            format: 'code',
            extras: { code: snap, error: runError ? runError.message : undefined },
        };
        this.messageHistory.push(assistantMsg);
        try { await this.persistChatMessage(assistantMsg); } catch (persistErr) {
            console.warn('Failed to persist code snapshot:', persistErr);
        }

        if (runError) throw runError;
    }

    _mountCodeCard(view) {
        const wrap = document.createElement('div');
        wrap.className = 'message assistant local-code';
        const time = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        wrap.innerHTML = `
            <div class="message-bubble code-bubble">
                <div class="message-header">
                    <div class="message-avatar">⚙</div>
                    <span class="message-name">Code Intelligence</span>
                    <span class="message-time">${time}</span>
                </div>
                <div class="message-content code-content"></div>
            </div>
        `;
        wrap.querySelector('.code-content').appendChild(view.getElement());
        this.messagesArea?.appendChild(wrap);
        this.scrollToBottom();
    }

    _streamCode(sessionId, view) {
        return this._sseLoop({
            url: (token) => `/api/code/${sessionId}/events${
                token ? `?access_token=${encodeURIComponent(token)}` : ''
            }`,
            view,
            failureMessage: 'Code intelligence failed',
        });
    }

    async _pollCode(sessionId, view) {
        // Polling fallback if SSE is wedged. Same self-healing 400ms cadence
        // as _pollThinking but tracking code-mode session shape.
        const seen = new Set();
        const start = Date.now();
        const TIMEOUT_MS = 30 * 60 * 1000; // 30 min hard cap

        while (true) {
            if (Date.now() - start > TIMEOUT_MS) {
                throw new Error('Code intelligence polling timed out');
            }
            await new Promise(r => setTimeout(r, 800));
            try {
                const resp = await this._authFetch(
                    `/api/code/${encodeURIComponent(sessionId)}/status`
                );
                if (!resp.ok) {
                    if (resp.status === 404) throw new Error('Code session lost');
                    continue;
                }
                const snap = await resp.json();
                view.handleEvent({ type: 'snapshot', ...snap });
                if (snap.status === 'completed') {
                    view.handleEvent({ type: 'done' });
                    return;
                }
                if (snap.status === 'failed') {
                    throw new Error(snap.error || 'Code intelligence failed');
                }
                if (snap.status === 'cancelled') {
                    view.handleEvent({ type: 'cancelled' });
                    return;
                }
            } catch (e) {
                console.warn('code polling error:', e);
            }
        }
    }

    // ── AI Model selector (More settings → AI Model) ───────────────────
    //
    // Three-tab UI backed by /api/models/* (Installed / Pull / Upload).
    // Per-mode scope toggle decides whether the chosen tag binds to a
    // single mode or all modes. Server-side resolution means the mere
    // act of saving a preference is enough — the next request, even
    // without a `preferred_model` field on the wire, will use it.
    //
    // localStorage `amor.preferredModel` is still written so:
    //  (a) the topbar chip shows the active tag without an HTTP call;
    //  (b) outbound bodies can carry preferred_model for back-compat.

    _MODEL_LS_KEY = 'amor.preferredModel';

    _readPreferredModel() {
        try {
            const raw = localStorage.getItem(this._MODEL_LS_KEY) || '';
            return raw.trim();
        } catch (_) { return ''; }
    }

    _writePreferredModel(tag) {
        try {
            const clean = (tag || '').trim();
            if (!clean) {
                localStorage.removeItem(this._MODEL_LS_KEY);
            } else {
                localStorage.setItem(this._MODEL_LS_KEY, clean);
            }
        } catch (_) { /* best-effort */ }
    }

    /** Idempotent state holder for the picker. */
    _modelState() {
        if (!this._modelStateObj) {
            this._modelStateObj = {
                scope: '__all__',         // active scope tab
                tab: 'installed',         // active source tab
                installed: [],            // list of {tag, display_name, ...}
                catalogue: [],            // pullable catalogue entries
                preferences: {},          // { mode: {model_tag, profile, ...} }
                autoSelect: null,         // {tag, reason}
                ollamaUp: true,
                pendingFile: null,        // currently-staged upload File
                pendingFileBytes: 0,
                uploading: false,
                pulling: false,
                // ── v3 additions ────────────────────────────────────────
                hardware: null,           // /api/models/hardware payload
                hardwarePref: 'auto',     // auto | gpu | gpu_partial | cpu
                gpuLayers: 999,           // Ollama num_gpu, used when hardwarePref=gpu_partial
                strategy: 'single',       // single | per_mode | per_role | ensemble
                routing: null,            // /api/models/routing payload
                discoverResults: [],      // search results
                discoverQuery: '',
                discoverSource: 'all',
                expandedTag: null,        // currently-expanded card's tag (one at a time)
                tested: {},               // tag → {output, elapsed_ms} cache
                ensembleMembers: [],      // selected tags for ensemble strategy
                // ── v4 additions ────────────────────────────────────────
                usage: {},                // tag → {count_total, by_mode, last_used_at}
                recommendation: null,     // {tag, reason, score, candidates}
                recommendDismissed: false,
                recommendQueryToken: 0,   // debounce token for /recommend
                liveTokRate: 0,           // streaming tok/s during Try it
            };
        }
        return this._modelStateObj;
    }

    // ── v4 — Preset library (Advanced expansion) ────────────────────────

    /** Hand-tuned slider preset library. Each value is what gets loaded
     *  when the user clicks a preset button in the Advanced panel. */
    _modelPresets() {
        return {
            creative: {
                temperature: 1.0, top_p: 0.95, top_k: 60,
                repeat_penalty: 1.05, num_ctx: 8192, num_gpu: 999,
                num_thread: 8, seed: -1,
                _description: 'High variance, broad sampling — good for ideation, brainstorms, marketing copy.',
            },
            precise: {
                temperature: 0.2, top_p: 0.7, top_k: 20,
                repeat_penalty: 1.1, num_ctx: 8192, num_gpu: 999,
                num_thread: 8, seed: -1,
                _description: 'Tight sampling — factual Q&A, summarisation, instruction following.',
            },
            coding: {
                temperature: 0.15, top_p: 0.95, top_k: 40,
                repeat_penalty: 1.05, num_ctx: 16384, num_gpu: 999,
                num_thread: 8, seed: -1,
                _description: 'Low temperature + larger context — code generation, refactoring, bug fixes.',
            },
            long_form: {
                temperature: 0.7, top_p: 0.9, top_k: 40,
                repeat_penalty: 1.15, num_ctx: 32768, num_gpu: 999,
                num_thread: 8, seed: -1,
                _description: 'Balanced sampling + 32k context — essays, deep research, multi-page docs.',
            },
            deterministic: {
                temperature: 0.0, top_p: 1.0, top_k: 1,
                repeat_penalty: 1.0, num_ctx: 4096, num_gpu: 999,
                num_thread: 8, seed: 42,
                _description: 'Greedy decoding + fixed seed — reproducible outputs, regression tests.',
            },
        };
    }

    /** Lazy-init the picker on first panel-open. */
    async _initModelPicker() {
        // Backwards-compatible name — defers to initModelSelector once.
        return this.initModelSelector();
    }

    /**
     * Initialise the picker. Wiring happens once; data refresh on every
     * call (cheap — backed by 2-min server-side cache).
     */
    async initModelSelector() {
        const root = document.getElementById('modelPicker');
        if (!root) return;

        // One-time wiring — tab buttons, scope buttons, drop-zone, etc.
        if (!root.dataset.wired) {
            root.dataset.wired = '1';
            this._wireModelTabs(root);
            this._wireModelScope(root);
            this._wireModelPullButton();
            this._wireModelUpload();
            // v3 — additional surfaces.
            this._wireHardwarePanel(root);
            this._wireModelStrategy(root);
            this._wireDiscoverTab(root);
            this._wireEnsembleConfig(root);
            // Topbar chip wiring — opens the panel + jumps to picker.
            const chip = document.getElementById('topBarModelChip');
            if (chip && !chip.dataset.wired) {
                chip.dataset.wired = '1';
                chip.addEventListener('click', () => {
                    if (typeof this.openResearchSettingsPanel === 'function') {
                        this.openResearchSettingsPanel();
                    }
                    setTimeout(() => root.scrollIntoView({behavior: 'smooth'}), 50);
                });
            }
        }

        // Refresh data + render every time.
        await this._refreshModelData();
        // v3 — async hardware probe + routing fetch run in parallel
        // with the main render so the panel never blocks on them.
        this._refreshHardware().catch(err =>
            console.warn('hardware probe failed:', err));
        this._refreshRouting().catch(err =>
            console.warn('routing fetch failed:', err));
    }

    _wireModelTabs(root) {
        root.querySelectorAll('.model-tab').forEach(btn => {
            btn.addEventListener('click', () => {
                const tab = btn.dataset.tab;
                if (!tab) return;
                this._modelState().tab = tab;
                root.querySelectorAll('.model-tab').forEach(b => {
                    b.classList.toggle('active', b === btn);
                    b.setAttribute('aria-selected', b === btn ? 'true' : 'false');
                });
                root.querySelectorAll('.model-tab-panel').forEach(p => {
                    const match = p.dataset.tabPanel === tab;
                    p.classList.toggle('active', match);
                    p.hidden = !match;
                });
            });
        });
    }

    _wireModelScope(root) {
        root.querySelectorAll('.model-scope-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                const scope = btn.dataset.scope || '__all__';
                this._modelState().scope = scope;
                root.querySelectorAll('.model-scope-btn').forEach(b => {
                    b.classList.toggle('active', b === btn);
                    b.setAttribute('aria-selected', b === btn ? 'true' : 'false');
                });
                // Re-render so per-mode "is_preferred" reflects the new scope.
                this._refreshModelData().catch(err =>
                    console.warn('model scope refresh failed:', err));
            });
        });
    }

    /**
     * Hit /api/models?mode=<scope> and re-render Installed / Pull tabs.
     * Falls back gracefully if Ollama is unreachable.
     *
     * v4 — also captures the embedded `hardware` envelope (so the
     * picker doesn't need a separate /hardware roundtrip on first
     * paint) and the per-tag `usage` counts.
     */
    async _refreshModelData() {
        const state = this._modelState();
        const url = `/api/models?mode=${encodeURIComponent(state.scope)}&effort=medium`;
        try {
            const resp = await this._authFetch(url);
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const data = await resp.json();
            state.installed = Array.isArray(data.installed) ? data.installed : [];
            state.catalogue = Array.isArray(data.catalogue) ? data.catalogue : [];
            state.autoSelect = data.auto_select || null;
            state.ollamaUp = !!data.ollama_available;
            // Keep `preferences` in sync with what the server says is
            // active for THIS scope — used by the rendering pass.
            state.preferences = state.preferences || {};
            state.preferences[state.scope] = data.active_preference || {};
            // v4 — hardware envelope embedded in the list response.
            if (data.hardware) {
                state.hardware = data.hardware;
                this._renderHardwarePanel();
            }
            // v4 — collect usage map from card-level `usage` fields.
            const usage = {};
            for (const m of state.installed) {
                if (m.usage && m.usage.count_total) usage[m.tag] = m.usage;
            }
            state.usage = usage;
        } catch (err) {
            console.warn('model list fetch failed:', err);
            state.ollamaUp = false;
        }
        this._renderInstalledTab();
        this._renderCatalogueTab();
        this._renderPanelFooter();
        this._renderModelChip();
        this._syncLegacySelect();
        this._renderRecommendBanner();
    }

    /** Keep the (now-hidden) <select id="aiModelSelect"> in sync so
     *  any code path still reading from it via DOM gets the right value. */
    _syncLegacySelect() {
        const sel = document.getElementById('aiModelSelect');
        if (!sel) return;
        const tag = this._readPreferredModel();
        // Inject an option for the current tag if missing.
        if (tag && !Array.from(sel.options).some(o => o.value === tag)) {
            const opt = document.createElement('option');
            opt.value = tag; opt.textContent = tag;
            sel.appendChild(opt);
        }
        sel.value = tag || '';
    }

    _renderInstalledTab() {
        const state = this._modelState();
        const list = document.getElementById('modelInstalledList');
        const empty = document.getElementById('modelInstalledEmpty');
        const count = document.getElementById('modelTabCountInstalled');
        const autoCard = document.getElementById('modelAutoCard');
        const autoMeta = document.getElementById('modelAutoMeta');
        if (!list) return;

        if (count) count.textContent = String(state.installed.length);
        if (autoMeta) {
            const auto = state.autoSelect;
            autoMeta.textContent = auto?.tag
                ? `Will pick ${auto.tag} — ${auto.reason || ''}`.trim()
                : 'Server picks the best installed model for each request.';
        }
        const activeTag = (state.preferences[state.scope]?.tag || '').toLowerCase();
        if (autoCard) {
            const isAuto = !activeTag;
            autoCard.classList.toggle('active', isAuto);
            autoCard.setAttribute('aria-pressed', isAuto ? 'true' : 'false');
            if (!autoCard.dataset.wired) {
                autoCard.dataset.wired = '1';
                autoCard.addEventListener('click', () => this._clearPreference());
            }
        }

        list.innerHTML = '';
        if (!state.installed.length) {
            if (empty) empty.hidden = false;
            return;
        }
        if (empty) empty.hidden = true;

        state.installed.forEach(m => {
            const card = this._makeModelCard(m, {pullable: false});
            const isPref = (m.tag || '').toLowerCase() === activeTag;
            if (isPref) card.classList.add('active');
            list.appendChild(card);
        });
    }

    _renderCatalogueTab() {
        const state = this._modelState();
        const list = document.getElementById('modelCatalogueList');
        const count = document.getElementById('modelTabCountPull');
        if (!list) return;

        const installable = state.catalogue.filter(m => !m.is_installed);
        if (count) count.textContent = String(installable.length);

        list.innerHTML = '';
        installable.forEach(m => {
            const card = this._makeModelCard(m, {pullable: true});
            list.appendChild(card);
        });
    }

    /** Build one .model-card. Installed cards expose an Advanced
     *  expansion (sliders + system prompt + Save / Try-it / Reset).
     *  Pullable cards click-to-pull; installed cards click-to-select. */
    _makeModelCard(model, {pullable}) {
        const card = document.createElement('div');
        card.className = 'model-card';
        card.setAttribute('role', 'button');
        card.tabIndex = 0;
        card.dataset.tag = model.tag;
        if (model.is_custom) card.classList.add('model-card-custom');
        if (model.is_auto_selected) card.classList.add('model-card-auto-pick');

        const spec = model.spec || {};
        const tier = spec.tier || (model.is_custom ? 'custom' : 'unknown');
        const bench = spec.swebench_pct
            ? `${Math.round(spec.swebench_pct)}% SWE-bench`
            : (spec.humaneval_pct ? `${Math.round(spec.humaneval_pct)}% HumanEval` : '');
        const sizeChip = model.size_bytes
            ? `${(model.size_bytes / 1024**3).toFixed(1)} GB`
            : (spec.vram_gb ? `${spec.vram_gb} GB VRAM` : '');

        const badge = pullable
            ? `<span class="model-card-badge model-card-badge-pull">Pull</span>`
            : (model.is_custom
                ? `<span class="model-card-badge model-card-badge-custom">custom</span>`
                : (model.is_auto_selected
                    ? `<span class="model-card-badge model-card-badge-auto">auto pick</span>`
                    : ''));

        // v4 — VRAM-fit badge (fits / tight / too_big / cpu / unknown).
        const fitBadge = (() => {
            const fit = model.fit;
            if (!fit || fit === 'unknown') return '';
            const labels = {
                fits: '✓ fits',
                tight: '⚠ tight',
                too_big: '✗ too big',
                cpu: 'CPU',
            };
            return `<span class="model-card-fit model-card-fit-${fit}">${labels[fit] || fit}</span>`;
        })();

        // v4 — usage counter chip ("used 47×").
        const usageBadge = (() => {
            const u = model.usage || {};
            const c = u.count_total || 0;
            if (c <= 0) return '';
            return `<span class="model-card-usage" title="${c} prior uses">used ${c}×</span>`;
        })();

        // Build the head + meta block. For installed cards we add an
        // Advanced toggle (chevron) on the right. For pullable cards
        // we keep the lean look.
        const advancedToggleHtml = pullable
            ? ''
            : `<button type="button" class="model-card-advanced-toggle"
                       title="Advanced options"
                       aria-label="Advanced options"
                       aria-expanded="false">
                   <i class="fas fa-chevron-down"></i>
               </button>`;

        card.innerHTML = `
            <div class="model-card-row">
                <div class="model-card-head">
                    <span class="model-card-icon"><i class="fas fa-${pullable ? 'cloud-download-alt' : 'cube'}"></i></span>
                    <span class="model-card-name">${this.escapeHtml(model.display_name || model.tag)}</span>
                    ${badge}
                </div>
                ${advancedToggleHtml}
            </div>
            <div class="model-card-meta">
                <code class="model-card-tag">${this.escapeHtml(model.tag)}</code>
                ${tier ? `<span class="model-card-tier">${this.escapeHtml(tier)}</span>` : ''}
                ${sizeChip ? `<span class="model-card-size">${this.escapeHtml(sizeChip)}</span>` : ''}
                ${bench ? `<span class="model-card-bench">${this.escapeHtml(bench)}</span>` : ''}
                ${fitBadge}
                ${usageBadge}
            </div>
        `;

        // Main click handler — select or pull. Keyboard activates too.
        const primaryAction = (e) => {
            // Don't trigger when the user clicked the chevron, delete,
            // or any nested button (handled by their own listeners).
            if (e.target.closest('.model-card-advanced-toggle, .model-card-delete, .model-advanced')) {
                return;
            }
            if (pullable) {
                this._pullModel(model.tag).catch(err =>
                    console.warn('model pull failed:', err));
            } else {
                this._savePreference(
                    model.tag,
                    model.is_custom ? 'gguf_upload' : 'ollama_registry',
                    model.display_name || model.tag,
                );
            }
        };
        card.addEventListener('click', primaryAction);
        card.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                primaryAction(e);
            }
        });

        // Advanced expansion (installed cards only).
        if (!pullable) {
            const toggleBtn = card.querySelector('.model-card-advanced-toggle');
            if (toggleBtn) {
                toggleBtn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    this._toggleAdvanced(card, model);
                });
            }
            // Auto-expand if this is the currently-active expanded card.
            if (this._modelState().expandedTag === model.tag) {
                // Defer so the DOM has the card attached first.
                setTimeout(() => this._toggleAdvanced(card, model, true), 0);
            }
        }

        // Custom models get a delete affordance.
        if (!pullable && model.is_custom) {
            const del = document.createElement('span');
            del.className = 'model-card-delete';
            del.title = 'Delete this custom model';
            del.innerHTML = '<i class="fas fa-trash-alt"></i>';
            del.addEventListener('click', (e) => {
                e.stopPropagation();
                this._deleteCustomModel(model.tag).catch(err =>
                    console.warn('model delete failed:', err));
            });
            // Insert into the head row so it's flush right of the toggle.
            card.querySelector('.model-card-row')?.appendChild(del);
        }

        return card;
    }

    // ── v3 — Advanced expansion (per-card profile editor) ──────────────

    /** Expand or collapse a card's Advanced panel. ``forceOpen`` forces
     *  the open state (used when restoring after re-render). */
    _toggleAdvanced(card, model, forceOpen) {
        const state = this._modelState();
        const existing = card.querySelector(':scope > .model-advanced');
        const toggleBtn = card.querySelector('.model-card-advanced-toggle');
        if (existing && !forceOpen) {
            // Collapse.
            existing.remove();
            card.classList.remove('model-card-expanded');
            toggleBtn?.setAttribute('aria-expanded', 'false');
            toggleBtn?.querySelector('i')?.classList.replace('fa-chevron-up', 'fa-chevron-down');
            if (state.expandedTag === model.tag) state.expandedTag = null;
            return;
        }
        if (existing && forceOpen) return;  // already open

        // Collapse any other expanded card (one at a time).
        document.querySelectorAll('.model-card.model-card-expanded')
            .forEach(c => {
                if (c !== card) {
                    c.classList.remove('model-card-expanded');
                    c.querySelector(':scope > .model-advanced')?.remove();
                    c.querySelector('.model-card-advanced-toggle')
                        ?.setAttribute('aria-expanded', 'false');
                }
            });

        const tpl = document.getElementById('modelAdvancedTemplate');
        if (!tpl) return;
        const node = tpl.content.firstElementChild.cloneNode(true);

        // Hydrate values from saved profile (if any).
        const profile = (model.profile)
            || (state.preferences[state.scope]?.profile)
            || {};
        this._hydrateAdvancedForm(node, profile);

        // Wire the form's controls.
        this._wireAdvancedForm(node, model);

        card.appendChild(node);
        card.classList.add('model-card-expanded');
        toggleBtn?.setAttribute('aria-expanded', 'true');
        toggleBtn?.querySelector('i')?.classList.replace('fa-chevron-down', 'fa-chevron-up');
        state.expandedTag = model.tag;
    }

    _hydrateAdvancedForm(root, profile) {
        const setRange = (key, fallback) => {
            const el = root.querySelector(`[data-key="${key}"]`);
            const out = root.querySelector(`[data-out="${key}"]`);
            if (!el) return;
            const v = (profile && profile[key] != null)
                ? profile[key]
                : fallback;
            el.value = v;
            if (out) out.textContent = String(v);
        };
        const setNumber = (key, fallback) => {
            const el = root.querySelector(`[data-key="${key}"]`);
            if (!el) return;
            el.value = (profile && profile[key] != null) ? profile[key] : fallback;
        };
        const setSelect = (key, fallback) => {
            const el = root.querySelector(`[data-key="${key}"]`);
            if (!el) return;
            const v = (profile && profile[key] != null)
                ? String(profile[key])
                : String(fallback);
            const opt = Array.from(el.options).find(o => o.value === v);
            el.value = opt ? v : String(fallback);
        };
        setRange('temperature', 0.7);
        setRange('top_p', 0.9);
        setNumber('top_k', 40);
        setRange('repeat_penalty', 1.1);
        setSelect('num_ctx', 4096);
        setRange('num_gpu', 999);
        setNumber('num_thread', 8);
        setNumber('seed', -1);
        const sp = root.querySelector('[data-key="system_prompt"]');
        if (sp) sp.value = profile?.system_prompt || '';
    }

    _wireAdvancedForm(root, model) {
        // Live-update value labels next to ranges.
        root.querySelectorAll('.adv-range').forEach(input => {
            input.addEventListener('input', () => {
                const out = root.querySelector(
                    `[data-out="${input.dataset.key}"]`,
                );
                if (out) out.textContent = input.value;
            });
        });

        // v4 — preset buttons load known-good slider configs.
        root.querySelectorAll('.adv-preset-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                const presets = this._modelPresets();
                const preset = presets[btn.dataset.preset];
                if (!preset) return;
                this._hydrateAdvancedForm(root, preset);
                root.querySelectorAll('.adv-range').forEach(i =>
                    i.dispatchEvent(new Event('input')),
                );
                root.querySelectorAll('.adv-preset-btn').forEach(b =>
                    b.classList.toggle('active', b === btn),
                );
                if (preset._description) {
                    this._toast?.(preset._description);
                }
            });
        });

        const collect = () => {
            const out = {};
            root.querySelectorAll('[data-key]').forEach(el => {
                const k = el.dataset.key;
                let v = el.value;
                if (el.tagName === 'TEXTAREA') {
                    if (v.trim()) out[k] = v.trim();
                    return;
                }
                if (el.type === 'range' || el.type === 'number') {
                    const num = Number(v);
                    if (!Number.isNaN(num)) out[k] = num;
                    return;
                }
                if (el.tagName === 'SELECT') {
                    const num = Number(v);
                    out[k] = Number.isNaN(num) ? v : num;
                }
            });
            return out;
        };

        // Save profile.
        root.querySelector('.adv-save-btn')?.addEventListener('click', () => {
            const profile = collect();
            this._savePreference(
                model.tag,
                model.is_custom ? 'gguf_upload' : 'ollama_registry',
                model.display_name || model.tag,
                profile,
            );
        });

        // Reset to template defaults.
        root.querySelector('.adv-reset-btn')?.addEventListener('click', () => {
            this._hydrateAdvancedForm(root, {});
            // Trigger range output refresh.
            root.querySelectorAll('.adv-range').forEach(input =>
                input.dispatchEvent(new Event('input')),
            );
        });

        // Try it — streams a tiny generation.
        root.querySelector('.adv-test-btn')?.addEventListener('click', () => {
            const promptInput = root.querySelector('.adv-test-prompt');
            const prompt = (promptInput?.value || '').trim()
                || 'Reply in one short sentence.';
            const profile = collect();
            const out = root.querySelector('.adv-test-output');
            if (out) {
                out.hidden = false;
                out.textContent = '…';
            }
            this._testGenerate(model.tag, prompt, profile, out)
                .catch(err => {
                    if (out) {
                        out.classList.add('adv-test-error');
                        out.textContent = `Failed: ${err.message}`;
                    }
                });
        });
    }

    _renderPanelFooter() {
        const el = document.getElementById('panelModelCurrent');
        if (!el) return;
        const state = this._modelState();
        const active = state.preferences[state.scope]?.tag;
        if (!active) {
            el.classList.add('is-auto');
            el.innerHTML = `Currently: <strong>Auto</strong> · <span>${this.escapeHtml(state.autoSelect?.tag || 'server-picked')}</span>`;
        } else {
            el.classList.remove('is-auto');
            const scopeLabel = state.scope === '__all__' ? 'all modes' : state.scope;
            el.innerHTML = `Currently: <strong>${this.escapeHtml(active)}</strong> · ${this.escapeHtml(scopeLabel)}`;
        }
    }

    /**
     * Render or hide the topbar chip showing the active model. Reads
     * localStorage so it works on first paint without an HTTP call.
     */
    _renderModelChip() {
        const chip = document.getElementById('topBarModelChip');
        const tagEl = document.getElementById('topBarModelTag');
        if (!chip || !tagEl) return;
        const tag = this._readPreferredModel();
        if (!tag) {
            chip.hidden = true;
            return;
        }
        tagEl.textContent = tag;
        chip.hidden = false;
    }

    /**
     * PUT /api/models/preference + sync localStorage + re-render.
     * v3 — accepts an optional ``profile`` dict (advanced options).
     */
    async _savePreference(tag, source, displayName, profile) {
        const state = this._modelState();
        // Layer the global hardware preference into the per-model profile
        // when one isn't already set there — this lets the picker's top
        // toggle ("Run on GPU/CPU") propagate into every saved model.
        const merged = this._mergeHardwareIntoProfile(profile);
        const body = {
            mode: state.scope,
            model_tag: tag,
            model_source: source || 'ollama_registry',
            display_name: displayName || tag,
        };
        if (merged && Object.keys(merged).length) body.profile = merged;

        try {
            const resp = await this._authFetch('/api/models/preference', {
                method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(body),
            });
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            // localStorage tracks the user's most recent pick across
            // ANY scope — this drives the topbar chip + the outbound
            // body's `preferred_model` field for clients on this tab.
            this._writePreferredModel(tag);
            this._toast?.(
                merged && Object.keys(merged).length
                    ? `Saved ${tag} with custom profile`
                    : `Model set: ${tag}`,
            );
            // v4 — fire-and-forget warmup so the next real request
            // doesn't pay the cold-load penalty. Failure is silent
            // (warmup is purely an optimisation).
            this._warmupModel(tag);
        } catch (err) {
            console.warn('save preference failed:', err);
            this._toast?.(`Failed to save preference: ${err.message}`, 'error');
        }
        await this._refreshModelData();
    }

    /** POST /api/models/warmup — fire-and-forget. */
    async _warmupModel(tag) {
        try {
            await this._authFetch('/api/models/warmup', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({tag}),
            });
        } catch (_) { /* best-effort */ }
    }

    /** Translate the hardware-pref toggle into Ollama options that are
     *  layered into the saved profile. ``hardwarePref`` of "auto" leaves
     *  whatever the user set in the Advanced sliders alone. */
    _mergeHardwareIntoProfile(profile) {
        const state = this._modelState();
        const out = profile ? {...profile} : {};
        const pref = state.hardwarePref || 'auto';
        if (pref === 'cpu') {
            out.num_gpu = 0;
        } else if (pref === 'gpu') {
            // 999 = "all layers on GPU" (Ollama clamps to model's actual layer count).
            if (out.num_gpu == null) out.num_gpu = 999;
        } else if (pref === 'gpu_partial') {
            // Only override if the user didn't already pick something explicit.
            if (out.num_gpu == null) out.num_gpu = state.gpuLayers || 32;
            else out.num_gpu = state.gpuLayers || out.num_gpu;
        }
        return out;
    }

    /** DELETE /api/models/preference/{mode} for the active scope. */
    async _clearPreference() {
        const state = this._modelState();
        try {
            await this._authFetch(
                `/api/models/preference/${encodeURIComponent(state.scope)}`,
                {method: 'DELETE'},
            );
            // Only nuke localStorage if no preference remains for any scope.
            this._writePreferredModel('');
        } catch (err) {
            console.warn('clear preference failed:', err);
        }
        await this._refreshModelData();
    }

    _wireModelPullButton() {
        const btn = document.getElementById('pullModelBtn');
        const input = document.getElementById('customModelTag');
        btn?.addEventListener('click', () => {
            const tag = (input?.value || '').trim();
            if (!tag) { input?.focus(); return; }
            this._pullModel(tag).catch(err =>
                console.warn('model pull failed:', err));
        });
        input?.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); btn?.click(); }
        });
    }

    /**
     * POST /api/models/pull (SSE). Drives #modelPullProgress and re-
     * fetches the catalogue when complete; auto-saves the just-pulled
     * tag as the active preference on success.
     */
    async _pullModel(tag) {
        const state = this._modelState();
        if (!tag || state.pulling) return;

        const progress = document.getElementById('modelPullProgress');
        const fill = document.getElementById('pullFill');
        const pct = document.getElementById('pullPct');
        const status = document.getElementById('pullStatus');
        const pullBtn = document.getElementById('pullModelBtn');

        if (progress) { progress.hidden = false; progress.classList.remove('error'); }
        if (pct) pct.textContent = '0%';
        if (fill) fill.style.width = '0%';
        if (status) status.textContent = 'Starting…';
        if (pullBtn) pullBtn.disabled = true;
        state.pulling = true;

        let resp;
        try {
            resp = await this._authFetch('/api/models/pull', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({tag}),
            });
            if (!resp.ok || !resp.body) {
                throw new Error(`pull start failed (${resp.status})`);
            }
        } catch (e) {
            if (status) status.textContent = `Failed: ${e.message}`;
            if (progress) progress.classList.add('error');
            if (pullBtn) pullBtn.disabled = false;
            state.pulling = false;
            return;
        }

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let success = false;
        try {
            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                buffer += decoder.decode(value, { stream: true });
                let idx;
                while ((idx = buffer.indexOf('\n\n')) !== -1) {
                    const chunk = buffer.slice(0, idx);
                    buffer = buffer.slice(idx + 2);
                    const dataLine = chunk.split('\n').find(l => l.startsWith('data:'));
                    if (!dataLine) continue;
                    let evt;
                    try { evt = JSON.parse(dataLine.slice(5).trim()); }
                    catch (_) { continue; }
                    if (evt.type === 'pull_progress') {
                        const p = Math.max(0, Math.min(100, Number(evt.pct || 0)));
                        if (fill) fill.style.width = `${p}%`;
                        if (pct) pct.textContent = `${p}%`;
                        if (status) status.textContent = evt.status || 'pulling…';
                    } else if (evt.type === 'pull_complete') {
                        if (fill) fill.style.width = '100%';
                        if (pct) pct.textContent = '100%';
                        if (status) status.textContent = 'Done';
                        success = true;
                    } else if (evt.type === 'pull_error') {
                        if (status) status.textContent = `Failed: ${evt.error || 'unknown'}`;
                        if (progress) progress.classList.add('error');
                    }
                }
            }
        } finally {
            if (pullBtn) pullBtn.disabled = false;
            state.pulling = false;
        }

        if (success) {
            // Refresh catalogue, then save the just-pulled tag for the
            // current scope so the next request actually uses it.
            await this._refreshModelData();
            await this._savePreference(tag, 'ollama_registry', tag);
            setTimeout(() => { if (progress) progress.hidden = true; }, 1500);
            // Clear the custom-tag input.
            const input = document.getElementById('customModelTag');
            if (input) input.value = '';
        }
    }

    // ── Tab 3 — GGUF upload ────────────────────────────────────────────

    _wireModelUpload() {
        const dropZone = document.getElementById('modelDropZone');
        const fileInput = document.getElementById('modelUploadInput');
        const cancelBtn = document.getElementById('modelUploadCancel');
        const submitBtn = document.getElementById('modelUploadSubmit');
        const limitEl = document.getElementById('modelUploadLimit');

        // The server-side limit is exposed via .env; we don't have a
        // dedicated endpoint, so the badge stays at the default 50 GB
        // unless overridden by the operator.
        if (limitEl) limitEl.textContent = '50';

        if (!dropZone || !fileInput) return;

        const handleFile = (file) => {
            if (!file) return;
            if (!/\.gguf$/i.test(file.name)) {
                this._toast?.('Pick a .gguf file', 'error');
                return;
            }
            this._stageUploadFile(file);
        };

        fileInput.addEventListener('change', () => handleFile(fileInput.files?.[0]));
        dropZone.addEventListener('click', () => fileInput.click());
        dropZone.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                fileInput.click();
            }
        });
        ['dragenter', 'dragover'].forEach(evt =>
            dropZone.addEventListener(evt, (e) => {
                e.preventDefault(); e.stopPropagation();
                dropZone.classList.add('dragging');
            })
        );
        ['dragleave', 'drop'].forEach(evt =>
            dropZone.addEventListener(evt, (e) => {
                e.preventDefault(); e.stopPropagation();
                dropZone.classList.remove('dragging');
            })
        );
        dropZone.addEventListener('drop', (e) => {
            const file = e.dataTransfer?.files?.[0];
            handleFile(file);
        });

        cancelBtn?.addEventListener('click', () => this._resetUploadForm());
        submitBtn?.addEventListener('click', () => this._submitUpload().catch(err =>
            console.warn('upload failed:', err)));
    }

    _stageUploadFile(file) {
        const state = this._modelState();
        state.pendingFile = file;
        state.pendingFileBytes = file.size;
        const form = document.getElementById('modelUploadForm');
        const dz = document.getElementById('modelDropZone');
        const fname = document.getElementById('modelUploadFname');
        const fsize = document.getElementById('modelUploadFsize');
        if (form) form.hidden = false;
        if (dz) dz.hidden = true;
        if (fname) fname.textContent = file.name;
        if (fsize) fsize.textContent = `${(file.size / 1024**3).toFixed(2)} GB`;
    }

    _resetUploadForm() {
        const state = this._modelState();
        state.pendingFile = null;
        state.pendingFileBytes = 0;
        state.uploading = false;
        const form = document.getElementById('modelUploadForm');
        const dz = document.getElementById('modelDropZone');
        const progress = document.getElementById('modelUploadProgress');
        const fileInput = document.getElementById('modelUploadInput');
        const display = document.getElementById('modelUploadDisplay');
        if (form) form.hidden = true;
        if (dz) dz.hidden = false;
        if (progress) progress.hidden = true;
        if (fileInput) fileInput.value = '';
        if (display) display.value = '';
    }

    async _submitUpload() {
        const state = this._modelState();
        if (!state.pendingFile || state.uploading) return;
        const file = state.pendingFile;
        const display = (document.getElementById('modelUploadDisplay')?.value || '').trim();

        const progress = document.getElementById('modelUploadProgress');
        const fill = document.getElementById('modelUploadFill');
        const pct = document.getElementById('modelUploadPct');
        const status = document.getElementById('modelUploadStatus');
        const submit = document.getElementById('modelUploadSubmit');
        if (progress) { progress.hidden = false; progress.classList.remove('error'); }
        if (fill) fill.style.width = '0%';
        if (pct) pct.textContent = '0%';
        if (status) status.textContent = 'Uploading…';
        if (submit) submit.disabled = true;
        state.uploading = true;

        const fd = new FormData();
        fd.append('file', file);
        if (display) fd.append('display_name', display);

        try {
            // We use XMLHttpRequest because fetch() doesn't expose
            // upload progress events in the browser.
            const result = await new Promise((resolve, reject) => {
                const xhr = new XMLHttpRequest();
                xhr.open('POST', '/api/models/upload', true);
                // Mirror _authFetch's headers (token + X-Client-Id) so
                // the server-side identity logic works for uploads too.
                const headers = (this._authHeaders ? this._authHeaders() : {});
                Object.entries(headers || {}).forEach(([k, v]) => {
                    if (v != null) xhr.setRequestHeader(k, v);
                });
                xhr.upload.onprogress = (e) => {
                    if (!e.lengthComputable) return;
                    const p = Math.round((e.loaded / e.total) * 100);
                    if (fill) fill.style.width = `${p}%`;
                    if (pct) pct.textContent = `${p}%`;
                    if (status && p < 100) status.textContent = 'Uploading…';
                    if (status && p >= 100) status.textContent = 'Registering with Ollama…';
                };
                xhr.onload = () => {
                    if (xhr.status >= 200 && xhr.status < 300) {
                        try { resolve(JSON.parse(xhr.responseText)); }
                        catch (_) { resolve({ok: true}); }
                    } else {
                        let detail = `HTTP ${xhr.status}`;
                        try {
                            const body = JSON.parse(xhr.responseText);
                            detail = body.detail || detail;
                        } catch (_) {}
                        reject(new Error(detail));
                    }
                };
                xhr.onerror = () => reject(new Error('Network error'));
                xhr.send(fd);
            });
            if (status) status.textContent = `Registered as ${result.tag}`;
            if (fill) fill.style.width = '100%';
            // Auto-save as the active preference for the current scope.
            await this._refreshModelData();
            await this._savePreference(
                result.tag, 'gguf_upload',
                result.display_name || file.name,
            );
            setTimeout(() => this._resetUploadForm(), 1800);
            // Switch to Installed tab so the user sees the new model.
            document.querySelector('.model-tab[data-tab="installed"]')?.click();
        } catch (err) {
            if (status) status.textContent = `Failed: ${err.message}`;
            if (progress) progress.classList.add('error');
        } finally {
            if (submit) submit.disabled = false;
            state.uploading = false;
        }
    }

    /** DELETE /api/models/custom/{tag} for an uploaded model. */
    async _deleteCustomModel(tag) {
        if (!tag.startsWith('custom/')) return;
        // No native confirm() in some testing harnesses; gate behind a
        // typed confirmation.
        const proceed = window.confirm?.(
            `Delete custom model ${tag}? This cannot be undone.`,
        );
        if (proceed === false) return;
        try {
            const resp = await this._authFetch(
                `/api/models/custom/${encodeURIComponent(tag)}`,
                {method: 'DELETE'},
            );
            if (!resp.ok) {
                const body = await resp.json().catch(() => ({}));
                throw new Error(body.detail || `HTTP ${resp.status}`);
            }
            this._toast?.(`Deleted ${tag}`);
        } catch (err) {
            console.warn('custom model delete failed:', err);
            this._toast?.(`Delete failed: ${err.message}`, 'error');
        }
        await this._refreshModelData();
    }

    /** Optional toast helper — falls back to console.log. */
    _toast(msg, kind) {
        try {
            const el = document.createElement('div');
            el.className = `model-toast model-toast-${kind || 'info'}`;
            el.textContent = msg;
            document.body.appendChild(el);
            setTimeout(() => el.classList.add('show'), 10);
            setTimeout(() => el.classList.remove('show'), 2400);
            setTimeout(() => el.remove(), 2700);
        } catch (_) {
            console.log('[model]', msg);
        }
    }

    // ── v3 — Hardware panel ────────────────────────────────────────────

    _wireHardwarePanel(root) {
        const refreshBtn = root.querySelector('#modelHwRefresh');
        refreshBtn?.addEventListener('click', () => {
            this._refreshHardware().catch(err =>
                console.warn('hardware refresh failed:', err));
        });
        const slider = root.querySelector('#modelHwGpuLayers');
        const sliderVal = root.querySelector('#modelHwGpuLayersValue');
        slider?.addEventListener('input', () => {
            const n = Number(slider.value);
            this._modelState().gpuLayers = n;
            if (sliderVal) sliderVal.textContent = String(n);
        });
        slider?.addEventListener('change', () => {
            this._saveRouting().catch(err =>
                console.warn('routing save failed:', err));
        });
        root.querySelectorAll('.hw-pref-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                const pref = btn.dataset.hw || 'auto';
                this._modelState().hardwarePref = pref;
                root.querySelectorAll('.hw-pref-btn').forEach(b => {
                    b.classList.toggle('active', b === btn);
                    b.setAttribute('aria-selected', b === btn ? 'true' : 'false');
                });
                const slideRow = root.querySelector('#modelHwGpuLayersRow');
                if (slideRow) slideRow.hidden = (pref !== 'gpu_partial');
                this._saveRouting().catch(err =>
                    console.warn('routing save failed:', err));
            });
        });
    }

    async _refreshHardware() {
        try {
            const resp = await this._authFetch('/api/models/hardware');
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const data = await resp.json();
            this._modelState().hardware = data;
            this._renderHardwarePanel();
        } catch (err) {
            console.warn('hardware probe failed:', err);
            const line = document.getElementById('modelHwLine');
            const sub = document.getElementById('modelHwSub');
            const icon = document.getElementById('modelHwIcon');
            if (icon) icon.innerHTML = '<i class="fas fa-exclamation-triangle"></i>';
            if (line) line.textContent = 'Hardware probe failed';
            if (sub) sub.textContent = err.message;
        }
    }

    _renderHardwarePanel() {
        const state = this._modelState();
        const hw = state.hardware || {};
        const line = document.getElementById('modelHwLine');
        const sub = document.getElementById('modelHwSub');
        const icon = document.getElementById('modelHwIcon');
        if (!line || !sub || !icon) return;
        if (hw.gpu_available) {
            icon.innerHTML = '<i class="fas fa-microchip" style="color: #6ddfb5"></i>';
            const name = hw.gpu_name || `${hw.gpu_count || 1}× GPU`;
            const vram = hw.vram_total_gb
                ? ` · ${hw.vram_total_gb} GB VRAM`
                : '';
            line.textContent = `GPU detected: ${name}${vram}`;
            const free = hw.vram_free_gb != null
                ? `${hw.vram_free_gb} GB free`
                : 'live VRAM unknown';
            const ver = hw.ollama_version
                ? ` · Ollama ${hw.ollama_version}`
                : '';
            sub.textContent = `${free}${ver} · CPU threads: ${hw.cpu_threads ?? '?'}`;
        } else {
            icon.innerHTML = '<i class="fas fa-microchip" style="color: #8a8aa0"></i>';
            line.textContent = 'CPU only';
            sub.textContent = `No GPU detected${hw.ollama_version ? ` · Ollama ${hw.ollama_version}` : ''} · CPU threads: ${hw.cpu_threads ?? '?'}`;
        }
    }

    // ── v3 — Strategy toggle (Single | Per-mode | Per-role | Ensemble) ─

    _wireModelStrategy(root) {
        root.querySelectorAll('.model-mode-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                const strategy = btn.dataset.strategy || 'single';
                this._modelState().strategy = strategy;
                root.querySelectorAll('.model-mode-btn').forEach(b => {
                    b.classList.toggle('active', b === btn);
                    b.setAttribute('aria-selected', b === btn ? 'true' : 'false');
                });
                this._renderStrategyHint();
                this._renderRoleMatrix();
                this._renderEnsembleConfig();
                this._saveRouting().catch(err =>
                    console.warn('routing save failed:', err));
            });
        });
    }

    _renderStrategyHint() {
        const hint = document.getElementById('modelModeHint');
        if (!hint) return;
        const map = {
            single:   'One model for everything.',
            per_mode: 'Pick a different model per mode (Research / Thinking / Code).',
            per_role: 'Code Intelligence: planner / coder / critic / reviewer can each have their own model.',
            ensemble: 'Run multiple models in parallel; pick the answer by voting.',
        };
        hint.textContent = map[this._modelState().strategy] || '';
    }

    _renderRoleMatrix() {
        const wrap = document.getElementById('modelRoleMatrix');
        const grid = document.getElementById('modelRoleGrid');
        if (!wrap || !grid) return;
        if (this._modelState().strategy !== 'per_role') {
            wrap.hidden = true;
            return;
        }
        wrap.hidden = false;
        const state = this._modelState();
        const installed = state.installed || [];
        const routes = (state.routing?.role_routes) || {};
        const roles = [
            {key: 'planner',  label: 'Planner',  icon: 'fa-route'},
            {key: 'coder',    label: 'Coder',    icon: 'fa-code'},
            {key: 'tester',   label: 'Tester',   icon: 'fa-vial'},
            {key: 'debugger', label: 'Debugger', icon: 'fa-bug'},
            {key: 'critic',   label: 'Critic',   icon: 'fa-balance-scale'},
            {key: 'reviewer', label: 'Reviewer', icon: 'fa-user-shield'},
        ];
        grid.innerHTML = '';
        roles.forEach(r => {
            const row = document.createElement('div');
            row.className = 'role-matrix-row';
            row.innerHTML = `
                <span class="role-matrix-icon"><i class="fas ${r.icon}"></i></span>
                <span class="role-matrix-name">${r.label}</span>
                <select class="role-matrix-select panel-setting-select">
                    <option value="">Auto</option>
                    ${installed.map(m =>
                        `<option value="${this.escapeHtml(m.tag)}"${m.tag === routes[r.key] ? ' selected' : ''}>${this.escapeHtml(m.display_name || m.tag)}</option>`
                    ).join('')}
                </select>
            `;
            const sel = row.querySelector('select');
            sel.addEventListener('change', () => {
                if (!state.routing) state.routing = {};
                state.routing.role_routes = {
                    ...(state.routing.role_routes || {}),
                    [r.key]: sel.value || null,
                };
                if (!sel.value) delete state.routing.role_routes[r.key];
                this._saveRouting().catch(err =>
                    console.warn('routing save failed:', err));
            });
            grid.appendChild(row);
        });
    }

    // ── v3 — Ensemble config ───────────────────────────────────────────

    _wireEnsembleConfig(root) {
        const sel = root.querySelector('#ensembleVotingSelect');
        sel?.addEventListener('change', () => {
            const state = this._modelState();
            if (!state.routing) state.routing = {};
            state.routing.ensemble = {
                ...(state.routing.ensemble || {}),
                voting: sel.value,
            };
            this._saveRouting().catch(err =>
                console.warn('routing save failed:', err));
        });
    }

    _renderEnsembleConfig() {
        const wrap = document.getElementById('modelEnsembleConfig');
        const list = document.getElementById('ensembleMembersList');
        const sel = document.getElementById('ensembleVotingSelect');
        if (!wrap || !list) return;
        const state = this._modelState();
        if (state.strategy !== 'ensemble') {
            wrap.hidden = true;
            return;
        }
        wrap.hidden = false;

        const members = state.routing?.ensemble?.members || [];
        const installed = state.installed || [];
        list.innerHTML = '';
        installed.forEach(m => {
            const row = document.createElement('label');
            row.className = 'ensemble-member-row';
            const checked = members.includes(m.tag) ? 'checked' : '';
            row.innerHTML = `
                <input type="checkbox" data-tag="${this.escapeHtml(m.tag)}" ${checked} />
                <span class="ensemble-member-name">${this.escapeHtml(m.display_name || m.tag)}</span>
                <code class="ensemble-member-tag">${this.escapeHtml(m.tag)}</code>
            `;
            const cb = row.querySelector('input');
            cb.addEventListener('change', () => {
                if (!state.routing) state.routing = {};
                const ens = state.routing.ensemble = state.routing.ensemble || {};
                const cur = new Set(ens.members || []);
                if (cb.checked) cur.add(m.tag);
                else cur.delete(m.tag);
                ens.members = Array.from(cur);
                this._saveRouting().catch(err =>
                    console.warn('routing save failed:', err));
            });
            list.appendChild(row);
        });

        if (sel) sel.value = state.routing?.ensemble?.voting || 'majority';
    }

    // ── v3 — Discover tab ──────────────────────────────────────────────

    _wireDiscoverTab(root) {
        const input = root.querySelector('#discoverSearchInput');
        const btn = root.querySelector('#discoverSearchBtn');
        const sel = root.querySelector('#discoverSourceSelect');
        const trigger = () => {
            const q = (input?.value || '').trim();
            const src = sel?.value || 'all';
            if (!q) { input?.focus(); return; }
            this._modelState().discoverQuery = q;
            this._modelState().discoverSource = src;
            this._runDiscoverSearch().catch(err =>
                console.warn('discover failed:', err));
        };
        btn?.addEventListener('click', trigger);
        input?.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); trigger(); }
        });
    }

    async _runDiscoverSearch() {
        const state = this._modelState();
        const list = document.getElementById('modelDiscoverList');
        const empty = document.getElementById('modelDiscoverEmpty');
        const count = document.getElementById('modelTabCountDiscover');
        if (!list) return;

        list.innerHTML = `<div class="model-empty"><i class="fas fa-spinner fa-spin"></i><span>Searching…</span></div>`;
        try {
            const url = `/api/models/search?q=${encodeURIComponent(state.discoverQuery)}&source=${encodeURIComponent(state.discoverSource)}&limit=24`;
            const resp = await this._authFetch(url);
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const data = await resp.json();
            state.discoverResults = data.results || [];
        } catch (err) {
            state.discoverResults = [];
            list.innerHTML = `<div class="model-empty model-empty-error"><i class="fas fa-exclamation-triangle"></i><span>Search failed: ${this.escapeHtml(err.message)}</span></div>`;
            return;
        }

        if (count) count.textContent = String(state.discoverResults.length);
        list.innerHTML = '';
        if (!state.discoverResults.length) {
            list.innerHTML = '';
            if (empty) {
                empty.hidden = false;
                empty.innerHTML = `<i class="fas fa-search"></i><span>No matches for "${this.escapeHtml(state.discoverQuery)}".</span>`;
            }
            return;
        }
        if (empty) empty.hidden = true;

        state.discoverResults.forEach(r => {
            list.appendChild(this._makeDiscoverCard(r));
        });
    }

    _makeDiscoverCard(result) {
        const card = document.createElement('div');
        card.className = 'model-card model-card-discover';
        card.setAttribute('role', 'button');
        card.tabIndex = 0;

        const stars = result.stars != null
            ? `<span class="model-card-stars"><i class="fas fa-star"></i> ${result.stars.toLocaleString()}</span>`
            : '';
        const dl = result.downloads != null
            ? `<span class="model-card-dl"><i class="fas fa-download"></i> ${result.downloads.toLocaleString()}</span>`
            : '';
        const license = result.license
            ? `<span class="model-card-tier">${this.escapeHtml(result.license)}</span>`
            : '';
        const sourceBadge = result.source === 'hf'
            ? `<span class="model-card-badge model-card-badge-custom">Hugging Face</span>`
            : `<span class="model-card-badge model-card-badge-active">Curated</span>`;

        // v4 — quantization picker for HF results. Ollama supports
        // every common GGUF quant on a model with the `:Q4_K_M` /
        // `:Q5_K_M` / etc. suffix. We default to whatever the search
        // returned (typically Q4_K_M) and let the user switch.
        const isHf = result.source === 'hf';
        const quants = ['Q3_K_M', 'Q4_K_M', 'Q5_K_M', 'Q6_K', 'Q8_0', 'F16'];
        // Extract the quant suffix from the result tag (after the colon).
        const colonIdx = (result.tag || '').lastIndexOf(':');
        const baseTag = colonIdx >= 0 ? result.tag.slice(0, colonIdx) : result.tag;
        const currentQuant = colonIdx >= 0 ? result.tag.slice(colonIdx + 1) : 'Q4_K_M';
        const quantPicker = isHf
            ? `<select class="discover-quant-picker" title="Quantization">
                ${quants.map(q =>
                    `<option value="${q}"${q === currentQuant ? ' selected' : ''}>${q}</option>`
                ).join('')}
               </select>`
            : '';

        card.innerHTML = `
            <div class="model-card-row">
                <div class="model-card-head">
                    <span class="model-card-icon"><i class="fas fa-cloud"></i></span>
                    <span class="model-card-name">${this.escapeHtml(result.display_name || result.tag)}</span>
                    ${sourceBadge}
                </div>
                ${quantPicker}
                <button type="button" class="panel-action-btn model-discover-load" title="Load this model">
                    <i class="fas fa-cloud-download-alt"></i>
                    <span>Install</span>
                </button>
            </div>
            <div class="model-card-meta">
                <code class="model-card-tag">${this.escapeHtml(result.tag)}</code>
                ${license}
                ${stars}
                ${dl}
            </div>
            ${result.description
                ? `<div class="model-card-description">${this.escapeHtml(result.description)}</div>`
                : ''}
        `;

        // Live-update the displayed tag when quant changes.
        const qSelect = card.querySelector('.discover-quant-picker');
        const tagEl = card.querySelector('.model-card-tag');
        const resolveTag = () => {
            if (!qSelect) return result.tag;
            return `${baseTag}:${qSelect.value}`;
        };
        qSelect?.addEventListener('change', () => {
            if (tagEl) tagEl.textContent = resolveTag();
        });
        qSelect?.addEventListener('click', (e) => e.stopPropagation());

        const installBtn = card.querySelector('.model-discover-load');
        const loadFlow = (e) => {
            e?.stopPropagation?.();
            const tag = resolveTag();
            const pullInput = document.getElementById('customModelTag');
            if (pullInput) pullInput.value = tag;
            this._setActiveTab('pull');
            this._pullModel(tag).catch(err =>
                console.warn('discover install failed:', err));
        };
        installBtn?.addEventListener('click', loadFlow);
        card.addEventListener('click', (e) => {
            if (e.target.closest('.model-discover-load, .discover-quant-picker')) return;
            const tag = resolveTag();
            const pullInput = document.getElementById('customModelTag');
            if (pullInput) pullInput.value = tag;
            this._setActiveTab('pull');
            this._toast?.(`Loaded ${tag} — review and click Pull to install`);
        });
        card.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); loadFlow(e); }
        });
        return card;
    }

    /** Programmatically activate one of the picker tabs. */
    _setActiveTab(tab) {
        const root = document.getElementById('modelPicker');
        if (!root) return;
        const btn = root.querySelector(`.model-tab[data-tab="${tab}"]`);
        if (btn) btn.click();
    }

    // ── v3 — Test generation ───────────────────────────────────────────

    /** POST /api/models/test — streams a tiny generation into ``outEl``. */
    async _testGenerate(tag, prompt, profile, outEl) {
        const body = {
            model_tag: tag,
            prompt: prompt || 'Reply in one short sentence.',
            profile: profile && Object.keys(profile).length ? profile : null,
            max_tokens: 256,
        };
        const resp = await this._authFetch('/api/models/test', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body),
        });
        if (!resp.ok || !resp.body) {
            throw new Error(`test start failed (${resp.status})`);
        }
        if (outEl) {
            outEl.textContent = '';
            outEl.classList.remove('adv-test-error');
            outEl.classList.add('adv-test-streaming');
        }
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let acc = '';
        while (true) {
            const {done, value} = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, {stream: true});
            let idx;
            while ((idx = buffer.indexOf('\n\n')) !== -1) {
                const chunk = buffer.slice(0, idx);
                buffer = buffer.slice(idx + 2);
                const dataLine = chunk.split('\n').find(l => l.startsWith('data:'));
                if (!dataLine) continue;
                let evt;
                try { evt = JSON.parse(dataLine.slice(5).trim()); } catch (_) { continue; }
                if (evt.type === 'test_chunk' && evt.delta) {
                    acc += evt.delta;
                    if (outEl) {
                        // Render text + (when present) live tok/s overlay.
                        outEl.textContent = acc;
                        if (evt.tokens_per_second != null) {
                            const tps = document.createElement('span');
                            tps.className = 'adv-test-tps';
                            tps.textContent = ` ${evt.tokens_per_second} tok/s`;
                            outEl.appendChild(tps);
                        }
                    }
                } else if (evt.type === 'test_done') {
                    if (outEl) {
                        outEl.classList.remove('adv-test-streaming');
                        const meta = document.createElement('div');
                        meta.className = 'adv-test-meta';
                        const tps = evt.tokens_per_second
                            ? ` · ${evt.tokens_per_second} tok/s`
                            : '';
                        meta.textContent = `${evt.tokens || 0} tokens · ${evt.elapsed_ms || 0} ms${tps}`;
                        outEl.appendChild(meta);
                    }
                } else if (evt.type === 'test_error') {
                    if (outEl) {
                        outEl.classList.remove('adv-test-streaming');
                        outEl.classList.add('adv-test-error');
                        outEl.textContent = `Failed: ${evt.error || 'unknown'}`;
                    }
                }
            }
        }
        return acc;
    }

    // ── v3 — Routing persistence ───────────────────────────────────────

    async _refreshRouting() {
        try {
            const resp = await this._authFetch('/api/models/routing');
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const data = await resp.json();
            const state = this._modelState();
            state.routing = data || {};
            state.strategy = data?.strategy || 'single';
            state.hardwarePref = data?.hardware_pref || 'auto';
            if (typeof data?.gpu_layers === 'number') {
                state.gpuLayers = data.gpu_layers;
            }
            this._reflectRoutingToUI();
        } catch (err) {
            console.warn('routing fetch failed:', err);
        }
    }

    /** Reflect server-state into the UI controls. Idempotent. */
    _reflectRoutingToUI() {
        const state = this._modelState();
        const root = document.getElementById('modelPicker');
        if (!root) return;
        // Strategy tab.
        root.querySelectorAll('.model-mode-btn').forEach(btn => {
            const on = btn.dataset.strategy === state.strategy;
            btn.classList.toggle('active', on);
            btn.setAttribute('aria-selected', on ? 'true' : 'false');
        });
        this._renderStrategyHint();
        this._renderRoleMatrix();
        this._renderEnsembleConfig();
        // Hardware pref toggle.
        root.querySelectorAll('.hw-pref-btn').forEach(btn => {
            const on = btn.dataset.hw === state.hardwarePref;
            btn.classList.toggle('active', on);
            btn.setAttribute('aria-selected', on ? 'true' : 'false');
        });
        const slideRow = root.querySelector('#modelHwGpuLayersRow');
        if (slideRow) slideRow.hidden = (state.hardwarePref !== 'gpu_partial');
        const slider = root.querySelector('#modelHwGpuLayers');
        const sliderVal = root.querySelector('#modelHwGpuLayersValue');
        if (slider && state.gpuLayers != null) {
            slider.value = state.gpuLayers;
            if (sliderVal) sliderVal.textContent = String(state.gpuLayers);
        }
    }

    /** PUT the current routing state. Debounced so a flurry of slider
     *  drags doesn't spam the server. */
    async _saveRouting() {
        if (this._routingSaveTimer) clearTimeout(this._routingSaveTimer);
        this._routingSaveTimer = setTimeout(async () => {
            const state = this._modelState();
            const body = {
                strategy: state.strategy || 'single',
                hardware_pref: state.hardwarePref || 'auto',
                gpu_layers: state.gpuLayers,
                role_routes: state.routing?.role_routes || {},
                mode_routes: state.routing?.mode_routes || {},
                ensemble: state.routing?.ensemble || {},
                fallback_chain: state.routing?.fallback_chain || [],
            };
            try {
                const resp = await this._authFetch('/api/models/routing', {
                    method: 'PUT',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(body),
                });
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            } catch (err) {
                console.warn('routing save failed:', err);
            }
        }, 350);
    }

    // ── v4 — Smart recommendation banner ────────────────────────────────

    /** Run a debounced recommendation for the prompt currently in the
     *  active chat input. Called from chat input listeners. */
    async _refreshRecommendation(prompt) {
        const root = document.getElementById('modelPicker');
        if (!root) return;
        const state = this._modelState();
        if (state.recommendDismissed) return;
        const trimmed = (prompt || '').trim();
        if (trimmed.length < 12) {
            this._hideRecommendBanner();
            return;
        }
        // Debounce — only the latest call wins.
        const myToken = ++state.recommendQueryToken;
        // Fire after 500ms of typing inactivity.
        if (this._recommendDebounceTimer) clearTimeout(this._recommendDebounceTimer);
        this._recommendDebounceTimer = setTimeout(async () => {
            if (myToken !== state.recommendQueryToken) return;
            try {
                const resp = await this._authFetch('/api/models/recommend', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({prompt: trimmed, mode: state.scope}),
                });
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                if (myToken !== state.recommendQueryToken) return;
                state.recommendation = await resp.json();
                this._renderRecommendBanner();
            } catch (err) {
                console.warn('recommend failed:', err);
            }
        }, 500);
    }

    _renderRecommendBanner() {
        const banner = document.getElementById('modelRecommendBanner');
        const tagEl = document.getElementById('recommendTag');
        const reasonEl = document.getElementById('recommendReason');
        const applyBtn = document.getElementById('recommendApplyBtn');
        const dismissBtn = document.getElementById('recommendDismissBtn');
        if (!banner || !tagEl || !reasonEl) return;
        const state = this._modelState();
        const rec = state.recommendation;
        if (!rec || !rec.tag) {
            banner.hidden = true;
            return;
        }
        // Don't suggest the model that's already active.
        const activeTag = (state.preferences[state.scope]?.tag || '').toLowerCase();
        if (rec.tag.toLowerCase() === activeTag) {
            banner.hidden = true;
            return;
        }
        tagEl.textContent = rec.display_name || rec.tag;
        reasonEl.textContent = rec.reason || '';
        banner.hidden = false;
        if (applyBtn && !applyBtn.dataset.wired) {
            applyBtn.dataset.wired = '1';
            applyBtn.addEventListener('click', () => {
                this._savePreference(
                    rec.tag, 'ollama_registry',
                    rec.display_name || rec.tag,
                ).catch(err => console.warn(err));
                this._hideRecommendBanner();
            });
        }
        if (dismissBtn && !dismissBtn.dataset.wired) {
            dismissBtn.dataset.wired = '1';
            dismissBtn.addEventListener('click', () => {
                this._modelState().recommendDismissed = true;
                this._hideRecommendBanner();
            });
        }
    }

    _hideRecommendBanner() {
        const banner = document.getElementById('modelRecommendBanner');
        if (banner) banner.hidden = true;
    }

    /** Headers used by XHR upload (mirrors _authFetch behaviour). */
    _authHeaders() {
        const headers = {};
        try {
            const token = localStorage.getItem('amor.accessToken');
            if (token) headers['Authorization'] = `Bearer ${token}`;
            const cid = localStorage.getItem('amor.clientId');
            if (cid) headers['X-Client-Id'] = cid;
        } catch (_) {}
        return headers;
    }

    // ── localStorage active-code persistence (mirrors active-research) ──

    _persistActiveCode(sessionId) {
        try {
            localStorage.setItem('amor.activeCode', JSON.stringify({
                sessionId, ts: Date.now(),
            }));
        } catch (_) {}
    }

    _clearActiveCode() {
        try { localStorage.removeItem('amor.activeCode'); } catch (_) {}
    }

    _readActiveCode() {
        try {
            const raw = localStorage.getItem('amor.activeCode');
            if (!raw) return null;
            const saved = JSON.parse(raw);
            if (!saved?.sessionId) return null;
            if (Date.now() - (saved.ts || 0) > 4 * 3600 * 1000) {
                this._clearActiveCode();
                return null;
            }
            return saved;
        } catch (_) { return null; }
    }

    /**
     * Resume an in-flight Code Intelligence session after page reload.
     * Mirrors `_resumeActiveResearchIfAny`. Called once from
     * DOMContentLoaded by app.js.
     */
    async _resumeActiveCodeIfAny() {
        const saved = this._readActiveCode();
        if (!saved) return;
        try {
            const resp = await this._authFetch(
                `/api/code/${encodeURIComponent(saved.sessionId)}/status`
            );
            if (resp.status === 404) {
                this._clearActiveCode();
                return;
            }
            if (!resp.ok) return;
            const snap = await resp.json();
            if (snap.status === 'completed' || snap.status === 'failed') {
                this._clearActiveCode();
                return;
            }
            if (typeof CodeView !== 'function') return;
            const view = CodeView.fromSnapshot({
                prompt: snap.prompt || '',
                effort: snap.effort || 'medium',
                language: snap.language || null,
                phases: snap.phases || [],
                models_used: snap.models_used || {},
                code: snap.code,
                tests: snap.tests,
                execution_results: snap.execution_results || [],
                static_analysis: snap.static_analysis,
                review: snap.review,
                debug_iterations: snap.debug_iterations,
                status: snap.status,
            });
            this._mountCodeCard(view);
            this._currentCodeBackendId = saved.sessionId;
            try {
                try { await this._streamCode(saved.sessionId, view); }
                catch (_) { await this._pollCode(saved.sessionId, view); }
            } finally {
                this._clearActiveCode();
            }
        } catch (e) {
            console.warn('resume-code failed:', e);
        }
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
            // Try the rich card paths FIRST — `format` may have been written
            // either as the canonical "research" / "thinking" or (for older
            // rows from a previous backend version) as "text" / "html" with
            // the snapshot still tucked into `extras`. Treat the presence
            // of `extras.research` / `extras.thinking` as the real signal.
            const looksLikeResearch =
                (msg.format === 'research' || (msg.extras?.research?.report_markdown ||
                    Array.isArray(msg.extras?.research?.phases))) &&
                msg.extras?.research && typeof ResearchView === 'function';
            if (looksLikeResearch) {
                try {
                    const view = ResearchView.fromSnapshot(msg.extras.research);
                    this._mountResearchCard(view);
                    return;
                } catch (e) {
                    console.warn('Failed to restore research snapshot:', e);
                }
            }

            const looksLikeThinking =
                (msg.format === 'thinking' || msg.extras?.thinking?.session ||
                    msg.extras?.thinking?.deliverable_markdown) &&
                msg.extras?.thinking && typeof ThinkingView === 'function';
            if (looksLikeThinking) {
                try {
                    const view = ThinkingView.fromSnapshot(msg.extras.thinking);
                    this._mountThinkingCard(view);
                    return;
                } catch (e) {
                    console.warn('Failed to restore thinking snapshot:', e);
                }
            }

            const looksLikeCode =
                (msg.format === 'code' || msg.extras?.code?.code ||
                    msg.extras?.code?.execution_results) &&
                msg.extras?.code && typeof CodeView === 'function';
            if (looksLikeCode) {
                try {
                    const view = CodeView.fromSnapshot(msg.extras.code);
                    this._mountCodeCard(view);
                    return;
                } catch (e) {
                    console.warn('Failed to restore code snapshot:', e);
                }
            }

            // Defensive markdown rendering — when the only thing we have is
            // raw markdown text in `content`, run it through the renderer
            // exposed by research-view.js so headings / bold / lists / code
            // come out properly instead of as literal "# ", "**", "[5]"
            // characters in the chat. Falls back to plain escaped text if
            // the renderer hasn't loaded yet.
            const looksLikeMarkdown = (msg.format === 'markdown') ||
                (typeof msg.content === 'string' &&
                 /(^|\n)\s*(#{1,6}\s|[-*+]\s|\d+\.\s|>\s)/.test(msg.content || '')) ||
                (typeof msg.content === 'string' && /\*\*[^*]+\*\*/.test(msg.content || ''));
            if (msg.role === 'assistant' && looksLikeMarkdown &&
                typeof window.__renderResearchMarkdown === 'function') {
                try {
                    const html = window.__renderResearchMarkdown(msg.content || '', new Set());
                    this.addMessage(msg.role, `<div class="research-result"><div class="research-markdown-restored">${html}</div></div>`,
                        msg.aiType, msg.extras || {});
                    return;
                } catch (e) {
                    console.warn('Failed to render restored markdown:', e);
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
            try { window.chatController?._resumeActiveCodeIfAny?.(); }
            catch (e) { console.warn('resume-code bootstrap failed:', e); }
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
