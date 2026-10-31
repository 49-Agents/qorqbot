/*
 * Adds explicit clipboard controls to the Guacamole client. The native
 * clipboard service remains the source of remote clipboard events and the
 * path used to send user-approved text back to active connections.
 */
(function installQorqutClipboard() {
    "use strict";

    var TOOLBAR_SELECTOR = "[data-qorqut-clipboard]";
    var clipboardSVG = '<svg aria-hidden="true" viewBox="0 0 24 24" focusable="false"><path d="M8 5.5h8a2 2 0 0 1 2 2V19H6V7.5a2 2 0 0 1 2-2Zm2-3h4a2 2 0 0 1 2 2v1H8v-1a2 2 0 0 1 2-2Z"/><path d="M9 10h6M9 13h6M9 16h4"/></svg>';

    function clipboardDataType(ClipboardData, text, source) {
        return new ClipboardData({ source: source, type: "text/plain", data: text });
    }

    function writeLocalText(text, fallbackField) {
        function fallbackCopy() {
            // The visible field is already selected by the Copy action. Keep
            // that selection available for a user-approved Ctrl+C if the
            // browser API and execCommand are both unavailable. Do not steal
            // focus after an asynchronous rejection if the user moved away.
            if (!fallbackField || !fallbackField.isConnected || document.activeElement !== fallbackField)
                return false;
            fallbackField.value = text;
            fallbackField.select();
            try { return document.execCommand("copy"); }
            catch (ignore) { return false; }
        }

        if (navigator.clipboard && navigator.clipboard.writeText) {
            try {
                return navigator.clipboard.writeText(text).then(function() { return true; }, fallbackCopy);
            }
            catch (ignore) { return Promise.resolve(fallbackCopy()); }
        }
        return Promise.resolve(fallbackCopy());
    }

    function mountToolbar(clipboardService, ClipboardData, latestRemote) {
        var host = document.querySelector(".client-view");
        if (!host || host.querySelector(TOOLBAR_SELECTOR)) return;

        var toolbar = document.createElement("section");
        toolbar.className = "qorqut-clipboard-toolbar";
        toolbar.setAttribute("data-qorqut-clipboard", "");
        toolbar.setAttribute("aria-label", "Clipboard tools");
        toolbar.innerHTML =
            '<button type="button" class="qorqut-clipboard-toggle" aria-label="Open clipboard tools" aria-expanded="false" aria-controls="qorqut-clipboard-panel">' + clipboardSVG + '</button>' +
            '<div id="qorqut-clipboard-panel" class="qorqut-clipboard-panel" hidden>' +
            '<label for="qorqut-clipboard-text">Clipboard text</label>' +
            '<textarea id="qorqut-clipboard-text" rows="4" spellcheck="false" placeholder="Paste text here with Ctrl+V"></textarea>' +
            '<div class="qorqut-clipboard-actions">' +
            '<button type="button" data-qorqut-copy>Copy</button>' +
            '<button type="button" data-qorqut-send>Send to desktop</button>' +
            '</div>' +
            '<p class="qorqut-clipboard-status" aria-live="polite"></p>' +
            '</div>';
        host.appendChild(toolbar);

        var toggle = toolbar.querySelector(".qorqut-clipboard-toggle");
        var panel = toolbar.querySelector(".qorqut-clipboard-panel");
        var field = toolbar.querySelector("#qorqut-clipboard-text");
        var status = toolbar.querySelector(".qorqut-clipboard-status");

        function setStatus(message) { status.textContent = message; }

        toggle.addEventListener("click", function() {
            var open = panel.hidden;
            panel.hidden = !open;
            toggle.setAttribute("aria-expanded", String(open));
            toggle.setAttribute("aria-label", open ? "Close clipboard tools" : "Open clipboard tools");
            if (open && latestRemote.value !== null && !field.value)
                field.value = latestRemote.value;
        });

        toolbar.querySelector("[data-qorqut-copy]").addEventListener("click", function() {
            if (latestRemote.value === null) {
                setStatus("No remote text is available yet.");
                return;
            }

            field.value = latestRemote.value;
            field.focus();
            field.select();
            writeLocalText(latestRemote.value, field).then(function(copied) {
                if (copied)
                    setStatus("Remote text copied. The field stays selected for review.");
                else
                    setStatus("Browser clipboard access was denied. Press Ctrl+C with the text selected.");
            }, function() {
                setStatus("Browser clipboard access was denied. Press Ctrl+C with the text selected.");
            });
        });

        toolbar.querySelector("[data-qorqut-send]").addEventListener("click", function() {
            var text = field.value;
            clipboardService.setClipboard(clipboardDataType(ClipboardData, text, null)).then(function() {
                setStatus("Text sent to the desktop clipboard. Nothing was typed or submitted.");
            }, function() {
                setStatus("Could not send text to the desktop clipboard.");
            });
        });
    }

    function start($rootScope, $injector) {
        var clipboardService = $injector.get("clipboardService");
        var ClipboardData = $injector.get("ClipboardData");
        var latestRemote = { value: null, source: null };
        var routePending = false;
        var observer = null;
        var observerTimer = null;

        function clearRemoteClipboard() {
            latestRemote.value = null;
            latestRemote.source = null;
        }

        function removeToolbar() {
            var toolbar = document.querySelector(TOOLBAR_SELECTOR);
            if (toolbar) toolbar.remove();
        }

        function stopObserver() {
            if (observer) observer.disconnect();
            observer = null;
            if (observerTimer) window.clearTimeout(observerTimer);
            observerTimer = null;
        }

        function watchForClientView() {
            stopObserver();
            var root = document.body || document.documentElement;
            if (!root) return;
            observer = new MutationObserver(ensureToolbar);
            observer.observe(root, { childList: true, subtree: true });
            observerTimer = window.setTimeout(stopObserver, 5000);
        }

        $rootScope.$on("guacClipboard", function clipboardChanged(event, data) {
            if (data && data.source && /^text\//.test(data.type || "") && typeof data.data === "string") {
                latestRemote.value = data.data;
                latestRemote.source = data.source;
                var field = document.querySelector("#qorqut-clipboard-text");
                if (field && document.activeElement !== field)
                    field.value = data.data;
            }
        });

        $rootScope.$on("guacBeforeKeydown", function guardToolbarKeydown(event) {
            var toolbar = document.querySelector(TOOLBAR_SELECTOR);
            if (toolbar && toolbar.contains(document.activeElement)) event.preventDefault();
        });
        $rootScope.$on("guacBeforeKeyup", function guardToolbarKeyup(event) {
            var toolbar = document.querySelector(TOOLBAR_SELECTOR);
            if (toolbar && toolbar.contains(document.activeElement)) event.preventDefault();
        });

        function ensureToolbar() {
            if (!routePending && document.querySelector(".client-view")) stopObserver();
            if (!routePending) mountToolbar(clipboardService, ClipboardData, latestRemote);
        }

        function routeStarted() {
            routePending = true;
            clearRemoteClipboard();
            stopObserver();
            removeToolbar();
        }

        function routeFinished() {
            routePending = false;
            ensureToolbar();
            if (!document.querySelector(".client-view")) watchForClientView();
        }

        $rootScope.$on("$routeChangeStart", routeStarted);
        $rootScope.$on("$routeUpdate", clearRemoteClipboard);
        $rootScope.$on("guacClientFocused", clearRemoteClipboard);
        $rootScope.$on("$routeChangeSuccess", routeFinished);
        $rootScope.$on("$routeChangeError", routeFinished);
        ensureToolbar();
        if (!document.querySelector(".client-view")) watchForClientView();
    }

    if (window.angular && angular.module)
        angular.module("index").run(["$rootScope", "$injector", start]);
})();
