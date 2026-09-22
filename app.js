/* Goonj — Free Voice Studio. Frontend talks to the free Goonj voice API
   (server-side neural TTS) — no browser WebSocket needed, works on every
   browser and phone. */
"use strict";

/* ===> DEPLOY STEP (do this before committing): set API_BASE to the stable
   public API URL from Koyeb, e.g. "https://goonj-api-xxxx.koyeb.app".
   It must be https with no trailing slash. The test tunnel URL below is
   already expired/dead — never ship it. */
const API_BASE = "https://goonj-production.up.railway.app";

const $ = id => document.getElementById(id);
const fmtPct = v => (v >= 0 ? "+" : "") + v + "%";
const fmtHz = v => (v >= 0 ? "+" : "") + v + "Hz";

function voiceById(id) { return window.GOONJ_VOICES.find(v => v.id === id); }

/* ---------- API helpers ---------- */
async function apiPost(path, payload) {
  let res;
  try {
    res = await fetch(API_BASE + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch (e) {
    throw new Error("Could not reach the Goonj voice service. Check your connection and try again.");
  }
  let data = null;
  try { data = await res.json(); } catch (e) { /* non-JSON */ }
  if (res.status === 429) throw new Error("The voice studio is busy — please try again in a minute.");
  if (!res.ok) throw new Error((data && data.detail) || ("Request failed (" + res.status + ")."));
  return data;
}
async function apiGet(path) {
  const res = await fetch(API_BASE + path);
  if (!res.ok) throw new Error("Request failed (" + res.status + ").");
  return res.json();
}
/* Poll a job until done/error. onProgress(pct, done, total) called each poll. */
async function pollJob(jobId, onProgress) {
  const t0 = Date.now(), MAX_MS = 2 * 3600 * 1000; // up to 2h for very long texts
  for (;;) {
    await new Promise(r => setTimeout(r, 2500));
    let j;
    try { j = await apiGet("/api/job/" + jobId); }
    catch (e) { continue; } // transient network blip: keep polling
    if (onProgress) onProgress(j.progress_pct || 0, j.chunks_done || 0, j.chunks_total || 0);
    if (j.status === "done") return j;
    if (j.status === "error") throw new Error(j.error || "Voice generation failed.");
    if (Date.now() - t0 > MAX_MS) throw new Error("Timed out waiting for audio.");
  }
}
async function downloadMp3(audioUrl) {
  const res = await fetch(API_BASE + audioUrl);
  if (!res.ok) throw new Error("Could not download the finished audio (" + res.status + ").");
  const buf = new Uint8Array(await res.arrayBuffer());
  const check = mp3Check(buf);
  if (!check.ok) throw new Error("Downloaded audio failed verification.");
  return { blob: new Blob([buf], { type: "audio/mpeg" }), bytes: buf.length, frames: check.frames };
}

/* ---------- MP3 sanity check ---------- */
function mp3Check(u8) {
  if (u8.length < 1000) return { ok: false, frames: 0 };
  let frames = 0;
  for (let i = 0; i < u8.length - 1; i++) {
    if (u8[i] === 0xFF && (u8[i + 1] & 0xE0) === 0xE0) { frames++; i += 400; }
  }
  return { ok: frames > 2, frames };
}
function fmtDur(s) { if (!isFinite(s)) return "?:??"; const m = Math.floor(s / 60), ss = Math.round(s % 60); return m + ":" + String(ss).padStart(2, "0"); }
function audioDuration(url) { return new Promise(res => { const a = new Audio(); a.preload = "metadata"; a.onloadedmetadata = () => res(a.duration); a.onerror = () => res(NaN); a.src = url; }); }

/* ---------- UI helpers ---------- */
function fillVoiceSelect(sel, defId) {
  const langs = {};
  for (const v of window.GOONJ_VOICES) { (langs[v.language] = langs[v.language] || []).push(v); }
  for (const lang of Object.keys(langs).sort()) {
    const og = document.createElement("optgroup"); og.label = lang;
    for (const v of langs[lang]) {
      const o = document.createElement("option"); o.value = v.id;
      const short = v.id.split("-").slice(2).join("-").replace(/Neural$/, "");
      o.textContent = `${short} (${v.gender})`;
      if (v.id === defId) o.selected = true;
      og.appendChild(o);
    }
    sel.appendChild(og);
  }
}
function setStatus(el, msg, isErr) { el.textContent = msg; el.classList.toggle("err", !!isErr); }
async function finishResult(dl, ids, warnText) {
  const url = URL.createObjectURL(dl.blob);
  const player = $(ids.player), link = $(ids.link);
  player.src = url; link.href = url;
  const dur = await audioDuration(url);
  $(ids.info).textContent = `MP3 • ${(dl.bytes / 1024).toFixed(1)} KB • ${fmtDur(dur)} • MP3 frames verified (${dl.frames})` + (warnText ? ` • Note: ${warnText}` : "");
  $(ids.result).classList.remove("hidden");
  return { bytes: dl.bytes, seconds: dur, frames: dl.frames };
}

let busy = false;
async function onGenerateStudio() {
  if (busy) return;
  const text = $("textInput").value.trim();
  if (!text) { setStatus($("status"), "Please enter some text first.", true); return; }
  if (text.length > 60000) { setStatus($("status"), "Text is too long (max 60,000 characters).", true); return; }
  const voiceId = $("voiceSelect").value;
  if (!voiceById(voiceId)) { setStatus($("status"), "Please pick a voice.", true); return; }
  const rate = fmtPct(+$("rateRange").value), pitch = fmtHz(+$("pitchRange").value);
  busy = true; $("generateBtn").disabled = true; $("result").classList.add("hidden");
  $("progressWrap").classList.remove("hidden");
  try {
    setStatus($("status"), "Sending to the voice studio…");
    const { job_id } = await apiPost("/api/tts", { voice_id: voiceId, text, rate, pitch, emotions: true });
    const job = await pollJob(job_id, (pct, done, total) => {
      $("progressLabel").textContent = total > 1 ? `Chunk ${done} of ${total}…` : `Synthesizing…`;
      $("progressBar").style.width = pct + "%";
      setStatus($("status"), total > 1 ? `Synthesizing chunk ${done} of ${total}…` : "Synthesizing…");
    });
    $("progressBar").style.width = "100%";
    const dl = await downloadMp3(job.audio_url);
    const warns = (job.warnings || []).join(" ");
    await finishResult(dl, { player: "player", link: "downloadLink", info: "fileInfo", result: "result" }, warns);
    setStatus($("status"), warns ? "Done with a note: " + warns : `Done — one MP3 (${(dl.bytes / 1024).toFixed(1)} KB).`, !!warns);
  } catch (e) { setStatus($("status"), "Failed: " + e.message, true); }
  finally { busy = false; $("generateBtn").disabled = false; }
}

/* Podcast: convert "Speaker 1:" lines to the "1:" form the API expects. */
function normalizePodcastScript(script) {
  return script.split("\n").map(ln => ln.replace(/^\s*Speaker\s+([12])\s*:/i, "$1:")).join("\n");
}
async function onGeneratePodcast() {
  if (busy) return;
  const script = normalizePodcastScript($("podScript").value);
  const lines = script.split("\n").map(l => l.trim()).filter(Boolean);
  if (!lines.length) { setStatus($("podStatus"), "Add at least one 'Speaker 1:' / 'Speaker 2:' line.", true); return; }
  if (script.length > 60000) { setStatus($("podStatus"), "Script is too long (max 60,000 characters).", true); return; }
  const v1 = $("podVoice1").value, v2 = $("podVoice2").value;
  if (!voiceById(v1) || !voiceById(v2)) { setStatus($("podStatus"), "Please pick both speaker voices.", true); return; }
  busy = true; $("podGenerate").disabled = true; $("podResult").classList.add("hidden");
  $("podProgressWrap").classList.remove("hidden");
  try {
    setStatus($("podStatus"), "Sending to the voice studio…");
    const { job_id } = await apiPost("/api/podcast", {
      mode: "dual", voice1: v1, voice2: v2, script, rate: "+0%", pitch: "+0Hz", emotions: true,
    });
    const job = await pollJob(job_id, (pct, done, total) => {
      $("podProgressLabel").textContent = `Segment ${done} of ${total}…`;
      $("podProgressBar").style.width = pct + "%";
      setStatus($("podStatus"), `Synthesizing segment ${done} of ${total}…`);
    });
    $("podProgressBar").style.width = "100%";
    const dl = await downloadMp3(job.audio_url);
    const warns = (job.warnings || []).join(" ");
    await finishResult(dl, { player: "podPlayer", link: "podDownloadLink", info: "podFileInfo", result: "podResult" }, warns);
    setStatus($("podStatus"), warns ? "Done with a note: " + warns : `Done — one MP3 (${(dl.bytes / 1024).toFixed(1)} KB).`, !!warns);
  } catch (e) { setStatus($("podStatus"), "Failed: " + e.message, true); }
  finally { busy = false; $("podGenerate").disabled = false; }
}

document.addEventListener("DOMContentLoaded", () => {
  fillVoiceSelect($("voiceSelect"), "ur-PK-AsadNeural");
  fillVoiceSelect($("podVoice1"), "en-US-GuyNeural");
  fillVoiceSelect($("podVoice2"), "en-US-AriaNeural");
  document.querySelectorAll(".tab").forEach(t => t.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(x => x.classList.remove("active")); t.classList.add("active");
    document.querySelectorAll(".panel").forEach(p => p.classList.add("hidden"));
    $("tab-" + t.dataset.tab).classList.remove("hidden");
  }));
  const bind = (r, v, fmt) => { $(r).addEventListener("input", () => { $(v).textContent = fmt(+$(r).value); }); };
  bind("rateRange", "rateVal", v => v + "%");
  bind("pitchRange", "pitchVal", v => v + "%");
  document.querySelectorAll("[data-m]").forEach(b => b.addEventListener("click", () => {
    const ta = $("textInput"), m = b.getAttribute("data-m"), s = ta.selectionStart ?? ta.value.length, e = ta.selectionEnd ?? s;
    ta.value = ta.value.slice(0, s) + " " + m + " " + ta.value.slice(e); ta.focus();
  }));
  $("sampleUrdu").addEventListener("click", () => { $("voiceSelect").value = "ur-PK-AsadNeural"; $("textInput").value = "Assalam o Alaikum! Yeh Goonj ka test hai. [sans] Goonj ab aap ki awaaz ban sakti hai."; });
  $("sampleEnglish").addEventListener("click", () => { $("voiceSelect").value = "en-US-AriaNeural"; $("textInput").value = "Hello! This is a test of Goonj, the free voice studio. Everything is generated on Goonj's free voice service."; });
  $("generateBtn").addEventListener("click", onGenerateStudio);
  $("podSample").addEventListener("click", () => { $("podScript").value = "Speaker 1: Assalam o Alaikum and welcome to the Goonj podcast!\nSpeaker 2: Thank you! Today we are testing dual-speaker voices. [sans] It sounds quite natural.\nSpeaker 1: It really does. [ruko] Let us hear how the second voice responds.\nSpeaker 2: I am the second speaker, and I approve this message."; });
  $("podGenerate").addEventListener("click", onGeneratePodcast);
});
