// MCP Plan File Manager - JavaScript

// --- page lifecycle ---------------------------------------------------------
//
// Navigation swaps the page shell in place instead of reloading the document
// (see "boosted navigation" at the foot of this file), so "the page is ready"
// happens many times per visit, not once. Anything that wires up elements has
// to run again each time; anything that listens on `document` must not.
//
//   onPage(fn)  - runs now-ish and after every swap. Use for element wiring.
//   once(key, fn) - runs the first time only. Use for document-level listeners.
//
const _pageInits = [];
const _done = new Set();

function onPage(fn) {
    _pageInits.push(fn);
}

function once(key, fn) {
    if (_done.has(key)) return;
    _done.add(key);
    fn();
}

function runPageInits() {
    _pageInits.forEach(function (fn) {
        // One broken initialiser must not stop the rest of the page working.
        try { fn(); } catch (e) { console.error('page init failed', e); }
    });
}

document.addEventListener('DOMContentLoaded', runPageInits);
document.addEventListener('flanner:page', runPageInits);

// Auto-hide flash messages after 5 seconds, and give each one a dismiss
// button.
//
// Scoped to the flash region rather than to `.alert` anywhere on the page.
// It used to match every `.alert`, which included the sidebar's attention
// badge: the badge grew a stray close button and then faded itself away.
// Marking the region is what makes that class of collision impossible.
onPage(function () {
    const alerts = document.querySelectorAll('[data-flash] .alert');
    alerts.forEach(alert => {
        setTimeout(() => {
            alert.style.transition = 'opacity 0.3s ease-out';
            alert.style.opacity = '0';
            setTimeout(() => {
                alert.remove();
            }, 300);
        }, 5000);
    });

    // Add close button to alerts. Guarded, because a navigation runs this
    // again over markup that may already carry one.
    alerts.forEach(alert => {
        if (alert.querySelector('[data-dismiss]')) return;
        const closeBtn = document.createElement('button');
        closeBtn.setAttribute('data-dismiss', '');
        closeBtn.setAttribute('aria-label', 'Dismiss');
        closeBtn.type = 'button';
        closeBtn.innerHTML = '×';
        closeBtn.style.cssText = `
            margin-left: auto;
            background: none;
            border: none;
            color: inherit;
            font-size: 1.5rem;
            cursor: pointer;
            padding: 0;
            width: 1.5rem;
            height: 1.5rem;
            display: flex;
            align-items: center;
            justify-content: center;
        `;
        closeBtn.onclick = () => {
            alert.style.opacity = '0';
            setTimeout(() => alert.remove(), 300);
        };
        alert.appendChild(closeBtn);
    });
});

// Textarea auto-resize
onPage(function () {
    const textareas = document.querySelectorAll('textarea.form-control');
    textareas.forEach(textarea => {
        textarea.addEventListener('input', function() {
            this.style.height = 'auto';
            this.style.height = (this.scrollHeight) + 'px';
        });
    });
});

// Markdown editor tab support
onPage(function () {
    const editors = document.querySelectorAll('.markdown-editor');
    editors.forEach(editor => {
        editor.addEventListener('keydown', function(e) {
            if (e.key === 'Tab') {
                e.preventDefault();
                const start = this.selectionStart;
                const end = this.selectionEnd;
                this.value = this.value.substring(0, start) + '    ' + this.value.substring(end);
                this.selectionStart = this.selectionEnd = start + 4;
            }
        });
    });
});

// Reading settings popover (plan viewer). Presentation only: sets data-* on
// <html>, which drives CSS variables, and persists to localStorage.
onPage(function () {
    const toggle = document.getElementById('reading-toggle');
    const panel = document.getElementById('reading-panel');
    if (!toggle || !panel) return;

    const KEY = 'flanner.reading';
    const DEFAULTS = { preset: 'default', font: 'mono', size: 'm', measure: 'comfortable' };
    // A preset is a shortcut for the other three, which is the only thing it
    // could honestly be: it had no styles of its own and did nothing at all.
    const PRESETS = {
        book:    { font: 'serif',  size: 'l',  measure: 'narrow' },
        plain:   { font: 'system', size: 'm',  measure: 'comfortable' },
        default: { font: 'mono',   size: 'm',  measure: 'comfortable' },
    };

    function load() {
        try {
            return Object.assign({}, DEFAULTS, JSON.parse(localStorage.getItem(KEY) || '{}'));
        } catch (e) {
            return Object.assign({}, DEFAULTS);
        }
    }

    function apply(state) {
        const d = document.documentElement;
        d.dataset.readingPreset = state.preset;
        d.dataset.readingFont = state.font;
        d.dataset.readingSize = state.size;
        d.dataset.readingMeasure = state.measure;
        panel.querySelectorAll('[data-reading]').forEach(function (seg) {
            const key = seg.getAttribute('data-reading');
            seg.querySelectorAll('button').forEach(function (b) {
                b.setAttribute('aria-pressed', String(b.getAttribute('data-value') === state[key]));
            });
        });
    }

    let state = load();
    apply(state);

    toggle.addEventListener('click', function () {
        const willOpen = panel.hasAttribute('hidden');
        if (willOpen) { panel.removeAttribute('hidden'); } else { panel.setAttribute('hidden', ''); }
        toggle.setAttribute('aria-expanded', String(willOpen));
    });

    panel.querySelectorAll('[data-reading] button').forEach(function (b) {
        b.addEventListener('click', function () {
            const key = b.parentElement.getAttribute('data-reading');
            const value = b.getAttribute('data-value');
            state[key] = value;
            if (key === 'preset' && PRESETS[value]) Object.assign(state, PRESETS[value]);
            // Night is about the page, not the typeface, so it drives the
            // theme the top bar also controls rather than inventing a second
            // dark mode that only applies to one column.
            if (key === 'preset' && value === 'night') applyTheme('dark');
            // Choosing a face or a size by hand is no longer whichever preset
            // was named, so the label stops claiming otherwise.
            if (key !== 'preset' && state.preset !== 'custom') state.preset = 'custom';
            try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) {}
            apply(state);
        });
    });

    // Registered once and re-resolving the elements each time: a navigation
    // replaces the panel, so a listener closing over today's node would be
    // holding a detached element by the next page.
    once('reading-dismiss', function () {
        function close(refocus) {
            const p = document.getElementById('reading-panel');
            const t = document.getElementById('reading-toggle');
            if (!p || !t || p.hasAttribute('hidden')) return false;
            p.setAttribute('hidden', '');
            t.setAttribute('aria-expanded', 'false');
            if (refocus) t.focus();
            return true;
        }
        document.addEventListener('click', function (e) {
            if (e.target.closest('#reading-panel') || e.target.closest('#reading-toggle')) return;
            close(false);
        });
        // Esc closes the popover and returns focus to its trigger.
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape') close(true);
        });
    });

    // Arrow keys move focus within each segmented control.
    panel.querySelectorAll('[data-reading]').forEach(function (seg) {
        seg.addEventListener('keydown', function (e) {
            if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
            const btns = Array.from(seg.querySelectorAll('button'));
            const i = btns.indexOf(document.activeElement);
            if (i < 0) return;
            e.preventDefault();
            const n = btns.length;
            btns[e.key === 'ArrowRight' ? (i + 1) % n : (i - 1 + n) % n].focus();
        });
    });
});

// Theme. Light is the default, not the operating system: this is a document
// tool people keep open beside an editor, and a light page is the one most
// readers expect. The OS setting is still available, as an explicit choice.
//
// Two controls drive it - the cycling button in the top bar and the segmented
// control on Settings - so both read the same value and both re-render when
// either one changes.
const THEME_KEY = 'flanner.theme';
const THEME_ORDER = ['light', 'dark', 'system'];
const THEME_GLYPH = { light: '○', dark: '●', system: '◐' };

function themeChoice() {
    try {
        const t = localStorage.getItem(THEME_KEY);
        return THEME_ORDER.indexOf(t) !== -1 ? t : 'light';
    } catch (e) { return 'light'; }
}

function applyTheme(mode) {
    const d = document.documentElement;
    // "system" is the only mode that leaves the attribute off, which is what
    // lets the prefers-color-scheme rules in tokens.css take over.
    if (mode === 'system') { delete d.dataset.theme; } else { d.dataset.theme = mode; }
    try { localStorage.setItem(THEME_KEY, mode); } catch (e) {}
    document.dispatchEvent(new CustomEvent('flanner:theme', { detail: mode }));
}

function renderThemeControls(mode) {
    // There are two buttons: one in the top bar for wide screens, one in the
    // header row for narrow ones. Only one is ever visible; both stay in step.
    document.querySelectorAll('[data-theme-toggle]').forEach(function (btn) {
        const glyph = btn.querySelector('[data-theme-glyph]') || btn.firstElementChild;
        if (glyph) glyph.textContent = THEME_GLYPH[mode];
        btn.setAttribute('aria-label', 'Theme: ' + mode + '. Click to change.');
        btn.title = 'Theme: ' + mode;
    });
    document.querySelectorAll('[data-theme-choice]').forEach(function (b) {
        b.setAttribute('aria-pressed', String(b.dataset.themeChoice === mode));
    });
}

once('theme-sync', function () {
    document.addEventListener('flanner:theme', function (e) { renderThemeControls(e.detail); });
});

onPage(function () {
    renderThemeControls(themeChoice());

    document.querySelectorAll('[data-theme-toggle]').forEach(function (btn) {
        btn.addEventListener('click', function () {
            const at = THEME_ORDER.indexOf(themeChoice());
            applyTheme(THEME_ORDER[(at + 1) % THEME_ORDER.length]);
        });
    });
    document.querySelectorAll('[data-theme-choice]').forEach(function (b) {
        b.addEventListener('click', function () { applyTheme(b.dataset.themeChoice); });
    });
});

// --- copy a command ---------------------------------------------------------
//
// Delegated on `document`, so it survives a page swap and needs no rewiring.
// The button is an icon, so success is shown by swapping the icon rather than
// the label; `aria-label` changes with it, because a screen reader gets
// nothing from an <svg> that turned into a tick.

once('copy-command', function () {
    document.addEventListener('click', function (event) {
        const button = event.target.closest('[data-copy]');
        if (!button) return;
        const source = document.querySelector(button.dataset.copy);
        if (!source) return;

        const said = button.getAttribute('aria-label') || 'Copy command';
        const settle = function (label, ok) {
            button.classList.toggle('is-copied', ok);
            button.setAttribute('aria-label', label);
            button.setAttribute('title', label);
            clearTimeout(button._copyTimer);
            button._copyTimer = setTimeout(function () {
                button.classList.remove('is-copied');
                button.setAttribute('aria-label', said);
                button.setAttribute('title', said);
            }, 1600);
        };

        // Absent on a page served over plain http from anything but
        // localhost, so the failure is reported rather than thrown.
        if (!navigator.clipboard) {
            settle('Press Ctrl+C to copy', false);
            return;
        }
        navigator.clipboard.writeText(source.textContent.trim()).then(
            function () { settle('Copied', true); },
            function () { settle('Press Ctrl+C to copy', false); }
        );
    });
});

// Command palette (Cmd/Ctrl+K): jump to any project or plan.
onPage(function () {
    const dlg = document.getElementById('cmdk');
    const input = document.getElementById('cmdk-input');
    const list = document.getElementById('cmdk-list');
    if (!dlg || !input || !list || typeof dlg.showModal !== 'function') return;

    let index = null;   // cached search index
    let items = [];      // current filtered results
    let active = 0;

    async function loadIndex() {
        if (index) return;
        try { index = await (await fetch('/api/search')).json(); } catch (e) { index = []; }
    }

    function render(query) {
        const q = query.trim().toLowerCase();
        const all = index || [];
        items = (q
            ? all.filter(function (it) {
                return (it.name + ' ' + (it.context || '')).toLowerCase().indexOf(q) !== -1;
            })
            : all
        ).slice(0, 20);
        active = 0;
        list.innerHTML = '';
        items.forEach(function (it, i) {
            const li = document.createElement('li');
            li.className = 'cmdk-item';
            li.id = 'cmdk-opt-' + i;
            li.setAttribute('role', 'option');
            const kind = document.createElement('span');
            kind.className = 'cmdk-kind';
            kind.textContent = it.type;
            const name = document.createElement('span');
            name.className = 'cmdk-name';
            name.textContent = it.name;
            li.append(kind, name);
            if (it.context) {
                const ctx = document.createElement('span');
                ctx.className = 'cmdk-ctx';
                ctx.textContent = it.context;
                li.appendChild(ctx);
            }
            li.addEventListener('click', function () { go(i); });
            list.appendChild(li);
        });
        if (!items.length) {
            const empty = document.createElement('li');
            empty.className = 'cmdk-empty';
            empty.textContent = 'No matches';
            list.appendChild(empty);
        }
        updateActive();
    }

    function updateActive() {
        const els = list.querySelectorAll('.cmdk-item');
        els.forEach(function (li, i) { li.setAttribute('aria-selected', String(i === active)); });
        if (els[active] && els[active].scrollIntoView) els[active].scrollIntoView({ block: 'nearest' });
        input.setAttribute('aria-activedescendant', items[active] ? 'cmdk-opt-' + active : '');
    }

    function go(i) {
        const it = items[i];
        if (it) window.location.href = it.url;
    }

    async function open() {
        await loadIndex();
        input.value = '';
        render('');
        if (!dlg.open) dlg.showModal();
        input.focus();
    }

    // The dialog lives outside the swapped shell, so one registration holds
    // for the life of the tab.
    once('cmdk-hotkey', function () {
        document.addEventListener('keydown', function (e) {
            if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
                e.preventDefault();
                open();
            }
        });
    });
    const trigger = document.getElementById('cmdk-open');
    if (trigger) trigger.addEventListener('click', open);
    input.addEventListener('input', function () { render(input.value); });
    input.addEventListener('keydown', function (e) {
        if (e.key === 'ArrowDown') { e.preventDefault(); active = Math.min(active + 1, items.length - 1); updateActive(); }
        else if (e.key === 'ArrowUp') { e.preventDefault(); active = Math.max(active - 1, 0); updateActive(); }
        else if (e.key === 'Enter') { e.preventDefault(); go(active); }
    });
    dlg.addEventListener('click', function (e) { if (e.target === dlg) dlg.close(); });
});

// List filter + sort: client-side, over the rendered page. Any [data-listgroup]
// with a [data-list-filter] input and/or [data-list-sort] select reorders and
// hides its [data-list-item] children by their data-* attributes.
// ponytail: operates on the current page (50 items); global search is Cmd+K.
onPage(function () {
    document.querySelectorAll('[data-listgroup]').forEach(function (group) {
        const list = group.querySelector('[data-list]');
        if (!list) return;
        // The controls usually sit in the top bar, which is outside the card
        // they act on, so fall back to the page. Safe because a page carries
        // at most one list group; if that stops being true, give the controls
        // a group id to point at.
        const filter = group.querySelector('[data-list-filter]')
            || document.querySelector('[data-list-filter]');
        const sort = group.querySelector('[data-list-sort]')
            || document.querySelector('[data-list-sort]');
        const empty = group.querySelector('[data-list-empty]');
        const items = function () { return Array.from(list.querySelectorAll('[data-list-item]')); };

        function applyFilter() {
            const q = (filter ? filter.value : '').toLowerCase().trim();
            let shown = 0;
            items().forEach(function (it) {
                const hay = (it.dataset.name || it.textContent).toLowerCase();
                const hit = !q || hay.indexOf(q) !== -1;
                it.hidden = !hit;
                if (hit) shown++;
            });
            if (empty) empty.hidden = shown !== 0;
        }

        function applySort() {
            if (!sort || !sort.value) return;
            const parts = sort.value.split(':');
            const key = parts[0];
            const mul = parts[1] === 'desc' ? -1 : 1;
            items().sort(function (a, b) {
                const av = a.dataset[key] || '';
                const bv = b.dataset[key] || '';
                const an = Number(av), bn = Number(bv);
                const numeric = av !== '' && bv !== '' && !isNaN(an) && !isNaN(bn);
                const cmp = numeric ? an - bn : av.localeCompare(bv);
                return cmp * mul;
            }).forEach(function (it) { list.appendChild(it); });
        }

        if (filter) filter.addEventListener('input', applyFilter);
        if (sort) sort.addEventListener('change', function () { applySort(); applyFilter(); });
        applySort();
    });
});

// Prefetch internal pages on hover, so a click feels instant.
once('prefetch', function () {
    const seen = new Set();
    document.body.addEventListener('mouseover', function (e) {
        const a = e.target.closest('a[href^="/"]');
        if (!a) return;
        const href = a.getAttribute('href');
        if (!href || seen.has(href) || href.indexOf('/static/') === 0) return;
        seen.add(href);
        const link = document.createElement('link');
        link.rel = 'prefetch';
        link.href = href;
        document.head.appendChild(link);
    });
});

// Inline uniqueness check: warn before submit if a project/plan name is taken,
// instead of only learning it from the server round-trip. Reuses /api/search.
onPage(function () {
    const inputs = document.querySelectorAll('[data-check-unique]');
    if (!inputs.length) return;
    let index = null;
    async function taken(type, scope) {
        if (!index) {
            try { index = await (await fetch('/api/search')).json(); } catch (e) { index = []; }
        }
        return index
            .filter(function (i) { return i.type === type && (!scope || i.context === scope); })
            .map(function (i) { return i.name.toLowerCase(); });
    }
    inputs.forEach(function (input) {
        const err = input.parentElement.querySelector('.field-error');
        let names = null;
        async function check() {
            if (names === null) names = await taken(input.dataset.checkUnique, input.dataset.checkScope || '');
            const v = input.value.trim().toLowerCase();
            const dup = !!v && names.indexOf(v) !== -1;
            input.setAttribute('aria-invalid', String(dup));
            if (err) {
                err.hidden = !dup;
                err.textContent = dup ? 'That name is already taken.' : '';
            }
        }
        input.addEventListener('input', check);
    });
});

// Confirmation dialogs
function confirmDelete(message) {
    return confirm(message || 'Are you sure you want to delete this item?');
}

// Copy to clipboard
function copyToClipboard(text) {
    navigator.clipboard.writeText(text).then(() => {
        showNotification('Copied to clipboard!', 'success');
    });
}

// Toasts: client-side notifications in a bottom-right, aria-live region.
// Styling and motion live in shell.css (.toast*); this only builds the nodes.
function showNotification(message, type = 'info') {
    let region = document.querySelector('.toast-region');
    if (!region) {
        region = document.createElement('div');
        region.className = 'toast-region';
        region.setAttribute('role', 'status');
        region.setAttribute('aria-live', 'polite');
        document.body.appendChild(region);
    }

    const toast = document.createElement('div');
    toast.className = 'toast toast--' + type;

    const body = document.createElement('div');
    body.className = 'toast__body';
    body.textContent = message;

    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'toast__close';
    close.setAttribute('aria-label', 'Dismiss');
    close.textContent = '×';

    let removed = false;
    function dismiss() {
        if (removed) return;
        removed = true;
        toast.classList.add('is-leaving');
        toast.addEventListener('animationend', () => toast.remove(), { once: true });
        setTimeout(() => toast.remove(), 400);  // fallback if animation is disabled
    }

    close.addEventListener('click', dismiss);
    toast.append(body, close);
    region.appendChild(toast);
    setTimeout(dismiss, 4000);
}

// Handle URL query parameters
onPage(function () {
    const params = new URLSearchParams(window.location.search);
    if (params.has('message')) {
        const messageType = params.get('message');
        if (messageType === 'no_changes') {
            showNotification('No changes detected. Content is identical to the current version.', 'info');
        }
    }
});

// Keyboard shortcuts
function inTextField() {
    const el = document.activeElement;
    return !!el && (/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName) || el.isContentEditable);
}
document.addEventListener('keydown', function(e) {
    // Ctrl+S or Cmd+S to save (prevent default and trigger form submit)
    if ((e.ctrlKey || e.metaKey) && e.key === 's') {
        e.preventDefault();
        const form = document.querySelector('form');
        if (form) {
            form.submit();
        }
        return;
    }
    // "?" opens the keyboard-shortcuts help (but not while typing)
    if (e.key === '?' && !inTextField()) {
        const help = document.getElementById('help');
        if (help && typeof help.showModal === 'function' && !help.open) {
            e.preventDefault();
            help.showModal();
        }
    }
});

console.log('🚀 MCP Plan File Manager loaded successfully!');

// The shortcuts dialog is reachable from the sidebar and the footer as well
// as by pressing ?. Selected by attribute rather than id, because there is
// now more than one of them and an id may only be used once.
onPage(function () {
    const dlg = document.getElementById('help');
    if (!dlg) return;
    document.querySelectorAll('[data-help-open]').forEach(function (link) {
        link.addEventListener('click', function (e) {
            e.preventDefault();
            if (typeof dlg.showModal === 'function') dlg.showModal();
        });
    });
});

// --- boosted navigation -----------------------------------------------------
//
// Clicking a link fetches the next page and swaps the shell, instead of
// letting the browser tear the document down and build it again. The visible
// difference is that the stylesheet, the fonts and the scroll position stop
// flashing on every click through the sidebar.
//
// This is not a single-page app and deliberately so. The server still renders
// every page; there is no client router, no state store, and no JSON API
// behind the screens. If the fetch fails, or the response is not a page we
// recognise, the browser does the navigation itself and nothing is lost.
//
// ponytail: ~70 lines instead of a framework. The one rule it imposes on the
// rest of this file is the onPage/once split at the top.
(function () {
    const SHELL = '.shell';
    if (!window.history || !window.history.pushState || !window.DOMParser) return;
    if (!document.querySelector(SHELL)) return;

    function boostable(a, event) {
        if (!a || event.defaultPrevented) return false;
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return false;
        if (event.button !== 0) return false;
        if (a.target && a.target !== '_self') return false;
        if (a.hasAttribute('download') || a.dataset.noBoost !== undefined) return false;
        if (a.origin !== location.origin) return false;
        const href = a.getAttribute('href') || '';
        if (href.charAt(0) === '#' || href.indexOf('/static/') === 0) return false;
        // Same page, different anchor: let the browser scroll.
        if (a.pathname === location.pathname && a.search === location.search && a.hash) return false;
        return true;
    }

    let token = 0;
    let slowTimer = null;

    // The indicator waits before showing itself. Most swaps land inside that
    // window, and a bar that flashes for 80ms reads as a glitch; the ones that
    // do not land are the pages that scan git, which take a second or more.
    function busy(on) {
        clearTimeout(slowTimer);
        if (on) {
            slowTimer = setTimeout(function () {
                document.documentElement.classList.add('is-navigating');
            }, 120);
        } else {
            document.documentElement.classList.remove('is-navigating');
        }
    }

    async function visit(url, push) {
        const mine = ++token;
        busy(true);
        let markup;
        try {
            const res = await fetch(url, {
                headers: { 'X-Requested-With': 'flanner-nav' },
                credentials: 'same-origin',
                redirect: 'follow',
            });
            // Anything but a rendered page - a download, an error, a redirect
            // off-site - is the browser's job, not ours.
            if (!res.ok || (res.headers.get('content-type') || '').indexOf('text/html') === -1) {
                location.href = url;
                return;
            }
            url = res.url || url;
            markup = await res.text();
        } catch (e) {
            location.href = url;
            return;
        }
        if (mine !== token) return;  // a later click won

        const next = new DOMParser().parseFromString(markup, 'text/html');
        const incoming = next.querySelector(SHELL);
        const current = document.querySelector(SHELL);
        if (!incoming || !current) { location.href = url; return; }
        // Some pages load their own scripts, which sit outside the shell and
        // would not come with the swap. Those get a real navigation, so
        // arriving by Back works the same as arriving by click.
        if (incoming.querySelector('[data-full-load]')) { location.href = url; return; }

        current.replaceWith(incoming);
        document.title = next.title;
        if (push) history.pushState({ boosted: true }, '', url);
        window.scrollTo(0, 0);
        busy(false);

        // Re-wire the new markup, then put focus where a real navigation
        // would have left it, so keyboard and screen-reader users are not
        // stranded at the top of a document that never reloaded.
        document.dispatchEvent(new CustomEvent('flanner:page'));
        const main = document.getElementById('main');
        if (main) main.focus({ preventScroll: true });
    }

    // Live updates re-render the current page through this same path. A
    // second "swap the page in place" would be a second set of bugs.
    window.flannerVisit = visit;

    document.addEventListener('click', function (e) {
        const a = e.target.closest('a[href]');
        if (!boostable(a, e)) return;
        e.preventDefault();
        visit(a.href, true);
    });

    // A GET form is a navigation with a query string on it - the sort menus
    // are exactly this - so it gets swapped like any other link. POSTs are
    // left alone: they change something, and the redirect afterwards is the
    // browser's business.
    document.addEventListener('submit', function (e) {
        const form = e.target;
        if (e.defaultPrevented || !form || form.tagName !== 'FORM') return;
        if ((form.method || 'get').toLowerCase() !== 'get') return;
        if (form.dataset.noBoost !== undefined) return;
        const action = new URL(form.action || location.href, location.href);
        if (action.origin !== location.origin) return;
        action.search = new URLSearchParams(new FormData(form)).toString();
        e.preventDefault();
        visit(action.href, true);
    });

    window.addEventListener('popstate', function () {
        visit(location.href, false);
    });
})();

// --- custom select ------------------------------------------------------------
//
// The popup list of a native <select> cannot be styled: every browser draws
// its own, and next to the rest of the page it looks borrowed. This replaces
// the *presentation* only. The real <select> stays in the DOM and stays the
// source of truth, so forms still submit it, the change event still fires,
// and with JS off you get the browser's own control rather than nothing.
onPage(function () {
    document.querySelectorAll('select.btn, select.input').forEach(function (select) {
        if (select.dataset.customised) return;
        select.dataset.customised = '1';

        const wrap = document.createElement('div');
        wrap.className = 'sel';
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'sel-button ' + select.className;
        button.setAttribute('aria-haspopup', 'listbox');
        button.setAttribute('aria-expanded', 'false');
        if (select.id) button.setAttribute('aria-labelledby', select.id + '-label ' + select.id);
        const label = document.createElement('span');
        label.className = 'sel-label';
        const caret = document.createElement('span');
        caret.className = 'sel-caret';
        caret.setAttribute('aria-hidden', 'true');
        caret.textContent = '⌄';
        button.append(label, caret);

        const list = document.createElement('div');
        list.className = 'sel-list';
        list.setAttribute('role', 'listbox');
        list.hidden = true;

        Array.from(select.options).forEach(function (option, index) {
            const item = document.createElement('button');
            item.type = 'button';
            item.className = 'sel-option';
            item.setAttribute('role', 'option');
            item.dataset.index = String(index);
            item.textContent = option.textContent.trim();
            list.appendChild(item);
        });

        select.parentNode.insertBefore(wrap, select);
        wrap.append(select, button, list);
        // The native control keeps working for assistive tech and for form
        // submission; it is only taken out of the visual flow.
        select.classList.add('sel-native');

        function sync() {
            const chosen = select.options[select.selectedIndex];
            label.textContent = chosen ? chosen.textContent.trim() : '';
            list.querySelectorAll('.sel-option').forEach(function (item) {
                const on = Number(item.dataset.index) === select.selectedIndex;
                item.setAttribute('aria-selected', String(on));
            });
        }

        function open(yes) {
            list.hidden = !yes;
            button.setAttribute('aria-expanded', String(yes));
            if (yes) {
                const current = list.querySelector('[aria-selected="true"]');
                (current || list.firstElementChild).focus();
            }
        }

        function choose(index) {
            if (select.selectedIndex !== index) {
                select.selectedIndex = index;
                // Dispatched so every existing listener - the list sort, the
                // version picker, the auto-submitting forms - sees a change
                // exactly as if a person had used the native control.
                select.dispatchEvent(new Event('change', { bubbles: true }));
            }
            sync();
            open(false);
            button.focus();
        }

        button.addEventListener('click', function () { open(list.hidden); });
        list.addEventListener('click', function (e) {
            const item = e.target.closest('.sel-option');
            if (item) choose(Number(item.dataset.index));
        });
        list.addEventListener('keydown', function (e) {
            const items = Array.from(list.querySelectorAll('.sel-option'));
            const at = items.indexOf(document.activeElement);
            if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
                e.preventDefault();
                const next = e.key === 'ArrowDown' ? at + 1 : at - 1;
                items[(next + items.length) % items.length].focus();
            } else if (e.key === 'Escape') {
                e.preventDefault(); open(false); button.focus();
            } else if (e.key === 'Tab') {
                open(false);
            }
        });
        select.addEventListener('change', sync);
        sync();
    });
});

once('select-dismiss', function () {
    document.addEventListener('click', function (e) {
        document.querySelectorAll('.sel-list:not([hidden])').forEach(function (list) {
            if (!list.parentElement.contains(e.target)) {
                list.hidden = true;
                list.parentElement.querySelector('.sel-button').setAttribute('aria-expanded', 'false');
            }
        });
    });
});

// --- mobile shell -------------------------------------------------------------
//
// The navigation is a drawer that covers the page rather than pushing it down,
// so opening the menu never moves what you were reading. The filter and sort
// controls fold behind an icon and open as a sheet from the bottom.
//
// Both are plain class toggles; the media query decides whether any of it is
// visible, so nothing here needs to know the viewport width.
once('mobile-shell', function () {
    const scrim = document.createElement('div');
    scrim.className = 'scrim';
    document.body.appendChild(scrim);

    function rail() { return document.querySelector('.rail'); }
    function menuButton() { return document.getElementById('menu-toggle'); }
    function sheet() { return document.getElementById('listctl'); }
    function sheetButton() { return document.getElementById('filter-toggle'); }

    function showScrim(on) {
        // One class. When closed the CSS leaves it transparent and
        // pointer-events: none, so it cannot swallow a click.
        scrim.classList.toggle('is-open', on);
    }

    // While the drawer or the sheet is open, everything behind it is made
    // inert: not clickable, not focusable, not reachable by tab or by a
    // screen reader. A scrim that only dims is a suggestion; `inert` is the
    // thing that actually stops a stray tap landing on the page underneath.
    //
    // The menu button itself is deliberately left out, so the drawer can
    // always be closed by the control that opened it.
    // Where the sheet came from, so it can be put back exactly there.
    let parkedFrom = null;

    function park(panel) {
        parkedFrom = { parent: panel.parentNode, next: panel.nextSibling };
        document.body.appendChild(panel);
    }

    function unpark() {
        const panel = document.getElementById('listctl');
        if (!panel || !parkedFrom) return;
        // A navigation may have replaced the page it belonged to; then there
        // is nowhere to put it back and the new page has its own.
        if (parkedFrom.parent && parkedFrom.parent.isConnected) {
            parkedFrom.parent.insertBefore(panel, parkedFrom.next);
        } else if (panel.parentNode === document.body) {
            panel.remove();
        }
        parkedFrom = null;
    }

    function behind() {
        return [document.querySelector('.main'),
                document.querySelector('.rail-brand'),
                document.querySelector('.rail-search'),
                document.querySelector('.rail-theme')].filter(Boolean);
    }

    function freeze(on) {
        behind().forEach(function (el) {
            if (on) { el.setAttribute('inert', ''); } else { el.removeAttribute('inert'); }
        });
        // Belt and braces for browsers without inert, and it stops the page
        // scrolling under an open drawer.
        document.documentElement.classList.toggle('is-locked', on);
    }

    function closeAll(refocus) {
        const r = rail(), s = sheet();
        if (r) r.classList.remove('is-open');
        if (s) s.classList.remove('is-open');
        unpark();
        const mb = menuButton(), sb = sheetButton();
        if (mb) mb.setAttribute('aria-expanded', 'false');
        if (sb) sb.setAttribute('aria-expanded', 'false');
        showScrim(false);
        freeze(false);
        if (refocus && refocus.isConnected) refocus.focus();
    }

    document.addEventListener('click', function (e) {
        const menu = e.target.closest('#menu-toggle');
        if (menu) {
            const open = !rail().classList.contains('is-open');
            closeAll();
            rail().classList.toggle('is-open', open);
            menu.setAttribute('aria-expanded', String(open));
            showScrim(open);
            freeze(open);
            if (open) { const first = rail().querySelector('.rail-nav a'); if (first) first.focus(); }
            return;
        }
        const filter = e.target.closest('#filter-toggle');
        if (filter) {
            const panel = sheet();
            if (!panel) return;
            const open = !panel.classList.contains('is-open');
            closeAll();
            // The controls live in the top bar, which is inside the part of
            // the page being frozen. Parked on <body> while open, so the
            // sheet stays usable and everything behind it does not.
            if (open) park(panel);
            panel.classList.toggle('is-open', open);
            filter.setAttribute('aria-expanded', String(open));
            showScrim(open);
            freeze(open);
            if (open) { const input = panel.querySelector('[data-list-filter]'); if (input) input.focus(); }
            return;
        }
        if (e.target === scrim) closeAll();
        // A link inside the drawer navigates; the drawer should not follow.
        if (e.target.closest('.rail-nav a')) closeAll();
    });

    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') closeAll();
    });

    // A navigation replaces the shell, so anything still open belongs to a
    // page that no longer exists.
    document.addEventListener('flanner:page', function () { closeAll(); });
});

// The filter icon only earns its place on pages that have something to filter.
onPage(function () {
    const button = document.getElementById('filter-toggle');
    if (button) button.hidden = !document.getElementById('listctl');
});

// The frontmatter block, collapsed on arrival so the writing starts near the
// top of the page. Presentation only: the markup is always in the document,
// so find-in-page and copy still reach it once it is open.
onPage(function () {
    document.querySelectorAll('[data-meta]').forEach(function (meta) {
        const button = meta.querySelector('.meta-toggle');
        const body = meta.querySelector('.meta-body');
        const label = meta.querySelector('[data-meta-label]');
        if (!button || !body) return;

        // Left at auto once open, so the block still reflows when the window
        // changes width. Pinned back to its measured height first, because a
        // transition cannot start from auto.
        body.addEventListener('transitionend', function (e) {
            if (e.propertyName === 'height' && meta.classList.contains('is-open')) {
                body.style.height = 'auto';
            }
        });

        button.addEventListener('click', function () {
            const open = !meta.classList.contains('is-open');
            const measured = body.scrollHeight;
            if (open) {
                meta.classList.add('is-open');
                body.style.height = measured + 'px';
            } else {
                body.style.height = measured + 'px';
                void body.offsetHeight;  // force a frame at the real height
                meta.classList.remove('is-open');
                body.style.height = '0px';
            }
            button.setAttribute('aria-expanded', String(open));
            if (label) label.textContent = open ? 'Hide metadata' : 'Show metadata';
        });
    });
});


// The freshness badge, fetched rather than rendered.
//
// It is the only number in the sidebar that costs git: a walk over every
// plan in every project, several subprocesses each. Computing it inline put
// that in front of the first byte of every page, including pages that show
// no freshness at all. The page arrives first now and the number follows.
(function () {
    function fillAttention() {
        const badges = document.querySelectorAll('[data-attention]');
        if (!badges.length) return;
        fetch('/nav/attention', { headers: { 'Accept': 'application/json' } })
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (data) {
                if (!data) return;
                badges.forEach(function (badge) {
                    badge.textContent = data.count;
                    badge.hidden = !data.count;
                });
            })
            .catch(function () { /* a badge must never break a page */ });
    }
    document.addEventListener('DOMContentLoaded', fillAttention);
    document.addEventListener('flanner:page', fillAttention);
})();


// The freshness column on /projects, fetched rather than rendered.
(function () {
    const DOTS = ['fresh', 'aging', 'suspect', 'stale'];

    function fillMix() {
        const cells = document.querySelectorAll('[data-mix]');
        if (!cells.length) return;
        fetch('/projects/freshness-mix', { headers: { 'Accept': 'application/json' } })
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (mix) {
                if (!mix) return;
                cells.forEach(function (cell) {
                    const counts = mix[cell.getAttribute('data-mix')];
                    if (!counts) return;
                    const parts = DOTS.filter(function (k) { return counts[k]; })
                        .map(function (k) {
                            return '<span class="d d-' + k + '"></span>' + counts[k];
                        });
                    if (parts.length) cell.innerHTML = parts.join(' ');
                });
            })
            .catch(function () { /* a column must never break a page */ });
    }
    document.addEventListener('DOMContentLoaded', fillMix);
    document.addEventListener('flanner:page', fillMix);
})();


// The drift table, filled in as each plan is judged.
//
// Judging one plan is several git subprocesses; judging all of them took
// about seven seconds against a real store with a cold cache, and that was
// seven seconds of blank page. The work is unchanged — what changes is that
// the first answer shows up in a few hundred milliseconds and the rest land
// as they come, with a count of what is left.
//
// Rows arrive as rendered html from the same partial the page uses. Building
// them here would be a second copy of that markup, free to drift from it.
(function () {
    function insertByDrift(list, row) {
        // Worst first, which is the order the page promises. Inserting in
        // place beats appending and re-sorting: the table never reshuffles
        // under someone who has started reading it.
        const drift = Number(row.getAttribute('data-drift') || 0);
        const existing = Array.from(list.children);
        const after = existing.find(function (el) {
            return Number(el.getAttribute('data-drift') || 0) < drift;
        });
        if (after) list.insertBefore(row, after);
        else list.appendChild(row);
    }

    let inFlight = null;

    function streamFreshness() {
        // Navigation is boosted, so leaving this page replaces the DOM under
        // a scan that is still running. Without this the fetch stays open and
        // the server keeps judging plans for another few seconds, writing
        // rows into a node nobody can see.
        if (inFlight) { inFlight.abort(); inFlight = null; }

        const card = document.querySelector('[data-freshness-stream]');
        if (!card || card.dataset.streamed) return;
        card.dataset.streamed = '1';
        const controller = new AbortController();
        inFlight = controller;

        const list = card.querySelector('[data-list]');
        const progress = card.querySelector('[data-scan-progress]');
        const count = card.querySelector('[data-drift-count]');
        const clean = card.querySelector('[data-scan-clean]');
        const tallies = {};
        document.querySelectorAll('[data-tally]').forEach(function (el) {
            tallies[el.getAttribute('data-tally')] = el;
            el.textContent = '0';
        });

        let total = 0;
        let judged = 0;
        let shown = 0;
        if (progress) {
            progress.hidden = false;
            progress.textContent = 'checking…';
            progress.setAttribute('aria-live', 'polite');
        }

        function handle(line) {
            if (!line) return;
            let msg;
            try { msg = JSON.parse(line); } catch (e) { return; }

            if (msg.total !== undefined) { total = msg.total; }
            if (msg.judged) { judged += msg.judged; }
            if (msg.html && list) {
                const holder = document.createElement('div');
                holder.innerHTML = msg.html.trim();
                const row = holder.firstElementChild;
                if (row) { insertByDrift(list, row); shown += 1; }
                if (count) count.textContent = shown;
            }
            if (progress && !msg.done) {
                progress.textContent = total
                    ? 'checked ' + judged + ' of ' + total + ' plans'
                    : 'checking…';
            }
            if (msg.done) {
                if (progress) progress.hidden = true;
                if (msg.tally) {
                    Object.keys(msg.tally).forEach(function (k) {
                        if (tallies[k]) tallies[k].textContent = msg.tally[k];
                    });
                }
                if (clean) clean.hidden = shown !== 0;
                // Tell the list machinery the rows it filters over changed.
                document.dispatchEvent(new CustomEvent('flanner:list-changed'));
            }
        }

        fetch('/freshness/stream', {
            headers: { 'Accept': 'application/x-ndjson' },
            signal: controller.signal,
        })
            .then(function (response) {
                if (!response.ok || !response.body) throw new Error('no stream');
                const reader = response.body.getReader();
                const decoder = new TextDecoder();
                let buffer = '';
                return (function pump() {
                    return reader.read().then(function (chunk) {
                        if (chunk.done) { handle(buffer.trim()); return; }
                        buffer += decoder.decode(chunk.value, { stream: true });
                        const lines = buffer.split("\n");
                        buffer = lines.pop() || '';
                        lines.forEach(function (line) { handle(line.trim()); });
                        return pump();
                    });
                })();
            })
            .catch(function (err) {
                // Leaving the page is not a failure to report.
                if (err && err.name === 'AbortError') return;
                if (progress) {
                    progress.hidden = false;
                    progress.textContent = 'could not check freshness — reload to retry';
                }
            })
            .finally(function () {
                if (inFlight === controller) inFlight = null;
            });
    }

    document.addEventListener('DOMContentLoaded', streamFreshness);
    document.addEventListener('flanner:page', streamFreshness);
})();


// Live updates: what a peer, an agent or another terminal just changed.
//
// The catalog has several writers and they are separate processes, so this
// page cannot be notified in-process. The server watches the catalog and
// says which plans moved; this decides what that means for the page you are
// looking at.
//
// Server-sent events rather than the ndjson the freshness scan uses: this
// stream is open-ended, so reconnecting after a sleep or a dropped
// connection is the browser's job rather than something to hand-roll.
(function () {
    if (!window.EventSource) return;

    let source = null;
    let opened = false;

    function planBanner(planId, version) {
        const host = document.querySelector('[data-live-plan="' + planId + '"]');
        if (!host) return;
        let banner = host.querySelector('[data-live-banner]');
        if (!banner) {
            banner = document.createElement('div');
            banner.setAttribute('data-live-banner', '');
            banner.className = 'notice info';
            host.prepend(banner);
        }
        // A link, not an automatic swap. This page may be half-read, and the
        // editor certainly must not have the document changed underneath
        // somebody typing into it.
        banner.innerHTML =
            'Version ' + version + ' of this plan arrived. ' +
            '<a href="' + location.pathname + '">Open it</a>.';
    }

    // `touched` is the list of plans that moved, or null for "something may
    // have moved and this connection cannot say what" — which is the state
    // after a reconnect, since the gap is unobserved by definition.
    function refresh(touched) {
        // A plan page cares about one plan, and about nothing else that moved.
        const watching = document.querySelector('[data-live-plan]');
        if (watching) {
            const id = watching.getAttribute('data-live-plan');
            if (touched && touched.indexOf(id) === -1) return;
            fetch('/plans/' + id + '/revision', { headers: { Accept: 'application/json' } })
                .then(function (r) { return r.ok ? r.json() : null; })
                .then(function (data) {
                    if (!data) return;
                    const shown = Number(watching.getAttribute('data-live-version') || 0);
                    if (Number(data.version) !== shown) planBanner(id, data.version);
                })
                .catch(function () {});
            return;
        }

        // A list page can simply be re-rendered: nothing on it is being
        // edited, and the boosted navigator already knows how to swap a page
        // without losing the shell.
        //
        // A value on the marker narrows it to one thing worth watching --
        // a plan's history cares about that plan and nothing else. No value
        // means any change is worth re-reading, which is right for a list of
        // everything.
        const list = document.querySelector('[data-live-list]');
        if (!list || !window.flannerVisit) return;
        const only = list.getAttribute('data-live-list');
        if (only && touched && touched.indexOf(only) === -1) return;
        window.flannerVisit(location.href, false);
    }

    function onCatalog(event) {
        let msg;
        try { msg = JSON.parse(event.data); } catch (e) { return; }
        const touched = [].concat(msg.added || [], msg.changed || [], msg.removed || []);
        if (touched.length) refresh(touched);
    }

    function onReady() {
        // The server ends a connection rather than holding one open forever,
        // and a laptop lid closing ends one too. Either way the browser
        // opens another, and whatever changed in between was seen by no
        // connection at all — so the first thing a second connection does is
        // assume it missed something.
        if (opened) refresh(null);
        opened = true;
    }

    function connect() {
        if (source) return;
        source = new EventSource('/events');
        source.addEventListener('ready', onReady);
        source.addEventListener('catalog', onCatalog);
        source.addEventListener('error', function () {
            // EventSource reconnects on its own, which is the reason to use
            // it here. Nothing to do but let it.
        });
    }

    document.addEventListener('DOMContentLoaded', connect);
    // Deliberately not re-opened per navigation: the connection belongs to
    // the tab, not to the page currently in it.
})();
