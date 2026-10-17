/* Frontend sem framework: um único arquivo cobre as duas páginas.
   O comportamento é ativado pela presença dos elementos no DOM. */
(function () {
  "use strict";

  // ------------------------------------------------------------ relógio
  const clock = document.getElementById("clock");
  if (clock) {
    const tick = () => (clock.textContent = new Date().toLocaleTimeString("pt-BR"));
    tick();
    setInterval(tick, 1000);
  }

  const fmtBytes = (n) => {
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + " MB";
    return (n / 1024 / 1024 / 1024).toFixed(2) + " GB";
  };

  const hhmmss = (total) => {
    total = Math.max(0, Math.floor(total));
    const h = String(Math.floor(total / 3600)).padStart(2, "0");
    const m = String(Math.floor((total % 3600) / 60)).padStart(2, "0");
    const s = String(total % 60).padStart(2, "0");
    return `${h}:${m}:${s}`;
  };

  // ------------------------------------- página principal: player HLS
  const video = document.getElementById("live-video");
  if (video) {
    const err = document.getElementById("player-error");
    const showError = () => err && err.classList.remove("hidden");
    const src = "/hls/stream.m3u8";

    if (window.Hls && window.Hls.isSupported()) {
      const hls = new window.Hls({ lowLatencyMode: true, liveSyncDurationCount: 3 });
      hls.loadSource(src);
      hls.attachMedia(video);
      hls.on(window.Hls.Events.ERROR, (_evt, data) => {
        if (data && data.fatal) {
          showError();
          // Recarrega a página para reconstruir o player (o FFmpeg pode ter
          // reiniciado o stream após uma queda de câmera).
          setTimeout(() => window.location.reload(), 5000);
        }
      });
    } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = src; // Safari/iOS tem suporte HLS nativo
    } else {
      showError();
    }
    video.play().catch(() => {});
  }

  // ------------------------------------- página principal: gravações
  const recList = document.getElementById("recordings");
  if (recList) {
    const load = async () => {
      try {
        const res = await fetch("/api/recordings", { cache: "no-store" });
        if (!res.ok) throw new Error(String(res.status));
        const items = await res.json();
        if (!items.length) {
          recList.innerHTML = '<li class="empty">Nenhuma gravação ainda.</li>';
          return;
        }
        recList.innerHTML = items
          .map(
            (it) =>
              `<li><a class="rec-link" href="${it.url}" download>${it.filename}</a>` +
              `<span class="rec-meta">${fmtBytes(it.size)}</span></li>`
          )
          .join("");
      } catch (e) {
        recList.innerHTML = '<li class="empty">Falha ao listar gravações.</li>';
      }
    };
    load();
    setInterval(load, 30000);
    const refresh = document.getElementById("refresh-recs");
    if (refresh) refresh.addEventListener("click", load);
  }

  // ------------------------------------- página do túnel
  const toggle = document.getElementById("tunnel-toggle");
  if (toggle) {
    const stateEl = document.getElementById("tunnel-state");
    const urlEl = document.getElementById("tunnel-url");
    const copyBtn = document.getElementById("copy-url");
    const timerEl = document.getElementById("tunnel-timer");
    let remaining = 0;

    const LABELS = {
      off: "Desligado",
      starting: "Iniciando…",
      active: "Ativo",
      expired: "Expirado",
    };

    const render = (s) => {
      toggle.checked = !!s.active;
      toggle.disabled = s.state === "starting";
      stateEl.textContent = LABELS[s.state] || s.state;
      stateEl.className = "state " + (s.state || "off");
      urlEl.value = s.url || "";
      copyBtn.disabled = !s.url;
      remaining = s.remaining_seconds || 0;
      timerEl.textContent = s.active && remaining > 0 ? hhmmss(remaining) : "—";
    };

    const refresh = async () => {
      try {
        const res = await fetch("/api/tunnel/status", { cache: "no-store" });
        render(await res.json());
      } catch (e) {
        /* mantém o último estado conhecido */
      }
    };

    toggle.addEventListener("change", async () => {
      toggle.disabled = true;
      const action = toggle.checked ? "start" : "stop";
      try {
        const res = await fetch(`/api/tunnel/${action}`, { method: "POST" });
        render(await res.json());
      } catch (e) {
        /* recarrega o estado real abaixo */
      }
      await refresh();
    });

    copyBtn.addEventListener("click", async () => {
      if (!urlEl.value) return;
      try {
        await navigator.clipboard.writeText(urlEl.value);
      } catch (e) {
        urlEl.select();
        document.execCommand("copy");
      }
      copyBtn.textContent = "Copiado!";
      setTimeout(() => (copyBtn.textContent = "Copiar link"), 1500);
    });

    refresh();
    setInterval(refresh, 5000);
    // Contador regressivo local entre polls.
    setInterval(() => {
      if (remaining > 0) {
        remaining -= 1;
        timerEl.textContent = hhmmss(remaining);
      }
    }, 1000);
  }
})();
