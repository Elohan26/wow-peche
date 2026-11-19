import { useCallback, useEffect, useRef, useState } from "react";

const DEFAULTS = {
  pre_delay: 2,
  loop: true,
  confidence: 70,
  dip_pixels: 24,
  bite_timeout: 22,
  loot_delay: 1.8,
};

const PHASES = {
  idle: "En attente",
  focus: "Premier plan",
  wait: "Tempo",
  cast: "Lancer",
  search: "Recherche",
  bite: "Écoute",
  click: "Ferrage",
  hooked: "Ça mord",
  loot: "Butin",
  error: "Erreur",
};

function loadSettings() {
  try {
    const saved = JSON.parse(localStorage.getItem("fishingbot") || "{}");
    delete saved.audio_ratio;
    delete saved.profile;
    const confidence = Math.min(95, Math.max(20, Number(saved.confidence) || DEFAULTS.confidence));
    return { ...DEFAULTS, ...saved, confidence };
  } catch {
    return { ...DEFAULTS };
  }
}

export default function App() {
  const [settings, setSettings] = useState(loadSettings);
  const [status, setStatus] = useState({
    running: false,
    phase: "idle",
    message: "En attente",
    cycle: 0,
    catches: 0,
    bobber: null,
    audio_device: "",
    audio_confidence: 0,
    confidence_threshold: 70,
    audio_armed: false,
    audio_error: "",
    audio_listening: false,
    reference_ready: false,
    reference_error: "",
    capturing: false,
    capture_seconds: 0,
    last_capture: "",
    bobber_reference: false,
    bobber_saved_at: 0,
    window: "",
    log: [],
  });
  const [linked, setLinked] = useState(false);
  const [error, setError] = useState("");
  const [preview, setPreview] = useState(0);
  const [detecting, setDetecting] = useState(false);
  const logRef = useRef(null);
  const fileRef = useRef(null);
  const socketOpen = useRef(false);
  const [dragOver, setDragOver] = useState(false);
  const [pastedAt, setPastedAt] = useState(0);

  const uploadBobber = useCallback(async (file) => {
    if (!file || !String(file.type || "").startsWith("image/")) {
      setError("Colle une image du bouchon.");
      return;
    }
    if (file.size > 6000000) {
      setError("Image trop lourde.");
      return;
    }
    setError("");
    try {
      const dataUrl = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = () => reject(new Error("lecture"));
        reader.readAsDataURL(file);
      });
      const response = await fetch("/api/bobber", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ image: dataUrl }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        setError(typeof data.detail === "string" ? data.detail : "Image du bouchon refusée");
        return;
      }
      setPastedAt(data.bobber_saved_at || Date.now());
    } catch {
      setError("Backend injoignable.");
    }
  }, []);

  useEffect(() => {
    const onPaste = (event) => {
      const tag = event.target && event.target.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA") return;
      const item = [...(event.clipboardData?.items || [])].find((entry) => entry.type.startsWith("image/"));
      const file = item && item.getAsFile();
      if (!file) return;
      event.preventDefault();
      uploadBobber(file);
    };
    window.addEventListener("paste", onPaste);
    return () => window.removeEventListener("paste", onPaste);
  }, [uploadBobber]);

  useEffect(() => {
    localStorage.setItem("fishingbot", JSON.stringify(settings));
  }, [settings]);

  useEffect(() => {
    fetch("/api/confidence", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confidence: settings.confidence }),
    }).catch(() => {});
    // Le seuil enregistré doit être celui du détecteur dès l'ouverture.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    let alive = true;
    const pull = async () => {
      if (socketOpen.current) return;
      try {
        const response = await fetch("/api/status");
        if (!response.ok) throw new Error("status");
        const data = await response.json();
        if (!alive) return;
        setLinked(true);
        setStatus(data);
        if (data.running || data.capturing) setPreview(Date.now());
      } catch {
        if (alive && !socketOpen.current) setLinked(false);
      }
    };
    pull();
    const timer = setInterval(pull, 1000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    let socket;
    let closed = false;
    let retry;

    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      socket = new WebSocket(`${proto}://${location.host}/ws`);
      socket.onopen = () => {
        socketOpen.current = true;
        setLinked(true);
        setError("");
      };
      socket.onmessage = (event) => {
        const data = JSON.parse(event.data);
        setStatus(data);
        if (data.running || data.capturing) setPreview(Date.now());
      };
      socket.onclose = () => {
        socketOpen.current = false;
        if (!closed) retry = setTimeout(connect, 1200);
      };
      socket.onerror = () => socket.close();
    };

    connect();
    return () => {
      closed = true;
      socketOpen.current = false;
      clearTimeout(retry);
      socket?.close();
    };
  }, []);

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [status.log]);

  useEffect(() => {
    if (!status.running && !status.capturing) return undefined;
    const timer = setInterval(() => setPreview(Date.now()), 700);
    return () => clearInterval(timer);
  }, [status.running, status.capturing]);

  useEffect(() => {
    const onKey = (event) => {
      if (event.key === "Escape" && (status.running || status.capturing)) {
        event.preventDefault();
        stop();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [status.running, status.capturing]);

  function patch(field, value) {
    setSettings((current) => ({ ...current, [field]: value }));
  }

  function setConfidence(value) {
    patch("confidence", value);
    fetch("/api/confidence", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confidence: value }),
    }).catch(() => {});
  }

  async function capture() {
    setError("");
    try {
      const response = await fetch("/api/capture", { method: "POST" });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) setError(typeof data.detail === "string" ? data.detail : "Capture impossible");
    } catch {
      setError("Backend injoignable. Lance d'abord le serveur Python sur le port 8765.");
    }
  }

  async function start() {
    setError("");
    try {
      const response = await fetch("/api/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(settings),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) setError(typeof data.detail === "string" ? data.detail : "Démarrage impossible");
    } catch {
      setError("Backend injoignable. Lance d'abord le serveur Python sur le port 8765.");
    }
  }

  async function stop() {
    try {
      await fetch("/api/stop", { method: "POST" });
    } catch {
      setError("Backend injoignable.");
    }
  }

  async function detect() {
    setError("");
    setDetecting(true);
    try {
      const response = await fetch("/api/detect", { method: "POST" });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        setError(typeof data.detail === "string" ? data.detail : "Repérage impossible");
        return;
      }
      if (!data.ok) {
        setError(data.error || "Repérage impossible");
        return;
      }
      setPreview(Date.now());
      if (!data.found) {
        setError("Bouchon non vu. Il doit être à l'écran, avec des plumes colorées, et le jeu en fenêtré.");
      }
    } catch {
      setError("Backend injoignable.");
    } finally {
      setDetecting(false);
    }
  }

  const threshold = Number(settings.confidence) || 70;
  const confidence = Number(status.audio_confidence) || 0;
  const live = status.running || status.audio_listening;
  const busy = status.running || status.capturing;
  const matched = live && confidence >= threshold;

  return (
    <main className="page">
      <header className="top">
        <div>
          <p className="eyebrow">World of Warcraft</p>
          <h1>Pêche automatique</h1>
        </div>
        <div className={`pill phase-${status.phase}`}>
          <span className="dot" />
          {status.capturing ? "Capture" : PHASES[status.phase] || status.phase}
          <em>{linked ? "relié" : "backend coupé"}</em>
        </div>
      </header>

      <section className="grid">
        <div className="panel controls">
          <p className="status-line">{status.message}</p>
          <div className="stats">
            <span>Lancers <strong>{status.cycle}</strong></span>
            <span>Ferrages <strong>{status.catches}</strong></span>
            <span>
              Bouchon{" "}
              <strong>{status.bobber ? `${status.bobber.x}, ${status.bobber.y}` : "—"}</strong>
            </span>
          </div>

          <div className="actions">
            <button className="go" onClick={start} disabled={!linked || busy}>
              Démarrer
            </button>
            <button className="stop" onClick={stop} disabled={!linked}>
              Arrêter
            </button>
            <button className="ghost" onClick={capture} disabled={!linked || busy}>
              {status.capturing ? "Capture…" : "Capturer le son"}
            </button>
            <button className="ghost" onClick={detect} disabled={busy || detecting || !linked}>
              {detecting ? "Repérage…" : "Repérer le bouchon"}
            </button>
          </div>
          <p className="device">
            Capturer le son passe sur le jeu et enregistre jusqu'à Échap. Découpe le wav, puis dépose-le dans sounds/plouf.wav.
          </p>

          <label className="field">
            <span>Tempo avant le premier lancer</span>
            <input
              type="number"
              min="0"
              max="15"
              step="0.5"
              value={settings.pre_delay}
              disabled={busy}
              onChange={(event) => patch("pre_delay", Number(event.target.value))}
            />
            <small>secondes, après le passage du jeu au premier plan</small>
          </label>

          <label className="check">
            <input
              type="checkbox"
              checked={settings.loop}
              disabled={busy}
              onChange={(event) => patch("loop", event.target.checked)}
            />
            Enchaîner les lancers
          </label>

          <label className="field">
            <span>Seuil de confiance : {threshold.toFixed(0)} %</span>
            <input
              type="range"
              min="20"
              max="95"
              step="1"
              value={threshold}
              onChange={(event) => setConfidence(Number(event.target.value))}
            />
            <small>Le clic part quand la similarité avec sounds/plouf.wav atteint ce pourcentage.</small>
          </label>

          <p className="device">
            {status.capturing
              ? `Enregistrement ${Number(status.capture_seconds || 0).toFixed(1)} s — Échap sauve le wav`
              : status.reference_ready
                ? "Référence prête : sounds/plouf.wav"
                : status.reference_error || "Dépose le son découpé dans sounds/plouf.wav"}
            {status.last_capture ? ` — dernier fichier : ${status.last_capture}` : ""}
          </p>

          <div
            className={`paste${dragOver ? " over" : ""}`}
            role="button"
            tabIndex={0}
            onClick={() => fileRef.current?.click()}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ") fileRef.current?.click();
            }}
            onDragOver={(event) => {
              event.preventDefault();
              setDragOver(true);
            }}
            onDragLeave={() => setDragOver(false)}
            onDrop={(event) => {
              event.preventDefault();
              setDragOver(false);
              const file = event.dataTransfer.files?.[0];
              if (file) uploadBobber(file);
            }}
          >
            <input
              ref={fileRef}
              type="file"
              accept="image/*"
              hidden
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) uploadBobber(file);
                event.target.value = "";
              }}
            />
            {status.bobber_reference || pastedAt ? (
              <img
                src={`/api/bobber.png?t=${pastedAt || status.bobber_saved_at}`}
                alt="Bouchon de référence"
              />
            ) : (
              <strong>Colle l'image du bouchon</strong>
            )}
            <small>
              Ctrl+V, ou glisse une capture du leurre tel qu'il apparaît maintenant. Une nouvelle image remplace la précédente.
            </small>
          </div>

          <details>
            <summary>Réglages avancés</summary>
            <label className="field">
              <span>Descente visuelle du bouchon</span>
              <input
                type="number"
                min="6"
                max="80"
                value={settings.dip_pixels}
                disabled={busy}
                onChange={(event) => patch("dip_pixels", Number(event.target.value))}
              />
              <small>pixels vers le bas, si le son rate le plouf</small>
            </label>
            <label className="field">
              <span>Attente max d'une touche</span>
              <input
                type="number"
                min="8"
                max="40"
                step="1"
                value={settings.bite_timeout}
                disabled={busy}
                onChange={(event) => patch("bite_timeout", Number(event.target.value))}
              />
              <small>secondes avant de relancer</small>
            </label>
            <label className="field">
              <span>Pause après un ferrage</span>
              <input
                type="number"
                min="0.4"
                max="8"
                step="0.1"
                value={settings.loot_delay}
                disabled={busy}
                onChange={(event) => patch("loot_delay", Number(event.target.value))}
              />
              <small>secondes, le temps de ramasser</small>
            </label>
          </details>

          {error && <p className="error">{error}</p>}
        </div>

        <div className="panel view">
          <div className={`confidence${matched ? " hot" : ""}`}>
            <strong>{live ? `${confidence.toFixed(0)} %` : "—"}</strong>
            <span>seuil {threshold.toFixed(0)} %</span>
            <em>{status.capturing ? "capture" : live ? (status.audio_armed ? "à l'écoute" : "en veille") : "coupé"}</em>
          </div>
          <div className="view-head">
            <h2>Ce que le bot voit</h2>
            {status.window && <span>{status.window}</span>}
          </div>
          <div className="frame">
            {preview ? (
              <img src={`/api/debug.jpg?t=${preview}`} alt="Dernière capture avec le bouchon entouré" />
            ) : (
              <p>Le cadre s'affiche au premier repérage ou au premier lancer.</p>
            )}
            <div className="hud">
              <span>Cycle {status.cycle}</span>
              <span>{PHASES[status.phase] || status.phase}</span>
              <span>
                {live ? `${confidence.toFixed(0)} %` : "son coupé"} / seuil {threshold.toFixed(0)} %
              </span>
              <span>
                {status.bobber
                  ? `bouchon ${status.bobber.match != null ? `${status.bobber.match} % · ` : ""}${status.bobber.x}, ${status.bobber.y}`
                  : "bouchon absent"}
              </span>
            </div>
          </div>
          <ol className="log" ref={logRef}>
            {status.log.length === 0 && <li className="muted">Journal vide.</li>}
            {status.log.map((line, index) => (
              <li key={`${line.t}-${index}`}>
                <time>{line.t}</time>
                {line.text}
              </li>
            ))}
          </ol>
        </div>
      </section>

      <footer>
        <p>
          La compétence Pêche doit être sur le bouton souris 4. Le jeu doit être en fenêtré ou sans bordure,
          et son son doit sortir sur le périphérique par défaut. Le bouchon est reconnu à ses plumes rouges et vertes.
          Échap annule la pêche, ou termine une capture en écrivant le wav.
        </p>
        <p>
          Sur un serveur officiel, ce genre d'automatisation est interdit et peut faire supprimer le compte.
        </p>
      </footer>
    </main>
  );
}
