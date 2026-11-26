// Звуки панели: синтез в браузере (Web Audio), без файлов. NEW — новая заявка, ALARM — заявку не взяли.
const SOUNDS = (() => {
  const n = (m) => 440 * 2 ** ((m - 69) / 12);          // нота по номеру MIDI: 72 = до второй октавы
  function osc(ctx, out, { f, t, d, type = "sine", peak = 0.2, a = 0.01, f2, detune = 0 }) {
    const o = ctx.createOscillator(), g = ctx.createGain();
    o.type = type; o.frequency.setValueAtTime(f, t); o.detune.value = detune;
    if (f2) o.frequency.exponentialRampToValueAtTime(f2, t + d);
    g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(peak, t + a); g.gain.exponentialRampToValueAtTime(0.0001, t + d);
    o.connect(g); g.connect(out); o.start(t); o.stop(t + d + 0.05);
    return o;
  }
  const bell = (c, o, f, t, d = 1.6, p = 0.16) => [[1, 1], [2.76, 0.45], [5.4, 0.2], [8.93, 0.08]].forEach(([m, v], i) => osc(c, o, { f: f * m, t, d: d / (1 + i * 0.9), peak: p * v, a: 0.004 }));
  const mar = (c, o, f, t, p = 0.22) => { osc(c, o, { f, t, d: 0.7, peak: p, a: 0.003 }); osc(c, o, { f: f * 4, t, d: 0.1, peak: p * 0.3, a: 0.002 }); };
  function pad(c, o, fs, t, d, p = 0.06) {
    const lp = c.createBiquadFilter(); lp.type = "lowpass"; lp.frequency.value = 1800; lp.connect(o);
    fs.forEach((f) => [-7, 7].forEach((dt) => osc(c, lp, { f, t, d, type: "sawtooth", peak: p, a: 0.25, detune: dt })));
  }
  // плавающий тон: точки [время, частота]
  function sweep(c, o, pts, t, { type = "square", peak = 0.1, lin = true } = {}) {
    const x = c.createOscillator(), g = c.createGain(), end = pts[pts.length - 1][0];
    x.type = type; x.frequency.setValueAtTime(pts[0][1], t);
    pts.slice(1).forEach(([dt, f]) => lin ? x.frequency.linearRampToValueAtTime(f, t + dt) : x.frequency.setValueAtTime(f, t + dt));
    g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(peak, t + 0.03);
    g.gain.setValueAtTime(peak, t + end - 0.05); g.gain.exponentialRampToValueAtTime(0.0001, t + end);
    x.connect(g); g.connect(o); x.start(t); x.stop(t + end + 0.05);
  }
  const beeps = (c, o, f, t, times, on, off, type = "square", p = 0.1) => { for (let i = 0; i < times; i++) osc(c, o, { f, t: t + i * (on + off), d: on, type, peak: p, a: 0.005 }); };

  const NEW = [
    ["Арпеджио", (c, o, t) => { [72, 76, 79, 84].forEach((m, i) => mar(c, o, n(m), t + i * 0.11)); bell(c, o, n(88), t + 0.46, 1.8, 0.1); }],
    ["Кристалл", (c, o, t) => { bell(c, o, n(88), t); bell(c, o, n(83), t + 0.18); bell(c, o, n(91), t + 0.36, 2); }],
    ["Мягкий аккорд", (c, o, t) => { pad(c, o, [60, 64, 67, 71].map(n), t, 2.2); bell(c, o, n(84), t + 0.05, 2, 0.07); }],
    ["Капли", (c, o, t) => { [1500, 1100, 1700, 1300].forEach((f, i) => osc(c, o, { f, f2: f / 2, t: t + i * 0.16, d: 0.14, peak: 0.25 })); bell(c, o, n(84), t + 0.7, 1.4, 0.08); }],
    ["Маримба", (c, o, t) => [79, 84, 88, 84, 91].forEach((m, i) => mar(c, o, n(m), t + i * 0.13))],
    ["Динь-дон", (c, o, t) => { bell(c, o, n(81), t, 1.8); bell(c, o, n(76), t + 0.45, 2.2); }],
    ["Восход", (c, o, t) => { [60, 64, 67, 72, 76, 79, 84].forEach((m, i) => osc(c, o, { f: n(m), t: t + i * 0.06, d: 0.5, type: "triangle", peak: 0.12 })); pad(c, o, [72, 76, 79].map(n), t + 0.4, 1.6); }],
    ["Ксилофон", (c, o, t) => [84, 86, 88, 91, 93, 96, 91, 96].forEach((m, i) => mar(c, o, n(m), t + i * 0.075 + (i > 5 ? 0.12 : 0), 0.18))],
    ["Гармония", (c, o, t) => { pad(c, o, [65, 69, 72].map(n), t, 1.0); pad(c, o, [60, 64, 67, 72].map(n), t + 0.8, 1.6); }],
    ["Пузырьки", (c, o, t) => { [0, 0.15, 0.3].forEach((dt, i) => osc(c, o, { f: 500 + i * 150, f2: 1100 + i * 250, t: t + dt, d: 0.16, peak: 0.22 })); bell(c, o, n(93), t + 0.5, 1.6, 0.08); }],
  ];
  const ALARM = [
    ["Сирена", (c, o, t) => sweep(c, o, [[0, 600], [0.5, 1300], [1, 600], [1.5, 1300], [2, 600], [2.5, 1300], [3, 600]], t)],
    ["Клаксон", (c, o, t) => { for (let i = 0; i < 5; i++) [440, 466].forEach((f) => osc(c, o, { f, t: t + i * 0.45, d: 0.35, type: "sawtooth", peak: 0.08, a: 0.01 })); }],
    ["Будильник", (c, o, t) => { for (let k = 0; k < 4; k++) beeps(c, o, 2000, t + k * 0.75, 4, 0.07, 0.05); }],
    ["Зуммер", (c, o, t) => { for (let i = 0; i < 4; i++) [150, 157].forEach((f) => osc(c, o, { f, t: t + i * 0.65, d: 0.5, type: "square", peak: 0.12, a: 0.01 })); }],
    ["Вой", (c, o, t) => sweep(c, o, [[0, 700], [1.2, 1600], [1.3, 700], [2.5, 1600], [2.6, 700]], t, { type: "triangle", peak: 0.25 })],
    ["Пейджер", (c, o, t) => { for (let k = 0; k < 3; k++) beeps(c, o, 3000, t + k * 0.8, 3, 0.09, 0.07, "square", 0.08); }],
    ["Тревога", (c, o, t) => sweep(c, o, [[0, 300], [1.4, 900], [1.5, 300], [2.9, 900]], t, { type: "sawtooth", peak: 0.1 })],
    ["Диссонанс", (c, o, t) => { for (let i = 0; i < 5; i++) [72, 73, 78].forEach((m) => osc(c, o, { f: n(m), t: t + i * 0.5, d: 0.38, type: "square", peak: 0.05, a: 0.005 })); }],
    ["Трель", (c, o, t) => sweep(c, o, Array.from({ length: 50 }, (_, i) => [i * 0.05, i % 2 ? 2400 : 1800]).concat([[2.55, 1800]]), t, { lin: false, peak: 0.08 })],
    ["Скорая", (c, o, t) => sweep(c, o, Array.from({ length: 10 }, (_, i) => [i * 0.3, i % 2 ? 620 : 940]).concat([[3, 940]]), t, { lin: false, peak: 0.12 })],
  ];
  let ctx;
  const audio = () => (ctx ||= new AudioContext());
  const play = (list, i) => { const c = audio(); c.resume(); const out = c.createGain(); out.gain.value = 0.9; out.connect(c.destination); list[i][1](c, out, c.currentTime + 0.05); };
  return { NEW, ALARM, audio, playNew: (i) => play(NEW, i), playAlarm: (i) => play(ALARM, i) };
})();
