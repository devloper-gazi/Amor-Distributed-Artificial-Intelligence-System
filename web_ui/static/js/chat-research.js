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

        // Expose open/close handlers for settings panel (wired during init)
        this.openResearchSettingsPanel = null;
        this.closeResearchSettingsPanel = null;

        this.init();
    }

    init() {
        // Event listeners
        this.sendButton?.addEventListener('click', () => this.sendMessage());
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
                    extras: msg.extras || {}
                })
            });
            if (!response.ok) {
                console.warn('Failed to persist message:', response.status, response.statusText);
            }
        } catch (e) {
            console.warn('Failed to persist message:', e);
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

        // Add user message
        this.addMessage('user', message);
        const userMsg = { role: 'user', content: message, format: 'text' };
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
            const friendly = this.formatErrorMessage(error);
            this.addMessage('assistant', friendly, 'error');
            // Persist error bubble so page reload keeps the context.
            const errMsg = {
                role: 'assistant',
                content: friendly,
                aiType: 'error',
                format: 'text',
                extras: { error: error.message || String(error) },
            };
            this.messageHistory.push(errMsg);
            try { await this.persistChatMessage(errMsg); } catch (_) {}
        } finally {
            this.isProcessing = false;
            if (this.sendButton) this.sendButton.disabled = false;
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
                history: this.messageHistory
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
                body: JSON.stringify(requestBody)
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
            }),
        });

        if (!startResponse.ok) {
            let detail = '';
            try { detail = (await startResponse.json()).detail || ''; } catch (_) {}
            const extra = detail ? ` - ${detail}` : '';
            throw new Error(`Failed to start research (${startResponse.status} ${startResponse.statusText})${extra}`);
        }

        const { session_id } = await startResponse.json();
        this.currentSessionId = session_id;

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
                }),
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
        return new Promise((resolve, reject) => {
            let es;
            try {
                const token = window.amorAuth?.accessToken || '';
                const qs = token ? `?access_token=${encodeURIComponent(token)}` : '';
                es = new EventSource(`/api/thinking/${sessionId}/events${qs}`);
            } catch (e) {
                reject(e);
                return;
            }
            let completed = false;
            const finish = (err) => {
                if (completed) return;
                completed = true;
                try { es.close(); } catch (_) {}
                if (err) reject(err); else resolve();
            };
            es.onmessage = (e) => {
                if (!e.data) return;
                let evt;
                try { evt = JSON.parse(e.data); } catch (_) { return; }
                view.handleEvent(evt);
                if (evt.type === 'done') finish();
                if (evt.type === 'error') finish(new Error(evt.message || 'Thinking failed'));
            };
            es.onerror = () => {
                if (completed) return;
                finish(new Error('SSE connection error'));
            };
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
        return new Promise((resolve, reject) => {
            let es;
            try {
                // EventSource can't send custom headers, so we piggy-back the
                // access token on the URL. The backend's get_current_user
                // dependency accepts an `access_token` query param as a
                // fallback for exactly this case.
                const token = window.amorAuth?.accessToken || '';
                const qs = token ? `?access_token=${encodeURIComponent(token)}` : '';
                es = new EventSource(`/api/local-ai/research/${sessionId}/events${qs}`);
            } catch (e) {
                reject(e);
                return;
            }
            let completed = false;
            const finish = (err) => {
                if (completed) return;
                completed = true;
                try { es.close(); } catch (_) {}
                if (err) reject(err); else resolve();
            };
            es.onmessage = (e) => {
                if (!e.data) return;
                let evt;
                try { evt = JSON.parse(e.data); } catch (_) { return; }
                view.handleEvent(evt);
                if (evt.type === 'done') finish();
                if (evt.type === 'error') finish(new Error(evt.message || 'Research failed'));
            };
            es.onerror = () => {
                // If we've already received a 'done', just swallow the error.
                if (completed) return;
                finish(new Error('SSE connection error'));
            };
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
    }
});
