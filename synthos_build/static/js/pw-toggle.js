/* ============================================================
   pw-toggle.js — password visibility toggle (eye icon)
   ============================================================
   Extracted 2026-04-23 from retail_portal.py inline scripts
   (was copy-pasted across 4+ templates). Applied automatically
   to any <input type="password"> on the page when this script
   is loaded. Paired with .pw-wrap/.pw-eye styles in core.css.

   Usage:
     <script src="/static/js/pw-toggle.js" defer></script>
   ============================================================ */

(function () {
    'use strict';

    var SVG_EYE_OPEN =
        '<svg viewBox="0 0 24 24">' +
          '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>' +
          '<circle cx="12" cy="12" r="3"/>' +
        '</svg>';

    var SVG_EYE_CLOSED =
        '<svg viewBox="0 0 24 24">' +
          '<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/>' +
          '<line x1="1" y1="1" x2="23" y2="23"/>' +
        '</svg>';

    function wrapInput(inp) {
        // Skip if already wrapped
        if (inp.parentElement && inp.parentElement.classList.contains('pw-wrap')) {
            return;
        }
        var wrap = document.createElement('div');
        wrap.className = 'pw-wrap';
        inp.parentNode.insertBefore(wrap, inp);
        wrap.appendChild(inp);

        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'pw-eye';
        btn.tabIndex = -1;
        btn.innerHTML = SVG_EYE_OPEN;
        btn.style.opacity = '0.4';

        btn.onclick = function () {
            var showing = inp.type === 'password';
            inp.type = showing ? 'text' : 'password';
            btn.innerHTML = showing ? SVG_EYE_CLOSED : SVG_EYE_OPEN;
            btn.style.opacity = showing ? '0.7' : '0.4';
        };

        wrap.appendChild(btn);
    }

    function init() {
        document.querySelectorAll('input[type="password"]').forEach(wrapInput);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
