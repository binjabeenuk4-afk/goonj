
/* Goonj — Free Voice Studio. 100% client-side neural TTS. */
"use strict";
const EDGE_WSS="wss://speech.platform.bing.com/consumer/speech/synthesize/readaloud/edge/v1";
const TRUSTED_CLIENT_TOKEN="6A5AA1D4EAFF4E9FB37E23D68491D6F4";
const CHUNK_LIMIT=4000, OUTPUT_FORMAT="audio-24khz-48kbitrate-mono-mp3";
const $=id=>document.getElementById(id);

/* ---------- SHA-256 (sync fallback when crypto.subtle is unavailable, e.g. non-secure origins) ---------- */
function sha256Bytes(bytes){
  const K=[0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2];
  const H=[0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19];
  const l=bytes.length,bitLen=l*8,padLen=(((l+8)>>6)+1)*64;
  const buf=new Uint8Array(padLen);buf.set(bytes);buf[l]=0x80;
  const dv=new DataView(buf.buffer);
  dv.setUint32(padLen-8,Math.floor(bitLen/4294967296));dv.setUint32(padLen-4,bitLen>>>0);
  const w=new Uint32Array(64),rotr=(x,n)=>(x>>>n)|(x<<(32-n));
  for(let off=0;off<padLen;off+=64){
    for(let i=0;i<16;i++)w[i]=dv.getUint32(off+i*4);
    for(let i=16;i<64;i++){const s0=rotr(w[i-15],7)^rotr(w[i-15],18)^(w[i-15]>>>3),s1=rotr(w[i-2],17)^rotr(w[i-2],19)^(w[i-2]>>>10);w[i]=(w[i-16]+s0+w[i-7]+s1)|0;}
    let a=H[0],b=H[1],c=H[2],d=H[3],e=H[4],f=H[5],g=H[6],h=H[7];
    for(let i=0;i<64;i++){
      const S1=rotr(e,6)^rotr(e,11)^rotr(e,25),ch=(e&f)^(~e&g),t1=(h+S1+ch+K[i]+w[i])|0;
      const S0=rotr(a,2)^rotr(a,13)^rotr(a,22),maj=(a&b)^(a&c)^(b&c),t2=(S0+maj)|0;
      h=g;g=f;f=e;e=(d+t1)|0;d=c;c=b;b=a;a=(t1+t2)|0;
    }
    H[0]=(H[0]+a)|0;H[1]=(H[1]+b)|0;H[2]=(H[2]+c)|0;H[3]=(H[3]+d)|0;H[4]=(H[4]+e)|0;H[5]=(H[5]+f)|0;H[6]=(H[6]+g)|0;H[7]=(H[7]+h)|0;
  }
  return H.map(x=>(x>>>0).toString(16).padStart(8,"0")).join("").toUpperCase();
}
async function secMsGec(){
  /* Must match edge-tts DRM.generate_sec_ms_gec exactly: unix seconds rounded
     DOWN to the nearest 5 minutes, Windows file-time ticks, SHA-256 hex upper. */
  const unixSec=Math.floor(Date.now()/1000);
  const rounded=unixSec-(unixSec%300);
  const ticks=(BigInt(rounded)*10000000n+116444736000000000n).toString();
  const bytes=new TextEncoder().encode(ticks+TRUSTED_CLIENT_TOKEN);
  if(window.crypto&&crypto.subtle){
    const d=await crypto.subtle.digest("SHA-256",bytes);
    return Array.from(new Uint8Array(d)).map(b=>b.toString(16).padStart(2,"0")).join("").toUpperCase();
  }
  return sha256Bytes(bytes);
}
function uuid4(){return"xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g,c=>{const r=crypto.getRandomValues(new Uint8Array(1))[0]&15,v=c==="x"?r:(r&3|8);return v.toString(16);});}
function xTimestamp(){
  const d=new Date(),D=["Sun","Mon","Tue","Wed","Thu","Fri","Sat"],M=["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"],p=n=>String(n).padStart(2,"0");
  return `${D[d.getUTCDay()]} ${M[d.getUTCMonth()]} ${p(d.getUTCDate())} ${d.getUTCFullYear()} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())} GMT+0000 (Coordinated Universal Time)`;
}
function voiceById(id){return window.GOONJ_VOICES.find(v=>v.id===id);}
function voiceFullName(v){const p=v.id.split("-"),lang=p[0]+"-"+p[1];return `Microsoft Server Speech Text to Speech Voice (${lang}, ${p.slice(2).join("-")})`;}
function voiceLang(v){const p=v.id.split("-");return p[0]+"-"+p[1];}
function escapeXml(s){return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
const fmtPct=v=>(v>=0?"+":"")+v+"%";

/* ---------- expression markers -> SSML ---------- */
function textToSsmlBody(text,baseRate,basePitch){
  const tokens=text.split(/(\[\s*(?:sans|ruko|hansna|rona|ahista|tez|normal)\s*\])/gi);
  let rate=baseRate,pitch=basePitch,out="";
  for(const tok of tokens){
    const m=tok.match(/^\[\s*(sans|ruko|hansna|rona|ahista|tez|normal)\s*\]$/i);
    if(m){
      const k=m[1].toLowerCase();
      if(k==="sans")out+=`<break time="400ms"/>`;
      else if(k==="ruko")out+=`<break time="1200ms"/>`;
      else if(k==="ahista"){rate=-30;pitch=-5;}
      else if(k==="tez"){rate=35;pitch=5;}
      else if(k==="hansna"){rate=25;pitch=25;}
      else if(k==="rona"){rate=-20;pitch=-20;}
      else if(k==="normal"){rate=baseRate;pitch=basePitch;}
    }else if(tok){
      out+=`<prosody rate="${fmtPct(rate)}" pitch="${fmtPct(pitch)}">${escapeXml(tok)}</prosody>`;
    }
  }
  return out;
}
function buildSsml(text,voice,rate,pitch){
  const body=textToSsmlBody(text,rate,pitch);
  return `<speak version="1.0" xmlns="http://www.w3.org/2001/XMLSchema" xmlns:mstts="https://www.w3.org/2001/mstts" xml:lang="${voiceLang(voice)}"><voice name="${voiceFullName(voice)}">${body}</voice></speak>`;
}

/* ---------- one chunk over the wire ---------- */
function concatBytes(arr){let n=0;for(const a of arr)n+=a.length;const o=new Uint8Array(n);let p=0;for(const a of arr){o.set(a,p);p+=a.length;}return o;}
function synthesizeChunk(text,voice,rate,pitch){
  return (async()=>{
    const gec=await secMsGec(); // computed per instructions; browser WS cannot set custom headers, handshake carries the page Origin (verified 101 earlier today)
    const connId=uuid4().replace(/-/g,""),reqId=uuid4().replace(/-/g,"");
    const url=`${EDGE_WSS}?TrustedClientToken=${TRUSTED_CLIENT_TOKEN}&Sec-MS-GEC=${gec}&Sec-MS-GEC-Version=1-143.0.3650.75&ConnectionId=${connId}`;
    const ts=xTimestamp(),ssml=buildSsml(text,voice,rate,pitch);
    const configMsg=`X-Timestamp:${ts}\r\nContent-Type:application/json; charset=utf-8\r\nPath:speech.config\r\n\r\n{"context":{"synthesis":{"audio":{"metadataoptions":{"sentenceBoundaryEnabled":"false","wordBoundaryEnabled":"false"},"outputFormat":"${OUTPUT_FORMAT}"}}}}`;
    const ssmlMsg=`X-RequestId:${reqId}\r\nContent-Type:application/ssml+xml\r\nPath:ssml\r\n\r\n${ssml}`;
    return new Promise((resolve,reject)=>{
      let done=false,timer=null;const chunks=[];
      const fail=msg=>{if(!done){done=true;clearTimeout(timer);try{ws.close();}catch(e){}reject(new Error(msg));}};
      const ws=new WebSocket(url);ws.binaryType="arraybuffer";
      timer=setTimeout(()=>fail("Timed out waiting for audio (60s)."),60000);
      ws.onopen=()=>{try{ws.send(configMsg);ws.send(ssmlMsg);}catch(e){fail("Failed to send synthesis request: "+e.message);}};
      ws.onmessage=ev=>{
        if(done)return;const data=ev.data;
        if(typeof data==="string"){
          if(data.includes("Path:turn.end")){done=true;clearTimeout(timer);try{ws.close();}catch(e){}
            const audio=concatBytes(chunks);
            if(audio.length<500)reject(new Error("Server ended the turn without audio."));
            else resolve(audio);
          }
        }else{
          const buf=new Uint8Array(data);if(buf.length<2)return;
          const hlen=(buf[0]<<8)|buf[1];if(buf.length<2+hlen)return;
          const htext=new TextDecoder().decode(buf.subarray(2,2+hlen));
          if(htext.includes("Path:audio"))chunks.push(buf.slice(2+hlen));
          else if(htext.includes("Path:turn.end")){done=true;clearTimeout(timer);try{ws.close();}catch(e){}
            const audio=concatBytes(chunks);
            if(audio.length<500)reject(new Error("Server ended the turn without audio."));
            else resolve(audio);
          }
        }
      };
      ws.onerror=()=>fail("WebSocket error — the speech service refused the connection.");
      ws.onclose=e=>{if(!done)fail(`Connection closed before audio completed (code ${e.code}).`);};
    });
  })();
}
async function withRetry(fn,tries){let last;for(let i=0;i<tries;i++){try{return await fn();}catch(e){last=e;await new Promise(r=>setTimeout(r,800*(i+1)));}}throw last;}

/* ---------- chunking ---------- */
function chunkText(text,limit){
  const sentences=text.match(/[^.!?…؟。\n]+[.!?…؟。\n]+|[^.!?…؟。\n]+$/g)||[text];
  const chunks=[];let cur="";
  for(const s of sentences){
    if((cur+s).length<=limit){cur+=s;continue;}
    if(cur.trim())chunks.push(cur);
    if(s.length>limit){for(let i=0;i<s.length;i+=limit)chunks.push(s.slice(i,i+limit));cur="";}
    else cur=s;
  }
  if(cur.trim())chunks.push(cur);
  return chunks;
}
function mp3Check(u8){
  if(u8.length<1000)return{ok:false,frames:0};
  let frames=0;
  for(let i=0;i<u8.length-1;i++){if(u8[i]===0xFF&&(u8[i+1]&0xE0)===0xE0){frames++;i+=400;}}
  return{ok:frames>2,frames};
}
function fmtDur(s){if(!isFinite(s))return"?:??";const m=Math.floor(s/60),ss=Math.round(s%60);return m+":"+String(ss).padStart(2,"0");}
function audioDuration(url){return new Promise(res=>{const a=new Audio();a.preload="metadata";a.onloadedmetadata=()=>res(a.duration);a.onerror=()=>res(NaN);a.src=url;});}

/* ---------- UI ---------- */
function fillVoiceSelect(sel,defId){
  const langs={};for(const v of window.GOONJ_VOICES){(langs[v.language]=langs[v.language]||[]).push(v);}
  for(const lang of Object.keys(langs).sort()){
    const og=document.createElement("optgroup");og.label=lang;
    for(const v of langs[lang]){const o=document.createElement("option");o.value=v.id;const short=v.id.split("-").slice(2).join("-").replace(/Neural$/,"");o.textContent=`${short} (${v.gender})`;if(v.id===defId)o.selected=true;og.appendChild(o);}
    sel.appendChild(og);
  }
}
function setStatus(el,msg,isErr){el.textContent=msg;el.classList.toggle("err",!!isErr);}
async function finishResult(parts,ids){
  const merged=concatBytes(parts),check=mp3Check(merged);
  if(!check.ok)throw new Error("Downloaded bytes failed the MP3 frame check.");
  const blob=new Blob([merged],{type:"audio/mpeg"}),url=URL.createObjectURL(blob);
  const player=$(ids.player),link=$(ids.link);
  player.src=url;link.href=url;
  const dur=await audioDuration(url);
  $(ids.info).textContent=`MP3 • ${(merged.length/1024).toFixed(1)} KB • ${fmtDur(dur)} • MP3 frames verified (${check.frames})`;
  $(ids.result).classList.remove("hidden");
  return{bytes:merged.length,seconds:dur,frames:check.frames};
}
let busy=false;
async function onGenerateStudio(){
  if(busy)return;const text=$("textInput").value.trim();
  if(!text){setStatus($("status"),"Please enter some text first.",true);return;}
  const voice=voiceById($("voiceSelect").value),rate=+$("rateRange").value,pitch=+$("pitchRange").value;
  const chunks=chunkText(text,CHUNK_LIMIT),parts=[];
  busy=true;$("generateBtn").disabled=true;$("result").classList.add("hidden");
  $("progressWrap").classList.remove("hidden");
  try{
    for(let i=0;i<chunks.length;i++){
      $("progressLabel").textContent=`Chunk ${i+1} of ${chunks.length}…`;
      $("progressBar").style.width=Math.round(i/chunks.length*100)+"%";
      setStatus($("status"),`Synthesizing chunk ${i+1} of ${chunks.length}…`);
      parts.push(await withRetry(()=>synthesizeChunk(chunks[i],voice,rate,pitch),3));
      $("progressBar").style.width=Math.round((i+1)/chunks.length*100)+"%";
    }
    const r=await finishResult(parts,{player:"player",link:"downloadLink",info:"fileInfo",result:"result"});
    setStatus($("status"),`Done — ${chunks.length} chunk(s) merged into one MP3 (${(r.bytes/1024).toFixed(1)} KB).`);
  }catch(e){setStatus($("status"),"Failed: "+e.message,true);}
  finally{busy=false;$("generateBtn").disabled=false;}
}
function parsePodcast(script){
  const segs=[];
  for(const ln of script.split("\n")){
    const m=ln.match(/^\s*(?:speaker\s*)?([12])\s*:\s*(.+?)\s*$/i);
    if(!m)continue;const sp=+m[1],text=m[2],last=segs[segs.length-1];
    if(last&&last.speaker===sp&&(last.text+" "+text).length<=CHUNK_LIMIT)last.text+=" "+text;
    else segs.push({speaker:sp,text});
  }
  return segs;
}
async function onGeneratePodcast(){
  if(busy)return;const segs=parsePodcast($("podScript").value);
  if(!segs.length){setStatus($("podStatus"),"Add at least one 'Speaker 1:' / 'Speaker 2:' line.",true);return;}
  const v1=voiceById($("podVoice1").value),v2=voiceById($("podVoice2").value),parts=[];
  busy=true;$("podGenerate").disabled=true;$("podResult").classList.add("hidden");
  $("podProgressWrap").classList.remove("hidden");
  try{
    for(let i=0;i<segs.length;i++){
      $("podProgressLabel").textContent=`Segment ${i+1} of ${segs.length} (Speaker ${segs[i].speaker})…`;
      $("podProgressBar").style.width=Math.round(i/segs.length*100)+"%";
      setStatus($("podStatus"),`Synthesizing segment ${i+1} of ${segs.length} (Speaker ${segs[i].speaker})…`);
      parts.push(await withRetry(()=>synthesizeChunk(segs[i].text,segs[i].speaker===1?v1:v2,0,0),3));
      $("podProgressBar").style.width=Math.round((i+1)/segs.length*100)+"%";
    }
    const r=await finishResult(parts,{player:"podPlayer",link:"podDownloadLink",info:"podFileInfo",result:"podResult"});
    setStatus($("podStatus"),`Done — ${segs.length} segment(s) merged into one MP3 (${(r.bytes/1024).toFixed(1)} KB).`);
  }catch(e){setStatus($("podStatus"),"Failed: "+e.message,true);}
  finally{busy=false;$("podGenerate").disabled=false;}
}
document.addEventListener("DOMContentLoaded",()=>{
  fillVoiceSelect($("voiceSelect"),"ur-PK-AsadNeural");
  fillVoiceSelect($("podVoice1"),"en-US-GuyNeural");
  fillVoiceSelect($("podVoice2"),"en-US-AriaNeural");
  document.querySelectorAll(".tab").forEach(t=>t.addEventListener("click",()=>{
    document.querySelectorAll(".tab").forEach(x=>x.classList.remove("active"));t.classList.add("active");
    document.querySelectorAll(".panel").forEach(p=>p.classList.add("hidden"));
    $("tab-"+t.dataset.tab).classList.remove("hidden");
  }));
  const bind=(r,v)=>{$(r).addEventListener("input",()=>{$(v).textContent=$(r).value+"%";});};
  bind("rateRange","rateVal");bind("pitchRange","pitchVal");
  document.querySelectorAll("[data-m]").forEach(b=>b.addEventListener("click",()=>{
    const ta=$("textInput"),m=b.getAttribute("data-m"),s=ta.selectionStart??ta.value.length,e=ta.selectionEnd??s;
    ta.value=ta.value.slice(0,s)+" "+m+" "+ta.value.slice(e);ta.focus();
  }));
  $("sampleUrdu").addEventListener("click",()=>{$("voiceSelect").value="ur-PK-AsadNeural";$("textInput").value="Assalam o Alaikum! Yeh Goonj ka test hai. [sans] Goonj ab aap ki awaaz ban sakti hai.";});
  $("sampleEnglish").addEventListener("click",()=>{$("voiceSelect").value="en-US-AriaNeural";$("textInput").value="Hello! This is a test of Goonj, the free voice studio. Everything runs right here in your browser.";});
  $("generateBtn").addEventListener("click",onGenerateStudio);
  $("podSample").addEventListener("click",()=>{$("podScript").value="Speaker 1: Assalam o Alaikum and welcome to the Goonj podcast!\nSpeaker 2: Thank you! Today we are testing dual-speaker voices. [sans] It sounds quite natural.\nSpeaker 1: It really does. [ruko] Let us hear how the second voice responds.\nSpeaker 2: I am the second speaker, and I approve this message.";});
  $("podGenerate").addEventListener("click",onGeneratePodcast);
});

