/*
 * Focus the Guacamole window before its client-level mouse handler prevents
 * the browser's default focus transfer. The event remains available to
 * Guacamole and the display's ordinary click behavior.
 */
(function installQorqutClickFocus() {
    "use strict";

    var DISPLAY_SELECTOR = ".client-main .display";

    function focusRemoteDisplay(event) {
        var target = event.target;
        if (!(target instanceof Element) || !target.closest(DISPLAY_SELECTOR))
            return;

        // Do not prevent or stop the event: Guacamole owns the input stream.
        window.focus();
    }

    // Guacamole 1.6 handles mouse events on the display and prevents their
    // default action. Pointerdown covers touch before compatibility mouse
    // events are synthesized, while the mouse guard avoids focusing twice.
    document.addEventListener("mousedown", focusRemoteDisplay, true);
    document.addEventListener("pointerdown", function focusTouchDisplay(event) {
        if (event.pointerType !== "mouse")
            focusRemoteDisplay(event);
    }, true);
})();
