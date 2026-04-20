/**
 * Amor — user chip (avatar + dropdown) rendered in the top bar.
 *
 * Listens to `amor:auth-changed` to rerender, and provides a simple dropdown
 * menu (account info / log out of this device / log out everywhere).
 */
(function () {
    'use strict';

    function initials(name) {
        if (!name) return 'U';
        const trimmed = String(name).trim();
        if (!trimmed) return 'U';
        const parts = trimmed.split(/\s+/).filter(Boolean);
        if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
        return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
    }

    function ensureHost() {
        let host = document.getElementById('amorUserChipHost');
        if (host) return host;

        // Inject inside the top bar, next to the right actions if present.
        const topBar = document.querySelector('.top-bar-minimal');
        if (!topBar) return null;

        host = document.createElement('div');
        host.id = 'amorUserChipHost';
        host.className = 'amor-user-chip-host';
        host.style.marginLeft = 'auto';
        host.style.paddingRight = '16px';
        topBar.appendChild(host);
        return host;
    }

    function render(user) {
        const host = ensureHost();
        if (!host) return;
        if (!user) {
            host.innerHTML = '';
            return;
        }

        const label = user.display_name || user.username || 'Account';
        const avatarInitials = initials(user.display_name || user.username);
        const secondary = user.email || (user.display_name ? `@${user.username}` : '');

        host.innerHTML = `
            <button type="button" class="amor-user-chip" id="amorUserChipBtn" aria-haspopup="menu" aria-expanded="false">
                <span class="avatar">${escapeHtml(avatarInitials)}</span>
                <span class="name">${escapeHtml(label)}</span>
                <i class="fas fa-chevron-down" style="font-size:10px;opacity:.55;margin-left:2px"></i>
            </button>
            <div class="amor-user-menu" id="amorUserMenu" role="menu" aria-hidden="true">
                <div class="menu-header">
                    <div class="primary">${escapeHtml(user.display_name || user.username || '')}</div>
                    <div class="secondary">${escapeHtml(secondary)}</div>
                </div>
                <button type="button" class="menu-item" data-action="logout" role="menuitem">
                    <i class="fas fa-right-from-bracket"></i><span>Log out of this device</span>
                </button>
                <button type="button" class="menu-item danger" data-action="logout-all" role="menuitem">
                    <i class="fas fa-shield-halved"></i><span>Sign out everywhere</span>
                </button>
            </div>
        `;

        const btn = host.querySelector('#amorUserChipBtn');
        const menu = host.querySelector('#amorUserMenu');

        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            const open = menu.classList.toggle('is-open');
            btn.setAttribute('aria-expanded', open ? 'true' : 'false');
            menu.setAttribute('aria-hidden', open ? 'false' : 'true');
        });

        document.addEventListener('click', () => {
            if (menu.classList.contains('is-open')) {
                menu.classList.remove('is-open');
                btn.setAttribute('aria-expanded', 'false');
                menu.setAttribute('aria-hidden', 'true');
            }
        });

        menu.addEventListener('click', async (e) => {
            const action = e.target?.closest('[data-action]')?.getAttribute('data-action');
            if (!action) return;
            e.stopPropagation();
            menu.classList.remove('is-open');
            btn.setAttribute('aria-expanded', 'false');
            if (action === 'logout') {
                await window.amorAuth.logout();
            } else if (action === 'logout-all') {
                try {
                    const res = await window.amorAuth.fetch('/api/auth/logout-all', { method: 'POST' });
                    if (res.status === 204) {
                        await window.amorAuth.logout();
                    }
                } catch (_e) {
                    await window.amorAuth.logout();
                }
            }
        });
    }

    function escapeHtml(s) {
        return String(s ?? '').replace(/[&<>"']/g, c => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
        }[c]));
    }

    document.addEventListener('amor:auth-changed', (e) => render(e.detail?.user || null));
    document.addEventListener('DOMContentLoaded', () => {
        if (window.amorAuth?.user) render(window.amorAuth.user);
    });
})();
