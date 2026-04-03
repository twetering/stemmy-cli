import * as THREE from "https://unpkg.com/three@0.164.1/build/three.module.js";

const ARENA_RADIUS = 26;
const PLAYER_HEIGHT = 1.7;
const PLAYER_SPEED = 7.2;
const WISP_COUNT = 6;

const root = document.getElementById("scene-root");
const input = document.getElementById("word-input");
const sendBtn = document.getElementById("send-btn");
const randomBtn = document.getElementById("random-btn");
const multiBtn = document.getElementById("multi-btn");
const micBtn = document.getElementById("mic-btn");
const playerAudio = document.getElementById("player");

const ui = {
  score: document.getElementById("score-val"),
  streak: document.getElementById("streak-val"),
  high: document.getElementById("highscore-val"),
  status: document.getElementById("status"),
  prompt: document.getElementById("prompt"),
  context: document.getElementById("context"),
  toastLayer: document.getElementById("toast-layer"),
};

const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.outputColorSpace = THREE.SRGBColorSpace;
root.appendChild(renderer.domElement);

const scene = new THREE.Scene();
scene.fog = new THREE.FogExp2(0x04070d, 0.04);

const camera = new THREE.PerspectiveCamera(68, window.innerWidth / window.innerHeight, 0.1, 220);
camera.position.set(0, PLAYER_HEIGHT, 12);

const clock = new THREE.Clock();
const player = {
  position: new THREE.Vector3(0, PLAYER_HEIGHT, 12),
  yaw: Math.PI,
  pitch: -0.12,
  bob: 0,
};
const keys = new Set();

let echoLevel = 2;
let multiMode = false;
let activeController = null;
let currentAudioUrl = null;
let score = 0;
let streak = 0;
let highScore = parseInt(localStorage.getItem("grafstemming_3d_hs") || "0", 10);
let listening = false;
let mouseDown = false;
let nearestWisp = null;

ui.high.textContent = String(highScore);

const ambient = new THREE.HemisphereLight(0x8db0ff, 0x0a0b10, 0.8);
scene.add(ambient);

const moon = new THREE.DirectionalLight(0x9eb9ff, 1.1);
moon.position.set(8, 18, 6);
scene.add(moon);

const glow = new THREE.PointLight(0x6aa7ff, 3.1, 26, 2);
glow.position.set(0, 0.2, 0);
scene.add(glow);

const floor = new THREE.Mesh(
  new THREE.CircleGeometry(ARENA_RADIUS + 10, 96),
  new THREE.MeshStandardMaterial({
    color: 0x0b111a,
    roughness: 0.95,
    metalness: 0.08,
  }),
);
floor.rotation.x = -Math.PI / 2;
scene.add(floor);

const floorGrid = new THREE.LineSegments(
  new THREE.EdgesGeometry(new THREE.CircleGeometry(ARENA_RADIUS + 6, 32)),
  new THREE.LineBasicMaterial({ color: 0x2b3956, transparent: true, opacity: 0.34 }),
);
floorGrid.rotation.x = -Math.PI / 2;
floorGrid.position.y = 0.02;
scene.add(floorGrid);

const pitGroup = new THREE.Group();
scene.add(pitGroup);

const rim = new THREE.Mesh(
  new THREE.TorusGeometry(5.1, 0.38, 20, 64),
  new THREE.MeshStandardMaterial({ color: 0x2a364d, metalness: 0.12, roughness: 0.7 }),
);
rim.rotation.x = Math.PI / 2;
pitGroup.add(rim);

const innerPit = new THREE.Mesh(
  new THREE.CylinderGeometry(4.5, 5, 7, 48, 1, true),
  new THREE.MeshStandardMaterial({
    color: 0x060910,
    roughness: 0.94,
    transparent: true,
    opacity: 0.96,
    side: THREE.DoubleSide,
  }),
);
innerPit.position.y = -3.2;
pitGroup.add(innerPit);

const pitCore = new THREE.Mesh(
  new THREE.SphereGeometry(1.4, 24, 24),
  new THREE.MeshBasicMaterial({ color: 0x82b5ff, transparent: true, opacity: 0.28 }),
);
pitCore.position.y = -2.2;
pitGroup.add(pitCore);

for (let i = 0; i < 10; i += 1) {
  const angle = (i / 10) * Math.PI * 2;
  const stone = new THREE.Mesh(
    new THREE.BoxGeometry(1.2 + Math.random() * 0.9, 3 + Math.random() * 5, 1.2 + Math.random() * 0.9),
    new THREE.MeshStandardMaterial({ color: 0x182030, roughness: 0.96 }),
  );
  const radius = 11 + Math.random() * 10;
  stone.position.set(Math.cos(angle) * radius, 1.4 + Math.random() * 2.8, Math.sin(angle) * radius);
  stone.rotation.y = angle + Math.random();
  scene.add(stone);
}

const starsGeo = new THREE.BufferGeometry();
const starPositions = [];
for (let i = 0; i < 900; i += 1) {
  const angle = Math.random() * Math.PI * 2;
  const radius = 8 + Math.random() * 80;
  starPositions.push(Math.cos(angle) * radius, Math.random() * 26 + 2, Math.sin(angle) * radius);
}
starsGeo.setAttribute("position", new THREE.Float32BufferAttribute(starPositions, 3));
const stars = new THREE.Points(
  starsGeo,
  new THREE.PointsMaterial({ color: 0x91aef7, size: 0.09, transparent: true, opacity: 0.65 }),
);
scene.add(stars);

const rippleMeshes = [];
function addRipple(color = 0x7eb0ff) {
  const ring = new THREE.Mesh(
    new THREE.TorusGeometry(4.9, 0.11, 12, 48),
    new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.72 }),
  );
  ring.rotation.x = Math.PI / 2;
  ring.position.y = 0.08;
  ring.userData.life = 0;
  scene.add(ring);
  rippleMeshes.push(ring);
}

function makeLabelSprite(text) {
  const canvas = document.createElement("canvas");
  canvas.width = 512;
  canvas.height = 128;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = "rgba(5,10,18,0.75)";
  roundRect(ctx, 12, 18, canvas.width - 24, canvas.height - 36, 24);
  ctx.fill();
  ctx.strokeStyle = "rgba(135,183,255,0.42)";
  ctx.lineWidth = 2;
  roundRect(ctx, 12, 18, canvas.width - 24, canvas.height - 36, 24);
  ctx.stroke();
  ctx.fillStyle = "#eef4ff";
  ctx.font = "600 48px Cinzel";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(text, canvas.width / 2, canvas.height / 2);
  const texture = new THREE.CanvasTexture(canvas);
  texture.needsUpdate = true;
  const material = new THREE.SpriteMaterial({ map: texture, transparent: true, depthWrite: false });
  const sprite = new THREE.Sprite(material);
  sprite.scale.set(5.8, 1.45, 1);
  return sprite;
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

const wisps = [];

async function createWisp() {
  try {
    const res = await fetch("/echo/random");
    if (!res.ok) {
      return null;
    }
    const data = await res.json();
    const group = new THREE.Group();
    const orb = new THREE.Mesh(
      new THREE.SphereGeometry(0.36, 20, 20),
      new THREE.MeshBasicMaterial({ color: 0xb4d0ff, transparent: true, opacity: 0.92 }),
    );
    const halo = new THREE.Mesh(
      new THREE.SphereGeometry(0.65, 20, 20),
      new THREE.MeshBasicMaterial({ color: 0x71a4ff, transparent: true, opacity: 0.18 }),
    );
    const label = makeLabelSprite(data.word);
    label.position.y = 1.05;
    group.add(halo, orb, label);
    const angle = Math.random() * Math.PI * 2;
    const radius = 8.5 + Math.random() * 11.5;
    group.position.set(Math.cos(angle) * radius, 1 + Math.random() * 2.2, Math.sin(angle) * radius);
    group.userData = {
      word: data.word,
      frequency: data.frequency || 0,
      angle,
      radius,
      speed: 0.22 + Math.random() * 0.28,
      phase: Math.random() * Math.PI * 2,
      consumed: false,
    };
    scene.add(group);
    wisps.push(group);
    return group;
  } catch {
    return null;
  }
}

async function ensureWisps() {
  const needed = WISP_COUNT - wisps.filter((w) => !w.userData.consumed).length;
  const tasks = [];
  for (let i = 0; i < needed; i += 1) {
    tasks.push(createWisp());
  }
  await Promise.all(tasks);
}

function calcPoints(word, freq) {
  const rarity = freq > 0 ? Math.max(1, Math.round(500 / Math.log10(freq + 2))) : 100;
  const len = Math.max(0, (word.length - 3) * 3);
  const strk = Math.floor(streak * 2);
  return { rarity, len, strk, total: rarity + len + strk };
}

function updateScore() {
  ui.score.textContent = String(score);
  ui.streak.textContent = String(streak);
  ui.high.textContent = String(highScore);
}

function showToast(text, negative = false) {
  const el = document.createElement("div");
  el.className = `toast${negative ? " negative" : ""}`;
  el.textContent = text;
  ui.toastLayer.appendChild(el);
  setTimeout(() => el.remove(), 2800);
}

function setStatus(text, prompt = ui.prompt.textContent) {
  ui.status.textContent = text;
  ui.prompt.textContent = prompt;
}

function setContext(before, word, after) {
  const pieces = [before, word ? `<strong>${word}</strong>` : "", after].filter(Boolean);
  ui.context.innerHTML = pieces.length ? pieces.join(" ").trim() : "";
}

function playBlob(blob) {
  if (currentAudioUrl) {
    URL.revokeObjectURL(currentAudioUrl);
  }
  currentAudioUrl = URL.createObjectURL(blob);
  playerAudio.src = currentAudioUrl;
  playerAudio.play().catch(() => {});
}

function playTransientBlob(blob, delay = 0, volume = 1) {
  const url = URL.createObjectURL(blob);
  const audio = new Audio(url);
  audio.volume = volume;
  setTimeout(() => {
    audio.play().catch(() => {});
  }, delay);
  audio.onended = () => URL.revokeObjectURL(url);
}

function pulsePit(color = 0x87b7ff) {
  glow.color.setHex(color);
  glow.intensity = 5.2;
  pitCore.material.opacity = 0.55;
  addRipple(color);
  setTimeout(() => {
    glow.color.setHex(0x6aa7ff);
    glow.intensity = 3.1;
    pitCore.material.opacity = 0.28;
  }, 450);
}

function breakStreak() {
  if (streak > 0) {
    showToast(`streak ${streak}x gebroken`, true);
  }
  streak = 0;
  updateScore();
}

async function queryEcho(query, options = {}) {
  if (!query) {
    return;
  }
  if (activeController) {
    activeController.abort();
  }
  activeController = new AbortController();
  input.value = "";
  setStatus(`zoeken naar "${query}"`, "de put luistert naar je roep");
  setContext("", "", "");

  const variant = Math.floor(Math.random() * 4);
  const url = `/echo/api?word=${encodeURIComponent(query)}&v=${variant}&echo=${echoLevel}`;

  try {
    if (multiMode) {
      const results = await Promise.allSettled(
        [0, 1, 2].map((v) =>
          fetch(`/echo/api?word=${encodeURIComponent(query)}&v=${v}&echo=${echoLevel}`, {
            signal: activeController.signal,
          }),
        ),
      );
      const ok = [];
      for (const item of results) {
        if (item.status !== "fulfilled" || !item.value.ok) {
          continue;
        }
        const response = item.value;
        const blob = await response.blob();
        ok.push({
          blob,
          freq: parseInt(response.headers.get("X-Word-Frequency") || "0", 10),
          matched: response.headers.get("X-Word-Matched") || query,
          before: decodeURIComponent(response.headers.get("X-Context-Before") || ""),
          after: decodeURIComponent(response.headers.get("X-Context-After") || ""),
        });
      }
      if (!ok.length) {
        breakStreak();
        pulsePit(0xc77070);
        setStatus(`"${query}" rust hier niet`, "kies een ander woord of zoek een nieuw fluisterlicht");
        return;
      }
      const first = ok[0];
      for (const [index, item] of ok.entries()) {
        playTransientBlob(item.blob, index * 1150, Math.max(0.32, 1 - index * 0.22));
      }
      streak += 1;
      const pts = calcPoints(first.matched, first.freq);
      score += pts.total;
      if (score > highScore) {
        highScore = score;
        localStorage.setItem("grafstemming_3d_hs", String(highScore));
      }
      updateScore();
      showToast(`+${pts.total} drievoudige echo`);
      setStatus(`${ok.length} echo's van "${first.matched}"`, "zoek verder of typ een nieuwe bezwering");
      setContext(first.before, first.matched, first.after);
      pulsePit(0xd8be6f);
      if (options.consumeWisp) {
        consumeWisp(options.consumeWisp);
      }
      return;
    }

    const res = await fetch(url, { signal: activeController.signal });
    if (!res.ok) {
      breakStreak();
      pulsePit(0xc77070);
      setStatus(`"${query}" rust hier niet`, "loop naar een nieuw licht of probeer een ander woord");
      showToast("geen echo gevonden", true);
      return;
    }
    const matched = res.headers.get("X-Word-Matched") || query;
    const freq = parseInt(res.headers.get("X-Word-Frequency") || "0", 10);
    const before = decodeURIComponent(res.headers.get("X-Context-Before") || "");
    const after = decodeURIComponent(res.headers.get("X-Context-After") || "");
    const blob = await res.blob();
    playBlob(blob);
    streak += 1;
    const pts = calcPoints(matched, freq);
    score += pts.total;
    if (score > highScore) {
      highScore = score;
      localStorage.setItem("grafstemming_3d_hs", String(highScore));
    }
    updateScore();
    showToast(`+${pts.total} ${matched}`);
    setStatus(`echo van "${matched}"`, `frequentie in archief: ${freq || "onbekend"}`);
    setContext(before, matched, after);
    pulsePit();
    if (options.consumeWisp) {
      consumeWisp(options.consumeWisp);
    }
  } catch (error) {
    if (error.name === "AbortError") {
      return;
    }
    setStatus("geen verbinding", "de 3D-ruimte bleef staan, maar de echo-service reageert niet");
    showToast("verbinding verbroken", true);
  }
}

function consumeWisp(wisp) {
  wisp.userData.consumed = true;
  wisp.visible = false;
  setTimeout(() => {
    scene.remove(wisp);
    const index = wisps.indexOf(wisp);
    if (index >= 0) {
      wisps.splice(index, 1);
    }
    ensureWisps();
  }, 120);
}

function nearestVisibleWisp() {
  let best = null;
  let bestDist = Infinity;
  for (const wisp of wisps) {
    if (wisp.userData.consumed) {
      continue;
    }
    const dist = wisp.position.distanceTo(player.position);
    if (dist < bestDist) {
      best = wisp;
      bestDist = dist;
    }
  }
  return { wisp: best, distance: bestDist };
}

function updatePrompt() {
  const { wisp, distance } = nearestVisibleWisp();
  nearestWisp = distance < 4.4 ? wisp : null;
  if (nearestWisp) {
    ui.prompt.textContent = `druk E om "${nearestWisp.userData.word}" te roepen`;
  } else {
    ui.prompt.textContent = "verken de cirkel, nader een fluisterlicht of typ een woord";
  }
}

function onResize() {
  camera.aspect = window.innerWidth / window.innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
}

window.addEventListener("resize", onResize);

window.addEventListener("keydown", (event) => {
  if (event.target === input) {
    if (event.key === "Enter") {
      queryEcho(input.value.trim());
    }
    return;
  }
  keys.add(event.key.toLowerCase());
  if (event.key.toLowerCase() === "e" && nearestWisp) {
    queryEcho(nearestWisp.userData.word, { consumeWisp: nearestWisp });
  }
});

window.addEventListener("keyup", (event) => {
  keys.delete(event.key.toLowerCase());
});

renderer.domElement.addEventListener("mousedown", () => {
  mouseDown = true;
});

window.addEventListener("mouseup", () => {
  mouseDown = false;
});

window.addEventListener("mousemove", (event) => {
  if (!mouseDown) {
    return;
  }
  player.yaw -= event.movementX * 0.004;
  player.pitch = THREE.MathUtils.clamp(player.pitch - event.movementY * 0.0035, -0.9, 0.45);
});

sendBtn.addEventListener("click", () => queryEcho(input.value.trim()));
randomBtn.addEventListener("click", async () => {
  try {
    const res = await fetch("/echo/random");
    const data = await res.json();
    queryEcho(data.word);
  } catch {
    showToast("geen willekeurig woord", true);
  }
});

multiBtn.addEventListener("click", () => {
  multiMode = !multiMode;
  multiBtn.classList.toggle("active", multiMode);
  multiBtn.textContent = `drievoudige echo: ${multiMode ? "aan" : "uit"}`;
});

for (const button of document.querySelectorAll("#echo-levels button")) {
  button.addEventListener("click", () => {
    echoLevel = parseInt(button.dataset.level || "2", 10);
    for (const b of document.querySelectorAll("#echo-levels button")) {
      b.classList.toggle("active", b === button);
    }
  });
}

const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
let recognition = null;
if (SR) {
  recognition = new SR();
  recognition.lang = "nl-NL";
  recognition.interimResults = false;
  recognition.maxAlternatives = 1;
  recognition.onresult = (event) => {
    listening = false;
    micBtn.classList.remove("active");
    const spoken = event.results[0][0].transcript.trim();
    queryEcho(spoken);
  };
  recognition.onerror = () => {
    listening = false;
    micBtn.classList.remove("active");
    showToast("spraak niet verstaan", true);
  };
  recognition.onend = () => {
    listening = false;
    micBtn.classList.remove("active");
  };
} else {
  micBtn.disabled = true;
}

micBtn.addEventListener("click", () => {
  if (!recognition) {
    return;
  }
  if (listening) {
    recognition.stop();
    return;
  }
  listening = true;
  micBtn.classList.add("active");
  recognition.start();
  setStatus("luisteren...", "spreek een woord of korte zin in");
});

function animate() {
  requestAnimationFrame(animate);
  const dt = Math.min(clock.getDelta(), 0.03);

  const forward = (keys.has("w") || keys.has("arrowup") ? 1 : 0) - (keys.has("s") || keys.has("arrowdown") ? 1 : 0);
  const strafe = (keys.has("d") || keys.has("arrowright") ? 1 : 0) - (keys.has("a") || keys.has("arrowleft") ? 1 : 0);
  const direction = new THREE.Vector3();
  if (forward || strafe) {
    const moveAngle = player.yaw + Math.atan2(strafe, forward);
    direction.set(Math.sin(moveAngle), 0, Math.cos(moveAngle));
    player.position.addScaledVector(direction, PLAYER_SPEED * dt);
  }

  const planar = new THREE.Vector2(player.position.x, player.position.z);
  if (planar.length() > ARENA_RADIUS - 2.5) {
    planar.setLength(ARENA_RADIUS - 2.5);
    player.position.x = planar.x;
    player.position.z = planar.y;
  }

  player.bob += (forward || strafe ? 10 : 3) * dt;
  camera.position.set(
    player.position.x,
    PLAYER_HEIGHT + Math.sin(player.bob) * (forward || strafe ? 0.06 : 0.02),
    player.position.z,
  );
  camera.rotation.order = "YXZ";
  camera.rotation.y = player.yaw;
  camera.rotation.x = player.pitch;

  const focus = new THREE.Vector3(
    player.position.x + Math.sin(player.yaw) * 10,
    PLAYER_HEIGHT + Math.sin(player.pitch) * 4,
    player.position.z + Math.cos(player.yaw) * 10,
  );
  camera.lookAt(focus);

  const elapsed = clock.elapsedTime;
  pitCore.position.y = -2.2 + Math.sin(elapsed * 1.3) * 0.12;
  rim.rotation.z = Math.sin(elapsed * 0.2) * 0.015;
  stars.rotation.y += dt * 0.012;

  for (const wisp of wisps) {
    if (wisp.userData.consumed) {
      continue;
    }
    wisp.userData.angle += dt * wisp.userData.speed;
    const offset = Math.sin(elapsed * 1.6 + wisp.userData.phase) * 0.35;
    wisp.position.x = Math.cos(wisp.userData.angle) * wisp.userData.radius;
    wisp.position.z = Math.sin(wisp.userData.angle) * wisp.userData.radius;
    wisp.position.y = 1.4 + offset;
    wisp.children[0].scale.setScalar(1 + Math.sin(elapsed * 3 + wisp.userData.phase) * 0.08);
    wisp.children[1].scale.setScalar(1 + Math.sin(elapsed * 2.2 + wisp.userData.phase) * 0.14);
    wisp.children[2].quaternion.copy(camera.quaternion);
  }

  for (const ring of [...rippleMeshes]) {
    ring.userData.life += dt;
    ring.scale.setScalar(1 + ring.userData.life * 3.2);
    ring.material.opacity = Math.max(0, 0.74 - ring.userData.life * 0.72);
    if (ring.userData.life > 1.2) {
      scene.remove(ring);
      rippleMeshes.splice(rippleMeshes.indexOf(ring), 1);
    }
  }

  updatePrompt();
  renderer.render(scene, camera);
}

ensureWisps();
updateScore();
setStatus("de put luistert", "verken de cirkel, nader een fluisterlicht of typ een woord");
animate();
