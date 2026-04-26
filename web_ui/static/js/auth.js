/**
 * Amor — client-side authentication layer.
 *
 * Two distinct views (sign-in / create-account) that swap with a short slide
 * transition; no tabs, no email fields — just a username + password flow that
 * matches the rest of the monochrome UI.
 *
 * Responsibilities
 *  - Track the current user + access token in memory (never in localStorage,
 *    to avoid XSS exfiltration; the refresh token lives in an httpOnly cookie
 *    scoped to /api/auth).
 *  - Silent refresh on boot + before expiry; 401 retry with a fresh token.
 *  - Block the rest of the app until authentication succeeds.
 *  - Expose `window.amorAuth` for app.js, and override `window.getChatHeaders`
 *    so every API call carries `Authorization: Bearer <token>`.
 */

(function () {
    'use strict';

    const LS_LAST_USER = 'amor_last_user_hint';

    const state = {
        user: null,
        accessToken: null,
        tokenExpiresAt: 0, // epoch seconds
        refreshTimer: null,
        ready: false,
        _readyResolvers: [],
    };

    // ------------------------------------------------------------------ API

    async function rawFetch(path, init = {}) {
        const headers = Object.assign(
            { 'Content-Type': 'application/json' },
            init.headers || {}
        );
        return fetch(path, { credentials: 'include', ...init, headers });
    }

    async function attemptRefresh() {
        try {
            const res = await rawFetch('/api/auth/refresh', { method: 'POST' });
            if (!res.ok) return null;
            const data = await res.json();
            applyTokens(data);
            return data;
        } catch (_err) {
            return null;
        }
    }

    function applyTokens(data) {
        state.accessToken = data.access_token;
        state.tokenExpiresAt = Math.floor(Date.now() / 1000) + (data.expires_in || 900);
        state.user = data.user;
        try { localStorage.setItem(LS_LAST_USER, data.user.username || ''); } catch (_e) {}
        scheduleRefresh();
        document.dispatchEvent(new CustomEvent('amor:auth-changed', { detail: { user: data.user } }));
    }

    function clearTokens() {
        state.accessToken = null;
        state.user = null;
        state.tokenExpiresAt = 0;
        if (state.refreshTimer) {
            clearTimeout(state.refreshTimer);
            state.refreshTimer = null;
        }
        document.dispatchEvent(new CustomEvent('amor:auth-changed', { detail: { user: null } }));
    }

    function scheduleRefresh() {
        if (state.refreshTimer) clearTimeout(state.refreshTimer);
        const nowSec = Math.floor(Date.now() / 1000);
        const skew = 30;
        const delay = Math.max(15, state.tokenExpiresAt - nowSec - skew);
        state.refreshTimer = setTimeout(() => { attemptRefresh(); }, delay * 1000);
    }

    async function register({ username, password, display_name }) {
        const body = { username, password };
        if (display_name) body.display_name = display_name;
        const res = await rawFetch('/api/auth/register', {
            method: 'POST',
            body: JSON.stringify(body),
        });
        if (!res.ok) throw new AuthError(await extractErrorDetail(res), res.status);
        applyTokens(await res.json());
        return state.user;
    }

    async function login({ identifier, password }) {
        const res = await rawFetch('/api/auth/login', {
            method: 'POST',
            body: JSON.stringify({ identifier, password }),
        });
        if (!res.ok) throw new AuthError(await extractErrorDetail(res), res.status);
        applyTokens(await res.json());
        return state.user;
    }

    async function logout() {
        try { await rawFetch('/api/auth/logout', { method: 'POST' }); } catch (_e) {}
        clearTokens();
        showAuthOverlay('login');
    }

    function authHeaders() {
        return state.accessToken ? { 'Authorization': `Bearer ${state.accessToken}` } : {};
    }

    async function authFetch(path, init = {}) {
        if (!state.accessToken || Math.floor(Date.now() / 1000) >= state.tokenExpiresAt - 2) {
            await attemptRefresh();
        }
        const doFetch = async () => {
            // Merge BOTH X-Client-Id (from getChatHeaders) AND Authorization
            // (from authHeaders). The legacy authFetch only added the JWT,
            // which made every chat-store route 400-out with
            // "Missing X-Client-Id header" — that broke auto-title,
            // query-records, active-query, /messages, etc. Caller-supplied
            // headers win last so explicit Content-Type etc. is preserved.
            const chatHdrs = (typeof window.getChatHeaders === 'function')
                ? window.getChatHeaders() : {};
            const headers = Object.assign(
                {},
                chatHdrs,
                authHeaders(),
                init.headers || {},
            );
            return fetch(path, { credentials: 'include', ...init, headers });
        };
        let res = await doFetch();
        if (res.status === 401) {
            const refreshed = await attemptRefresh();
            if (refreshed) {
                res = await doFetch();
            } else {
                clearTokens();
                showAuthOverlay('login');
            }
        }
        return res;
    }

    async function extractErrorDetail(res) {
        try {
            const data = await res.json();
            if (typeof data?.detail === 'string') return data.detail;
            if (Array.isArray(data?.detail)) {
                return data.detail.map(e => e.msg || JSON.stringify(e)).join('; ');
            }
            if (data?.detail) return JSON.stringify(data.detail);
        } catch (_e) {}
        return `Request failed (${res.status})`;
    }

    class AuthError extends Error {
        constructor(message, status) {
            super(message); this.name = 'AuthError'; this.status = status;
        }
    }

    // ------------------------------------------------------------------ UI

    const OVERLAY_HTML = `
        <div class="amor-auth-overlay" id="amorAuthOverlay" role="dialog" aria-modal="true" aria-labelledby="amorAuthTitle">
            <div class="amor-auth-card" data-mode="login">
                <header class="amor-auth-head">
                    <div class="amor-auth-logo" aria-hidden="true"><i class="fas fa-robot"></i></div>
                    <div class="amor-auth-brand-text">
                        <div class="amor-auth-brand-name">Amor</div>
                        <div class="amor-auth-brand-tag">AI Research Assistant</div>
                    </div>
                </header>

                <!-- Sign-in view -->
                <section class="amor-auth-view" data-view="login" aria-labelledby="amorAuthTitleLogin">
                    <h2 id="amorAuthTitleLogin" class="amor-auth-title">Welcome back</h2>
                    <p class="amor-auth-sub">Sign in to continue your research.</p>

                    <form class="amor-auth-form" data-auth-form="login" novalidate autocomplete="on">
                        <label class="amor-auth-field">
                            <span>Username</span>
                            <input type="text" name="identifier" autocomplete="username" autocapitalize="none" autocorrect="off" spellcheck="false" required>
                        </label>
                        <label class="amor-auth-field">
                            <span>Password</span>
                            <input type="password" name="password" autocomplete="current-password" required>
                        </label>
                        <button type="submit" class="amor-auth-submit">
                            <span class="label">Sign in</span>
                            <span class="spinner" aria-hidden="true"></span>
                        </button>
                        <div class="amor-auth-error" role="alert" hidden></div>
                    </form>

                    <div class="amor-auth-switch">
                        <span class="amor-auth-switch-prompt">Don't have an account?</span>
                        <button type="button" class="amor-auth-switch-btn" data-goto="register">Create one</button>
                    </div>
                </section>

                <!-- Register view -->
                <section class="amor-auth-view" data-view="register" aria-labelledby="amorAuthTitleRegister" hidden>
                    <h2 id="amorAuthTitleRegister" class="amor-auth-title">Create your account</h2>
                    <p class="amor-auth-sub">Your history stays tied to your profile — nowhere else.</p>

                    <form class="amor-auth-form" data-auth-form="register" novalidate autocomplete="on">
                        <label class="amor-auth-field">
                            <span>Username</span>
                            <input type="text" name="username" autocomplete="username" autocapitalize="none" autocorrect="off" spellcheck="false" minlength="3" maxlength="64" pattern="[A-Za-z0-9_\\-]{3,64}" required>
                            <small class="amor-auth-hint">3–64 characters · letters, numbers, underscore, hyphen.</small>
                        </label>
                        <label class="amor-auth-field">
                            <span>Display name <em>optional</em></span>
                            <input type="text" name="display_name" maxlength="128" autocomplete="nickname">
                        </label>
                        <label class="amor-auth-field">
                            <span>Password</span>
                            <input type="password" name="password" autocomplete="new-password" minlength="10" required>
                            <small class="amor-auth-hint">At least 10 characters with upper, lower, and a digit.</small>
                        </label>
                        <button type="submit" class="amor-auth-submit">
                            <span class="label">Create account</span>
                            <span class="spinner" aria-hidden="true"></span>
                        </button>
                        <div class="amor-auth-error" role="alert" hidden></div>
                    </form>

                    <div class="amor-auth-switch">
                        <span class="amor-auth-switch-prompt">Already have an account?</span>
                        <button type="button" class="amor-auth-switch-btn" data-goto="login">Sign in</button>
                    </div>
                </section>

                <footer class="amor-auth-footer">
                    Runs entirely on your server — no API keys required.
                </footer>
            </div>
        </div>
    `;

    function injectOverlay() {
        if (document.getElementById('amorAuthOverlay')) return;
        const tpl = document.createElement('div');
        tpl.innerHTML = OVERLAY_HTML.trim();
        document.body.appendChild(tpl.firstElementChild);
        wireOverlay();
    }

    function wireOverlay() {
        const overlay = document.getElementById('amorAuthOverlay');
        if (!overlay) return;

        const lastHint = (() => { try { return localStorage.getItem(LS_LAST_USER) || ''; } catch (_e) { return ''; } })();
        if (lastHint) {
            const idInput = overlay.querySelector('form[data-auth-form="login"] input[name="identifier"]');
            if (idInput) idInput.value = lastHint;
        }

        overlay.querySelectorAll('[data-goto]').forEach(btn => {
            btn.addEventListener('click', () => {
                switchView(btn.getAttribute('data-goto'));
            });
        });

        overlay.querySelector('form[data-auth-form="login"]').addEventListener('submit', async (e) => {
            e.preventDefault();
            await submitForm(e.currentTarget, async (fd) => login({
                identifier: fd.get('identifier')?.trim() || '',
                password: fd.get('password') || '',
            }));
        });

        overlay.querySelector('form[data-auth-form="register"]').addEventListener('submit', async (e) => {
            e.preventDefault();
            await submitForm(e.currentTarget, async (fd) => register({
                username: fd.get('username')?.trim() || '',
                password: fd.get('password') || '',
                display_name: fd.get('display_name')?.trim() || '',
            }));
        });
    }

    function switchView(next) {
        const card = document.querySelector('.amor-auth-card');
        const overlay = document.getElementById('amorAuthOverlay');
        if (!card || !overlay) return;

        const current = card.getAttribute('data-mode');
        if (current === next) return;

        const direction = next === 'register' ? 'forward' : 'back';
        card.setAttribute('data-transition', direction);

        // Run the transition on the next frame so CSS picks up the state change.
        requestAnimationFrame(() => {
            card.setAttribute('data-mode', next);

            overlay.querySelectorAll('.amor-auth-view').forEach(v => {
                v.hidden = v.getAttribute('data-view') !== next;
            });

            // Reset any lingering errors on the new form and focus first field.
            const activeForm = overlay.querySelector(`form[data-auth-form="${next}"]`);
            activeForm?.querySelectorAll('.amor-auth-error').forEach(el => {
                el.hidden = true;
                el.textContent = '';
            });
            const firstInput = activeForm?.querySelector('input');
            setTimeout(() => firstInput?.focus(), 220);

            setTimeout(() => card.removeAttribute('data-transition'), 320);
        });
    }

    async function submitForm(form, submitFn) {
        const submitBtn = form.querySelector('.amor-auth-submit');
        const errBox = form.querySelector('.amor-auth-error');
        errBox.hidden = true;
        errBox.textContent = '';
        submitBtn.classList.add('is-loading');
        submitBtn.disabled = true;
        try {
            const fd = new FormData(form);
            await submitFn(fd);
            hideAuthOverlay();
            resolveReady();
        } catch (err) {
            errBox.textContent = err?.message || 'Something went wrong';
            errBox.hidden = false;
        } finally {
            submitBtn.classList.remove('is-loading');
            submitBtn.disabled = false;
        }
    }

    function showAuthOverlay(mode = 'login') {
        injectOverlay();
        const overlay = document.getElementById('amorAuthOverlay');
        const card = overlay?.querySelector('.amor-auth-card');
        if (!overlay || !card) return;

        card.setAttribute('data-mode', mode);
        overlay.querySelectorAll('.amor-auth-view').forEach(v => {
            v.hidden = v.getAttribute('data-view') !== mode;
        });

        overlay.classList.add('is-visible');
        document.body.classList.add('amor-auth-blocking');

        const firstInput = overlay.querySelector(`form[data-auth-form="${mode}"] input`);
        setTimeout(() => firstInput?.focus(), 80);
    }

    function hideAuthOverlay() {
        const overlay = document.getElementById('amorAuthOverlay');
        if (!overlay) return;
        overlay.classList.remove('is-visible');
        document.body.classList.remove('amor-auth-blocking');
        overlay.querySelectorAll('.amor-auth-error').forEach(e => { e.hidden = true; e.textContent = ''; });
    }

    function resolveReady() {
        state.ready = true;
        document.dispatchEvent(new CustomEvent('amor:authenticated', { detail: { user: state.user } }));
        const resolvers = state._readyResolvers.splice(0);
        resolvers.forEach(r => { try { r(); } catch (_e) {} });
    }

    function ready() {
        if (state.ready) return Promise.resolve();
        return new Promise((resolve) => state._readyResolvers.push(resolve));
    }

    // ------------------------------------------------------------------ bootstrap

    async function bootstrap() {
        injectOverlay();
        const refreshed = await attemptRefresh();
        if (refreshed) {
            hideAuthOverlay();
            resolveReady();
        } else {
            showAuthOverlay('login');
        }
    }

    // Public API
    window.amorAuth = {
        get user() { return state.user; },
        get accessToken() { return state.accessToken; },
        isAuthenticated() { return !!state.accessToken; },
        ready,
        login,
        register,
        logout,
        refresh: attemptRefresh,
        authHeaders,
        fetch: authFetch,
        showLogin: () => showAuthOverlay('login'),
        showRegister: () => showAuthOverlay('register'),
    };

    // Every outgoing API call carries the JWT through this shared accessor.
    window.getChatHeaders = function () {
        const headers = {};
        try {
            const clientId = localStorage.getItem('chat_client_id');
            if (clientId) headers['X-Client-Id'] = clientId;
        } catch (_e) {}
        if (state.accessToken) headers['Authorization'] = `Bearer ${state.accessToken}`;
        return headers;
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', bootstrap, { once: true });
    } else {
        bootstrap();
    }
})();
