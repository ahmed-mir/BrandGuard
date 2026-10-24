(() => {
  const demoGuide = document.querySelector("[data-demo-walkthrough]");
  if (demoGuide) {
    const dismiss = demoGuide.querySelector("[data-dismiss-demo-walkthrough]");
    const guideKey = `brandguard-demo-guide:${demoGuide.dataset.demoGuideKey || "workspace"}`;
    try {
      if (window.sessionStorage.getItem(guideKey) === "dismissed") demoGuide.hidden = true;
    } catch (_error) {
      // The walkthrough still works if browser storage is unavailable.
    }
    if (dismiss) {
      dismiss.hidden = false;
      dismiss.addEventListener("click", () => {
        demoGuide.hidden = true;
        try {
          window.sessionStorage.setItem(guideKey, "dismissed");
        } catch (_error) {
          // Dismiss for this page view when session storage is unavailable.
        }
        document.getElementById("workspace-welcome-title")?.focus();
      });
    }
  }

  const sidebar = document.getElementById("app-sidebar");
  const main = document.getElementById("app-main");
  const toggle = document.querySelector("[data-app-menu-toggle]");
  const closers = document.querySelectorAll("[data-app-menu-close]");
  const mobile = window.matchMedia("(max-width: 800px)");

  if (!sidebar || !main || !toggle) return;
  document.documentElement.classList.add("app-menu-enhanced");
  let open = false;

  function setOpen(nextOpen, restoreFocus = true) {
    open = nextOpen;
    sidebar.classList.toggle("is-open", open);
    sidebar.inert = mobile.matches && !open;
    main.inert = mobile.matches && open;
    if (mobile.matches && !open) sidebar.setAttribute("aria-hidden", "true");
    else sidebar.removeAttribute("aria-hidden");
    sidebar.setAttribute("aria-label", "Workspace navigation");
    if (mobile.matches && open) {
      sidebar.setAttribute("role", "dialog");
      sidebar.setAttribute("aria-modal", "true");
    } else {
      sidebar.removeAttribute("role");
      sidebar.removeAttribute("aria-modal");
    }
    toggle.setAttribute("aria-expanded", String(open));
    toggle.querySelector(".visually-hidden").textContent = open
      ? "Close workspace navigation"
      : "Open workspace navigation";
    document.body.classList.toggle("app-menu-open", mobile.matches && open);
    if (mobile.matches) {
      const scrim = document.querySelector(".app-sidebar-scrim");
      if (scrim) scrim.hidden = !open;
      const closeButton = sidebar.querySelector(".app-sidebar__close");
      if (closeButton) closeButton.hidden = !open;
      toggle.hidden = false;
      if (open) {
        window.requestAnimationFrame(() => {
          sidebar.querySelector(".app-sidebar__nav a[href]")?.focus();
        });
      } else if (restoreFocus) {
        toggle.focus();
      }
    } else {
      sidebar.inert = false;
      main.inert = false;
      sidebar.removeAttribute("aria-hidden");
      toggle.hidden = true;
      const scrim = document.querySelector(".app-sidebar-scrim");
      if (scrim) scrim.hidden = true;
      const closeButton = sidebar.querySelector(".app-sidebar__close");
      if (closeButton) closeButton.hidden = true;
      document.body.classList.remove("app-menu-open");
    }
  }

  toggle.hidden = false;
  setOpen(false, false);
  toggle.addEventListener("click", () => setOpen(!open));
  closers.forEach((button) => button.addEventListener("click", () => setOpen(false)));
  sidebar.addEventListener("click", (event) => {
    if (mobile.matches && event.target.closest("a[href]")) setOpen(false, false);
  });
  document.addEventListener("keydown", (event) => {
    if (!open || !mobile.matches) return;
    if (event.key === "Escape") {
      event.preventDefault();
      setOpen(false);
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = Array.from(
      sidebar.querySelectorAll('a[href], button:not([disabled]):not([hidden])'),
    );
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });
  mobile.addEventListener("change", () => setOpen(false, false));
})();
