// jev-gmail-filter: the little JavaScript the UI needs. Everything works as
// plain links and forms; this only adds live progress and small conveniences.

(function () {
  "use strict";

  // Live scan progress: poll while a scan runs, reload when it finishes.
  const bar = document.querySelector("[data-scan-progress]");
  if (bar) {
    const fill = bar.querySelector("span");
    const text = document.querySelector("[data-scan-text]");
    const tick = async () => {
      try {
        const res = await fetch("/scan/status", { cache: "no-store" });
        const job = await res.json();
        if (job.finished) { location.reload(); return; }
        if (job.total) {
          fill.style.width = Math.round((job.n / job.total) * 100) + "%";
          const verb = job.kind === "Labelling" ? "labelled" : "judged";
          text.textContent = `${job.kind}: ${verb} ${job.n} of ${job.total} emails…`;
        }
      } catch (e) { /* server restarting; try again */ }
      setTimeout(tick, 1000);
    };
    setTimeout(tick, 600);
  }

  // Google sign-in: poll until the browser tab finishes.
  if (document.querySelector("[data-signin-waiting]")) {
    const tick = async () => {
      try {
        const res = await fetch("/setup/signin/status", { cache: "no-store" });
        const s = await res.json();
        if (s.state !== "waiting") { location.reload(); return; }
      } catch (e) { /* ignore */ }
      setTimeout(tick, 1000);
    };
    setTimeout(tick, 1000);
  }

  // Selects and toggles marked data-autosubmit submit their form on change.
  document.addEventListener("change", (event) => {
    const el = event.target;
    if (el.matches && el.matches("[data-autosubmit]")) el.form.requestSubmit();
  });

  // File drop zone: show the chosen file's name and submit.
  document.querySelectorAll("[data-drop]").forEach((zone) => {
    const input = zone.querySelector("input[type=file]");
    const name = zone.querySelector("[data-drop-name]");
    ["dragenter", "dragover"].forEach((t) => zone.addEventListener(t, (e) => { e.preventDefault(); zone.classList.add("over"); }));
    ["dragleave", "drop"].forEach((t) => zone.addEventListener(t, () => zone.classList.remove("over")));
    zone.addEventListener("drop", (e) => {
      e.preventDefault();
      if (e.dataTransfer.files.length) { input.files = e.dataTransfer.files; input.dispatchEvent(new Event("change")); }
    });
    input.addEventListener("change", () => {
      if (input.files.length) { name.textContent = input.files[0].name; input.form.requestSubmit(); }
    });
  });

  // Keyboard: j / k move through lists with data-nav-list, like a mail client.
  document.addEventListener("keydown", (event) => {
    if (event.target.closest("input, textarea, select, [contenteditable]")) return;
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    const links = Array.from(document.querySelectorAll("[data-nav-list] > a"));
    if (!links.length || (event.key !== "j" && event.key !== "k")) return;
    const current = links.findIndex((a) => a.getAttribute("aria-current") === "true");
    const next = event.key === "j" ? Math.min(current + 1, links.length - 1) : Math.max(current - 1, 0);
    if (next !== current && links[next]) location.href = links[next].href;
  });
})();
