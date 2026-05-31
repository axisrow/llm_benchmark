(function () {
    const THEME_STORAGE_KEY = 'llm-benchmark-theme';

    function getStoredTheme() {
        return localStorage.getItem(THEME_STORAGE_KEY);
    }

    function setStoredTheme(theme) {
        localStorage.setItem(THEME_STORAGE_KEY, theme);
    }

    function getPreferredTheme() {
        const storedTheme = getStoredTheme();
        if (storedTheme === 'light' || storedTheme === 'dark' || storedTheme === 'auto') {
            return storedTheme;
        }
        return 'auto';
    }

    function getResolvedTheme(theme) {
        if (theme === 'auto') {
            return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
        }
        return theme;
    }

    function setTheme(theme) {
        document.documentElement.setAttribute('data-bs-theme', getResolvedTheme(theme));
        document.documentElement.setAttribute('data-app-theme-choice', theme);
    }

    function updateThemeSwitcher(theme) {
        const label = document.querySelector('[data-theme-label]');
        const buttons = document.querySelectorAll('[data-bs-theme-value]');
        const activeButton = document.querySelector(`[data-bs-theme-value="${theme}"]`);

        buttons.forEach(button => {
            const isActive = button === activeButton;
            button.classList.toggle('active', isActive);
            button.setAttribute('aria-pressed', String(isActive));
        });

        if (label && activeButton) {
            label.textContent = activeButton.dataset.themeLabel || activeButton.textContent.trim();
        }
    }

    function initThemeSwitcher() {
        const preferredTheme = getPreferredTheme();
        setTheme(preferredTheme);
        updateThemeSwitcher(preferredTheme);

        document.querySelectorAll('[data-bs-theme-value]').forEach(button => {
            button.addEventListener('click', () => {
                const theme = button.getAttribute('data-bs-theme-value');
                setStoredTheme(theme);
                setTheme(theme);
                updateThemeSwitcher(theme);
            });
        });

        window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
            if (getPreferredTheme() === 'auto') {
                setTheme('auto');
                updateThemeSwitcher('auto');
            }
        });
    }

    function escapeHtml(s) {
        return String(s ?? '').replace(/[&<>"]/g, c =>
            ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
    }

    function setLoaded(content) {
        content.classList.remove('loading');
    }

    function fmtSecs(v) {
        return typeof v === 'number' && isFinite(v) ? v.toFixed(2) + 'с' : 'N/A';
    }

    function formatPrice(pricing) {
        if (!pricing) return 'N/A';
        const prompt = pricing.prompt_per_1m;
        const comp = pricing.completion_per_1m;
        if (prompt === null || prompt === undefined || comp === null || comp === undefined) {
            return pricing.note ? `N/A (${escapeHtml(pricing.note)})` : 'N/A';
        }
        if (prompt === 0 && comp === 0) {
            return '<span class="text-status-ok fw-semibold">Free</span>';
        }
        const usd = v => '$' + v.toFixed(v < 0.1 ? 4 : 2);
        return `${usd(prompt)} / ${usd(comp)}`;
    }

    function renderPromptText(prompt) {
        const lines = String(prompt ?? '').split(/\r?\n/);
        let html = '';
        let paragraph = [];

        const flushParagraph = () => {
            if (paragraph.length === 0) return;
            html += `<p>${paragraph.map(escapeHtml).join('<br>')}</p>`;
            paragraph = [];
        };

        lines.forEach(line => {
            const trimmed = line.trim();
            const section = trimmed.match(/^={3,}\s*(.+?)\s*={3,}\s*(.*)$/);
            if (section) {
                flushParagraph();
                html += `<h3>${escapeHtml(section[1])}</h3>`;
                if (section[2]) paragraph.push(section[2]);
                return;
            }
            if (!trimmed) {
                flushParagraph();
                return;
            }
            paragraph.push(line);
        });

        flushParagraph();
        return html;
    }

    function projectHref(name) {
        return `project.html?p=${encodeURIComponent(name)}`;
    }

    function summaryStatus(summary, copies) {
        if ((summary?.error || 0) > 0) {
            return {
                className: 'status-error',
                badgeClass: 'badge-status-error',
                label: 'Ошибка',
                shortLabel: 'Error',
            };
        }
        if ((summary?.timeout || 0) > 0) {
            return {
                className: 'status-timeout',
                badgeClass: 'badge-status-timeout',
                label: 'Таймаут',
                shortLabel: 'Timeout',
            };
        }
        return {
            className: 'status-ok',
            badgeClass: 'badge-status-ok',
            label: 'OK',
            shortLabel: 'OK',
        };
    }

    function runStatus(run) {
        if (run.code === 0) {
            return { className: 'success', badgeClass: 'badge-status-ok', label: 'Готово' };
        }
        if (run.code === 1) {
            return { className: 'timeout', badgeClass: 'badge-status-timeout', label: 'Таймаут' };
        }
        return { className: 'failed', badgeClass: 'badge-status-error', label: 'Ошибка' };
    }

    function successRate(summary, copies) {
        return ((summary?.ok || 0) / (copies || 1) * 100).toFixed(0);
    }

    setTheme(getPreferredTheme());
    document.addEventListener('DOMContentLoaded', initThemeSwitcher);

    window.BenchmarkUI = {
        escapeHtml,
        fmtSecs,
        formatPrice,
        getPreferredTheme,
        projectHref,
        renderPromptText,
        runStatus,
        setLoaded,
        setTheme,
        successRate,
        summaryStatus,
    };
}());
