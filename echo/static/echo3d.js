import * as THREE from "https://unpkg.com/three@0.164.1/build/three.module.js";

const root = document.getElementById("scene-root");
const body = document.body;
const wordInput = document.getElementById("word-input");
const sendBtn = document.getElementById("send-btn");
const randomBtn = document.getElementById("random-btn");
const multiBtn = document.getElementById("multi-btn");
const micBtn = document.getElementById("mic-btn");
const musicBtn = document.getElementById("music-btn");
const collapseBtn = document.getElementById("collapse-btn");
const playerAudio = document.getElementById("player");
const ambienceAudio = document.getElementById("ambience");

const ui = {
  score: document.getElementById("score-val"),
  streak: document.getElementById("streak-val"),
  high: document.getElementById("highscore-val"),
  status: document.getElementById("status"),
  prompt: document.getElementById("prompt"),
  context: document.getElementById("context"),
  history: document.getElementById("history"),
  ritualLine: document.getElementById("ritual-line"),
  wordLayer: document.getElementById("word-layer"),
  toastLayer: document.getElementById("toast-layer"),
};

const state = {
  pointerX: 0,
  pointerY: 0,
  smoothX: 0,
  smoothY: 0,
  echoLevel: 2,
  multiMode: false,
  score: 0,
  streak: 0,
  highScore: parseInt(localStorage.getItem("grafstemming_3d_hs") || "0", 10),
  activeController: null,
  currentAudioUrl: null,
  musicEnabled: true,
  musicStarted: false,
  collapsed: false,
  listening: false,
};

ui.high.textContent = String(state.highScore);

const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.outputColorSpace = THREE.SRGBColorSpace;
root.appendChild(renderer.domElement);

const scene = new THREE.Scene();
scene.fog = new THREE.FogExp2(0x06080d, 0.043);

const camera = new THREE.PerspectiveCamera(46, window.innerWidth / window.innerHeight, 0.1, 170);
const clock = new THREE.Clock();

const ambient = new THREE.HemisphereLight(0x8ea9cf, 0x07080a, 0.78);
scene.add(ambient);

const moon = new THREE.DirectionalLight(0x9eb9df, 1.25);
moon.position.set(-12, 20, 7);
scene.add(moon);

const pitLight = new THREE.PointLight(0x79a2d4, 5.6, 38, 2);
pitLight.position.set(0, -12, 0);
scene.add(pitLight);

const floor = new THREE.Mesh(
  new THREE.CircleGeometry(28, 128),
  new THREE.MeshStandardMaterial({ color: 0x141922, roughness: 0.98, metalness: 0.02 }),
);
floor.rotation.x = -Math.PI / 2;
scene.add(floor);

const path = new THREE.Mesh(
  new THREE.RingGeometry(8.4, 20.5, 128),
  new THREE.MeshStandardMaterial({
    color: 0x0e141b,
    roughness: 0.94,
    transparent: true,
    opacity: 0.95,
  }),
);
path.rotation.x = -Math.PI / 2;
path.position.y = 0.02;
scene.add(path);

const wellBody = new THREE.Mesh(
  new THREE.CylinderGeometry(7.2, 7.6, 2.9, 128),
  new THREE.MeshStandardMaterial({ color: 0x2a313c, roughness: 0.9, metalness: 0.04 }),
);
wellBody.position.y = 0.95;
scene.add(wellBody);

const rim = new THREE.Mesh(
  new THREE.TorusGeometry(7.05, 0.52, 28, 128),
  new THREE.MeshStandardMaterial({ color: 0x4d493f, roughness: 0.64, metalness: 0.08 }),
);
rim.rotation.x = Math.PI / 2;
rim.position.y = 2.08;
scene.add(rim);

const rimGlow = new THREE.Mesh(
  new THREE.TorusGeometry(5.95, 0.1, 18, 128),
  new THREE.MeshBasicMaterial({ color: 0xd0bb89, transparent: true, opacity: 0.16 }),
);
rimGlow.rotation.x = Math.PI / 2;
rimGlow.position.y = 1.88;
scene.add(rimGlow);

const shaftWall = new THREE.Mesh(
  new THREE.CylinderGeometry(5.9, 6.2, 70, 128, 1, true),
  new THREE.MeshStandardMaterial({
    color: 0x07090d,
    roughness: 0.98,
    side: THREE.DoubleSide,
  }),
);
shaftWall.position.y = -33;
scene.add(shaftWall);

const shaftGlow = new THREE.Mesh(
  new THREE.CylinderGeometry(2.6, 3.8, 56, 64),
  new THREE.MeshBasicMaterial({ color: 0x6b91c1, transparent: true, opacity: 0.14 }),
);
shaftGlow.position.y = -28;
scene.add(shaftGlow);

const water = new THREE.Mesh(
  new THREE.CircleGeometry(3.4, 72),
  new THREE.MeshBasicMaterial({ color: 0x486d9d, transparent: true, opacity: 0.16 }),
);
water.rotation.x = -Math.PI / 2;
water.position.y = -58;
scene.add(water);

function addForegroundStone(x, y, z, w, h, d, rotY = 0) {
  const stone = new THREE.Mesh(
    new THREE.BoxGeometry(w, h, d),
    new THREE.MeshStandardMaterial({ color: 0x242b35, roughness: 0.96 }),
  );
  stone.position.set(x, y, z);
  stone.rotation.y = rotY;
  scene.add(stone);
}

addForegroundStone(-9.4, 0.75, 8.8, 8.4, 1.5, 3.6, -0.34);
addForegroundStone(9.1, 0.75, 8.7, 7.6, 1.35, 3.3, 0.28);
addForegroundStone(0, 0.42, -12.5, 10.4, 0.84, 6.4, 0.12);

const chainGroup = new THREE.Group();
scene.add(chainGroup);

for (let i = 0; i < 18; i += 1) {
  const link = new THREE.Mesh(
    new THREE.TorusGeometry(0.16, 0.03, 8, 18),
    new THREE.MeshStandardMaterial({ color: 0x3d3a34, roughness: 0.74, metalness: 0.18 }),
  );
  link.rotation.x = Math.PI / 2;
  link.rotation.y = i % 2 === 0 ? 0 : Math.PI / 2;
  link.position.set(-3.9, 3.6 - i * 0.52, 3.7);
  chainGroup.add(link);
}

const lanternLightA = new THREE.PointLight(0xbd8259, 0.92, 8, 2.3);
lanternLightA.position.set(-5.4, 2.2, 6.2);
scene.add(lanternLightA);

const lanternLightB = new THREE.PointLight(0xc1875d, 0.88, 8, 2.3);
lanternLightB.position.set(5.7, 2.2, 6.5);
scene.add(lanternLightB);

const lanternBulbA = new THREE.Mesh(
  new THREE.SphereGeometry(0.22, 14, 14),
  new THREE.MeshBasicMaterial({ color: 0xf1bc89, transparent: true, opacity: 0.62 }),
);
lanternBulbA.position.copy(lanternLightA.position);
scene.add(lanternBulbA);

const lanternBulbB = lanternBulbA.clone();
lanternBulbB.position.copy(lanternLightB.position);
scene.add(lanternBulbB);

const stars = [];
for (let i = 0; i < 34; i += 1) {
  const dot = new THREE.Mesh(
    new THREE.SphereGeometry(0.03 + Math.random() * 0.025, 8, 8),
    new THREE.MeshBasicMaterial({
      color: Math.random() > 0.55 ? 0xc1875d : 0x8fb0d7,
      transparent: true,
      opacity: 0.32 + Math.random() * 0.2,
    }),
  );
  dot.position.set((Math.random() - 0.5) * 20, 1 + Math.random() * 7, 1 + Math.random() * 8);
  dot.userData = {
    angle: Math.random() * Math.PI * 2,
    radius: 0.6 + Math.random() * 1.4,
    speed: 0.25 + Math.random() * 0.45,
    anchorX: dot.position.x,
    anchorY: dot.position.y,
    anchorZ: dot.position.z,
  };
  scene.add(dot);
  stars.push(dot);
}

const rippleMeshes = [];
const risingSprites = [];

function addRipple(color = 0x8eb1db, speed = 1.25, opacity = 0.5) {
  const ring = new THREE.Mesh(
    new THREE.TorusGeometry(5.98, 0.11, 14, 128),
    new THREE.MeshBasicMaterial({ color, transparent: true, opacity }),
  );
  ring.rotation.x = Math.PI / 2;
  ring.position.y = 1.9;
  ring.userData = { life: 0, speed };
  scene.add(ring);
  rippleMeshes.push(ring);
}

function makeSpriteTexture(text) {
  const canvas = document.createElement("canvas");
  canvas.width = 1024;
  canvas.height = 256;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.font = "600 108px Cormorant Garamond";
  ctx.fillStyle = "rgba(240,236,226,0.96)";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(text, canvas.width / 2, canvas.height / 2);
  return new THREE.CanvasTexture(canvas);
}

function spawnRisingEcho(text) {
  const texture = makeSpriteTexture(text);
  const material = new THREE.SpriteMaterial({ map: texture, transparent: true, depthWrite: false });
  const sprite = new THREE.Sprite(material);
  sprite.scale.set(5.4, 1.24, 1);
  sprite.position.set((Math.random() - 0.5) * 0.8, -34 - Math.random() * 12, (Math.random() - 0.5) * 0.8);
  sprite.material.opacity = 0;
  sprite.userData = {
    life: 0,
    driftX: (Math.random() - 0.5) * 0.24,
    driftZ: (Math.random() - 0.5) * 0.24,
  };
  scene.add(sprite);
  risingSprites.push(sprite);
}

function showFallingWord(text) {
  const el = document.createElement("div");
  el.className = "falling-word";
  el.textContent = text;
  ui.wordLayer.appendChild(el);
  setTimeout(() => el.remove(), 2000);
}

function showRisingWord(text, warm = false) {
  const el = document.createElement("div");
  el.className = "rising-word";
  el.textContent = text;
  if (warm) {
    el.style.color = "rgba(244,227,185,0.14)";
    el.style.textShadow = "0 0 34px rgba(204,177,124,0.22)";
  }
  ui.wordLayer.appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

function showToast(text, negative = false) {
  const el = document.createElement("div");
  el.className = `toast${negative ? " negative" : ""}`;
  el.textContent = text;
  ui.toastLayer.appendChild(el);
  setTimeout(() => el.remove(), 2800);
}

function addHistory(word) {
  const chip = document.createElement("span");
  chip.className = "history-chip";
  chip.textContent = word;
  ui.history.appendChild(chip);
  while (ui.history.children.length > 8) {
    ui.history.removeChild(ui.history.firstChild);
  }
}

function setStatus(text, prompt = ui.prompt.textContent) {
  ui.status.textContent = text;
  ui.prompt.textContent = prompt;
}

function setContext(before, word, after) {
  const pieces = [before, word ? `<strong>${word}</strong>` : "", after].filter(Boolean);
  ui.context.innerHTML = pieces.length ? pieces.join(" ").trim() : "Nog geen stem uit de diepte ontvangen.";
}

function calcPoints(word, freq) {
  const rarity = freq > 0 ? Math.max(1, Math.round(500 / Math.log10(freq + 2))) : 100;
  const len = Math.max(0, (word.length - 3) * 3);
  const strk = Math.floor(state.streak * 2);
  return { total: rarity + len + strk };
}

function updateScore() {
  ui.score.textContent = String(state.score);
  ui.streak.textContent = String(state.streak);
  ui.high.textContent = String(state.highScore);
}

function breakStreak() {
  if (state.streak > 0) {
    showToast(`streak ${state.streak}x gebroken`, true);
  }
  state.streak = 0;
  updateScore();
}

function playBlob(blob) {
  if (state.currentAudioUrl) {
    URL.revokeObjectURL(state.currentAudioUrl);
  }
  state.currentAudioUrl = URL.createObjectURL(blob);
  playerAudio.src = state.currentAudioUrl;
  playerAudio.play().catch(() => {});
}

function playTransientBlob(blob, delay = 0, volume = 1) {
  const url = URL.createObjectURL(blob);
  const audio = new Audio(url);
  audio.volume = volume;
  setTimeout(() => audio.play().catch(() => {}), delay);
  audio.onended = () => URL.revokeObjectURL(url);
}

function pulseWell(color = 0x8eb1db, strong = false) {
  pitLight.color.setHex(color);
  pitLight.intensity = strong ? 7.8 : 6.8;
  shaftGlow.material.opacity = strong ? 0.24 : 0.18;
  rimGlow.material.opacity = strong ? 0.4 : 0.28;
  addRipple(color, strong ? 1.55 : 1.25, strong ? 0.58 : 0.48);
  addRipple(color, strong ? 1.9 : 1.5, strong ? 0.42 : 0.34);
  setTimeout(() => {
    pitLight.color.setHex(0x79a2d4);
    pitLight.intensity = 5.6;
    shaftGlow.material.opacity = 0.14;
    rimGlow.material.opacity = 0.16;
  }, 640);
}

function ensureAmbience() {
  if (!state.musicEnabled || state.musicStarted) {
    return;
  }
  state.musicStarted = true;
  ambienceAudio.currentTime = 0;
  ambienceAudio.volume = 0;
  ambienceAudio.play().then(() => {
    musicBtn.classList.add("active");
    let volume = 0;
    const timer = setInterval(() => {
      volume = Math.min(0.18, volume + 0.02);
      ambienceAudio.volume = volume;
      if (volume >= 0.18) {
        clearInterval(timer);
      }
    }, 150);
  }).catch(() => {
    state.musicStarted = false;
    showToast("muziek kon niet starten", true);
  });
}

function setMusic(enabled) {
  state.musicEnabled = enabled;
  if (!enabled) {
    ambienceAudio.pause();
    ambienceAudio.currentTime = 0;
    state.musicStarted = false;
    musicBtn.classList.remove("active");
    return;
  }
  ensureAmbience();
}

async function fetchEchoVariant(query, variant) {
  const response = await fetch(`/echo/api?word=${encodeURIComponent(query)}&v=${variant}&echo=${state.echoLevel}`, {
    signal: state.activeController.signal,
  });
  if (!response.ok) {
    return null;
  }
  return {
    blob: await response.blob(),
    freq: parseInt(response.headers.get("X-Word-Frequency") || "0", 10),
    matched: response.headers.get("X-Word-Matched") || query,
    before: decodeURIComponent(response.headers.get("X-Context-Before") || ""),
    after: decodeURIComponent(response.headers.get("X-Context-After") || ""),
  };
}

async function queryEcho(query) {
  if (!query) {
    return;
  }
  ensureAmbience();

  if (state.activeController) {
    state.activeController.abort();
  }
  state.activeController = new AbortController();
  wordInput.value = "";

  showFallingWord(query);
  addRipple(0xccb17c, 1.05, 0.54);
  ui.ritualLine.textContent = `"${query}" valt omlaag, wordt klein, en raakt het water ver beneden.`;
  setStatus(`"${query}" valt`, "luister naar de tijd tussen offer en antwoord");
  setContext("", "", "");

  try {
    if (state.multiMode) {
      const settled = await Promise.allSettled([0, 1, 2].map((variant) => fetchEchoVariant(query, variant)));
      const results = settled.filter((x) => x.status === "fulfilled" && x.value).map((x) => x.value);

      if (!results.length) {
        breakStreak();
        pulseWell(0xc87472, true);
        setStatus(`"${query}" verdwijnt`, "de put houdt dit woord voor zichzelf");
        ui.ritualLine.textContent = "Er stijgt niets op. Alleen een late druppel keert terug.";
        showToast("geen echo gevonden", true);
        return;
      }

      const first = results[0];
      results.forEach((item, index) => {
        playTransientBlob(item.blob, index * 1180, Math.max(0.28, 1 - index * 0.2));
        setTimeout(() => {
          spawnRisingEcho(item.matched);
          showRisingWord(item.matched, true);
        }, 440 + index * 280);
      });

      state.streak += 1;
      const points = calcPoints(first.matched, first.freq);
      state.score += points.total;
      if (state.score > state.highScore) {
        state.highScore = state.score;
        localStorage.setItem("grafstemming_3d_hs", String(state.highScore));
      }
      updateScore();
      addHistory(first.matched);
      setStatus(`drie echo's stijgen op`, `drie stemmen wonen op verschillende dieptes`);
      setContext(first.before, first.matched, first.after);
      ui.ritualLine.textContent = `Meerdere echo's klimmen na elkaar omhoog en laten "${first.matched}" langs de stenen glijden.`;
      pulseWell(0xccb17c, true);
      showToast(`+${points.total} drievoudige echo`);
      return;
    }

    const variant = Math.floor(Math.random() * 4);
    const result = await fetchEchoVariant(query, variant);
    if (!result) {
      breakStreak();
      pulseWell(0xc87472, true);
      setStatus(`"${query}" verdwijnt`, "de put houdt dit woord voor zichzelf");
      ui.ritualLine.textContent = "Er stijgt niets op. Alleen een late druppel keert terug.";
      showToast("geen echo gevonden", true);
      return;
    }

    playBlob(result.blob);
    state.streak += 1;
    const points = calcPoints(result.matched, result.freq);
    state.score += points.total;
    if (state.score > state.highScore) {
      state.highScore = state.score;
      localStorage.setItem("grafstemming_3d_hs", String(state.highScore));
    }
    updateScore();
    addHistory(result.matched);
    setStatus(`echo van "${result.matched}"`, "de diepte geeft het woord terug");
    setContext(result.before, result.matched, result.after);
    ui.ritualLine.textContent = `Vanuit de donkere schacht kruipt "${result.matched}" weer omhoog.`;
    pulseWell();
    spawnRisingEcho(result.matched);
    showRisingWord(result.matched);
    showToast(`+${points.total} ${result.matched}`);
  } catch (error) {
    if (error.name === "AbortError") {
      return;
    }
    setStatus("de put zwijgt", "het archief reageert niet");
    ui.ritualLine.textContent = "Je hoort wel de schacht, maar geen stem bereikt de rand.";
    showToast("verbinding verbroken", true);
  }
}

function onResize() {
  camera.aspect = window.innerWidth / window.innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
}

window.addEventListener("resize", onResize);

window.addEventListener("pointermove", (event) => {
  state.pointerX = (event.clientX / window.innerWidth - 0.5) * 2;
  state.pointerY = (event.clientY / window.innerHeight - 0.5) * 2;
});

window.addEventListener("pointerdown", () => {
  ensureAmbience();
});

window.addEventListener("keydown", (event) => {
  if (event.target === wordInput) {
    if (event.key === "Enter") {
      queryEcho(wordInput.value.trim());
    }
    return;
  }
  ensureAmbience();
});

sendBtn.addEventListener("click", () => queryEcho(wordInput.value.trim()));

randomBtn.addEventListener("click", async () => {
  ensureAmbience();
  try {
    const res = await fetch("/echo/random");
    const data = await res.json();
    queryEcho(data.word);
  } catch {
    showToast("geen orakelwoord", true);
  }
});

multiBtn.addEventListener("click", () => {
  state.multiMode = !state.multiMode;
  multiBtn.textContent = `drievoudige echo: ${state.multiMode ? "aan" : "uit"}`;
  multiBtn.classList.toggle("active", state.multiMode);
  ui.ritualLine.textContent = state.multiMode
    ? "De put staat nu open voor drie antwoorden tegelijk."
    : "De put antwoordt weer met een enkele stem.";
});

for (const button of document.querySelectorAll("#echo-levels button")) {
  button.addEventListener("click", () => {
    state.echoLevel = parseInt(button.dataset.level || "2", 10);
    for (const el of document.querySelectorAll("#echo-levels button")) {
      el.classList.toggle("active", el === button);
    }
    ui.ritualLine.textContent = `Echo-instelling veranderd naar ${button.textContent}.`;
  });
}

musicBtn.addEventListener("click", () => {
  setMusic(!state.musicEnabled);
});

collapseBtn.addEventListener("click", () => {
  state.collapsed = !state.collapsed;
  body.classList.toggle("chrome-collapsed", state.collapsed);
  collapseBtn.classList.toggle("active", state.collapsed);
  ui.ritualLine.textContent = state.collapsed
    ? "De grote vensters zijn weg. Alleen de rite blijft over."
    : "De vensters zijn terug. De put blijft onveranderd luisteren.";
});

const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
let recognition = null;

if (SR) {
  recognition = new SR();
  recognition.lang = "nl-NL";
  recognition.interimResults = false;
  recognition.maxAlternatives = 1;
  recognition.onresult = (event) => {
    state.listening = false;
    micBtn.classList.remove("active");
    queryEcho(event.results[0][0].transcript.trim());
  };
  recognition.onerror = () => {
    state.listening = false;
    micBtn.classList.remove("active");
    showToast("spraak niet verstaan", true);
  };
  recognition.onend = () => {
    state.listening = false;
    micBtn.classList.remove("active");
  };
} else {
  micBtn.disabled = true;
}

micBtn.addEventListener("click", () => {
  ensureAmbience();
  if (!recognition) {
    return;
  }
  if (state.listening) {
    recognition.stop();
    return;
  }
  state.listening = true;
  micBtn.classList.add("active");
  recognition.start();
  ui.ritualLine.textContent = "Je stem zakt de schacht in. Wacht op wat ervan terugkeert.";
});

function animate() {
  requestAnimationFrame(animate);
  const elapsed = clock.getElapsedTime();
  const dt = Math.min(clock.getDelta(), 0.03);

  state.smoothX += (state.pointerX - state.smoothX) * 0.04;
  state.smoothY += (state.pointerY - state.smoothY) * 0.04;

  camera.position.set(state.smoothX * 0.8, 4.9 + state.smoothY * 0.25, 12.6 + state.smoothY * 0.1);
  camera.lookAt(state.smoothX * 0.36, -18.5 + state.smoothY * -1.2, 0);

  pitLight.position.x = Math.sin(elapsed * 0.42) * 0.18;
  pitLight.position.z = Math.cos(elapsed * 0.38) * 0.18;
  shaftGlow.scale.x = 1 + Math.sin(elapsed * 1.1) * 0.03;
  shaftGlow.scale.z = 1 + Math.cos(elapsed * 1.2) * 0.03;
  water.material.opacity = 0.14 + Math.sin(elapsed * 1.6) * 0.025;
  rimGlow.rotation.z += dt * 0.035;
  chainGroup.rotation.z = Math.sin(elapsed * 0.34) * 0.03;
  lanternLightA.intensity = 0.9 + Math.sin(elapsed * 2.5) * 0.11;
  lanternLightB.intensity = 0.88 + Math.cos(elapsed * 2.2) * 0.11;

  stars.forEach((dot) => {
    dot.userData.angle += dt * dot.userData.speed;
    dot.position.x = dot.userData.anchorX + Math.cos(dot.userData.angle) * dot.userData.radius;
    dot.position.y = dot.userData.anchorY + Math.sin(dot.userData.angle * 1.7) * 0.16;
    dot.position.z = dot.userData.anchorZ + Math.sin(dot.userData.angle) * dot.userData.radius;
  });

  for (const ring of [...rippleMeshes]) {
    ring.userData.life += dt * ring.userData.speed;
    const scale = 1 + ring.userData.life * 1.62;
    ring.scale.set(scale, 1, scale);
    ring.position.y = 1.9 - ring.userData.life * 0.12;
    ring.material.opacity = Math.max(0, ring.material.opacity - dt * 0.4);
    if (ring.userData.life > 1.7) {
      scene.remove(ring);
      rippleMeshes.splice(rippleMeshes.indexOf(ring), 1);
    }
  }

  for (const sprite of [...risingSprites]) {
    sprite.userData.life += dt;
    sprite.position.y += dt * 6;
    sprite.position.x += sprite.userData.driftX * dt;
    sprite.position.z += sprite.userData.driftZ * dt;
    sprite.material.opacity = sprite.userData.life < 0.9
      ? sprite.userData.life
      : Math.max(0, 1 - (sprite.userData.life - 0.9) / 2.6);
    sprite.scale.multiplyScalar(1 + dt * 0.04);
    sprite.quaternion.copy(camera.quaternion);
    if (sprite.userData.life > 3.5) {
      scene.remove(sprite);
      risingSprites.splice(risingSprites.indexOf(sprite), 1);
    }
  }

  renderer.render(scene, camera);
}

updateScore();
setStatus("de put luistert", "Spreek of typ een woord en luister hoe de schacht het teruggeeft.");
setContext("", "", "");
ui.ritualLine.textContent = "Klik ergens in de scene om de ruimte te wekken. Daarna hoef je alleen nog woorden te offeren.";
animate();
