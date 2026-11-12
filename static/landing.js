"use strict";
(() => {
  const body = document.body;
  const canvas = document.getElementById("glass-engine");
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  const chapters = [
    { caption: "A complete picture begins with evidence.", stage: "Evidence connected", at: 1200 },
    { caption: "Nine specialists. One shared understanding.", stage: "Checks complete", at: 6500 },
    { caption: "Written policy. A considered outcome.", stage: "Decision recorded", at: 12000 },
  ];
  const poster = new URLSearchParams(location.search).has("poster");
  let playing = !reduced.matches && !poster;
  let offset = playing ? 0 : 12000;
  let started = performance.now(), frame = 0, lastDraw = -1000, lastChapter = -1;
  let gl, program, sizeUniform, timeUniform, lost = false, visibilityPlaying = false;

  const vertex = `
    attribute vec2 position;
    void main(){gl_Position=vec4(position,0.0,1.0);}
  `;
  const fragment = `
    precision highp float;
    uniform vec2 resolution;
    uniform float time;
    mat2 turn(float a){float s=sin(a),c=cos(a);return mat2(c,-s,s,c);}
    float softMin(float a,float b,float k){float h=clamp(.5+.5*(b-a)/k,0.,1.);return mix(b,a,h)-k*h*(1.-h);}
    float ring(vec3 p,float radius,float thick){return length(vec2(length(p.xy)-radius,p.z))-thick;}
    float roundedRing(vec3 p){vec2 q=abs(p.xy);float r=pow(pow(q.x,3.2)+pow(q.y,3.2),1./3.2);return length(vec2(r-1.48,p.z))-.135;}
    float field(vec3 p){
      p.xy=turn(-.38+sin(time*.15)*.09)*p.xy;
      p.xz=turn(.42+time*.075)*p.xz;
      vec3 a=p;a.yz=turn(.18)*a.yz;
      float d=roundedRing(a);
      vec3 b=p;b.yz=turn(1.17+sin(time*.12)*.14)*b.yz;b.xy=turn(.75)*b.xy;
      d=softMin(d,ring(b,1.37,.10),.10);
      vec3 c=p;c.xz=turn(1.22)*c.xz;c.xy=turn(-.42+time*.035)*c.xy;
      return softMin(d,ring(c,1.67,.065),.07);
    }
    vec3 normalAt(vec3 p){vec2 e=vec2(.0013,0.);return normalize(vec3(field(p+e.xyy)-field(p-e.xyy),field(p+e.yxy)-field(p-e.yxy),field(p+e.yyx)-field(p-e.yyx)));}
    vec3 environment(vec3 r){
      vec3 c=mix(vec3(.35,.52,.72),vec3(.94,.98,1.),smoothstep(-.8,.8,r.y));
      float strip=pow(max(0.,dot(r,normalize(vec3(-.7,1.5,1.)))),34.);
      float line=pow(max(0.,1.-abs(r.x*.72+r.y*.18-.28)),42.);
      c+=vec3(.8,.86,.93)*strip+vec3(.46,.57,.7)*line;
      c*=.7+.3*smoothstep(-.5,.7,r.z);
      return c;
    }
    void main(){
      vec2 uv=(gl_FragCoord.xy*2.-resolution)/min(resolution.x,resolution.y);
      uv.y+=.04;
      vec3 origin=vec3(0.,.04,5.6),ray=normalize(vec3(uv*1.03,-3.1));
      float distance=0.;bool hit=false;
      for(int i=0;i<86;i++){float d=field(origin+ray*distance);if(d<.0016){hit=true;break;}distance+=d*.87;if(distance>10.)break;}
      if(!hit){gl_FragColor=vec4(0.);return;}
      vec3 point=origin+ray*distance,n=normalAt(point);
      vec3 reflected=reflect(ray,n),transmitted=refract(ray,n,.72);
      float facing=max(0.,dot(-ray,n));
      float fresnel=.13+.87*pow(1.-facing,3.5);
      vec3 reflection=environment(reflected),refraction=environment(transmitted)*vec3(.79,.89,1.);
      vec3 color=mix(refraction,reflection,fresnel*.84+.13);
      float key=pow(max(0.,dot(n,normalize(vec3(-.5,1.2,2.)))),65.);
      float rim=pow(1.-facing,7.);
      color+=vec3(.8,.88,1.)*key*.7+vec3(.3,.42,.6)*rim*.32;
      color=mix(color,vec3(.93,.97,1.),.08);
      gl_FragColor=vec4(color,.83+.15*fresnel);
    }
  `;

  function shader(type, source) {
    const item = gl.createShader(type);
    gl.shaderSource(item, source); gl.compileShader(item);
    if (!gl.getShaderParameter(item, gl.COMPILE_STATUS)) { gl.deleteShader(item); return null; }
    return item;
  }
  function setup() {
    try {
      gl = canvas.getContext("webgl", { alpha: true, antialias: true, premultipliedAlpha: false, powerPreference: "low-power" });
      if (!gl) return;
      const vs = shader(gl.VERTEX_SHADER, vertex), fs = shader(gl.FRAGMENT_SHADER, fragment);
      if (!vs || !fs) return;
      program = gl.createProgram(); gl.attachShader(program, vs); gl.attachShader(program, fs); gl.linkProgram(program);
      if (!gl.getProgramParameter(program, gl.LINK_STATUS)) { program = null; return; }
      gl.useProgram(program);
      const buffer = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1,1,-1,-1,1,-1,1,1,-1,1,1]), gl.STATIC_DRAW);
      const position = gl.getAttribLocation(program, "position");
      gl.enableVertexAttribArray(position); gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);
      sizeUniform = gl.getUniformLocation(program, "resolution"); timeUniform = gl.getUniformLocation(program, "time");
      body.classList.add("webgl-ready"); resize();
    } catch (_) { program = null; body.classList.remove("webgl-ready"); }
  }
  function resize() {
    if (!gl || !program || lost) return;
    const rect = canvas.getBoundingClientRect();
    const density = Math.min(devicePixelRatio || 1, 1.25, 1050 / Math.max(rect.width, rect.height));
    canvas.width = Math.max(1, Math.round(rect.width * density)); canvas.height = Math.max(1, Math.round(rect.height * density));
    gl.viewport(0, 0, canvas.width, canvas.height);
    gl.uniform2f(sizeUniform, canvas.width, canvas.height); draw(offset);
  }
  function draw(ms) {
    if (!gl || !program || lost) return;
    gl.uniform1f(timeUniform, ms / 1000);
    gl.clearColor(0, 0, 0, 0); gl.clear(gl.COLOR_BUFFER_BIT); gl.drawArrays(gl.TRIANGLES, 0, 6);
  }
  function elapsed(now = performance.now()) { return offset + (playing ? now - started : 0); }
  function updateChapter(ms) {
    const index = ms < 4800 ? 0 : ms < 9700 ? 1 : 2;
    if (index !== lastChapter) {
      lastChapter = index; body.dataset.chapter = String(index);
      document.getElementById("scene-index").textContent = `0${index + 1} / 03`;
      document.getElementById("scene-caption").textContent = chapters[index].caption;
      document.getElementById("dossier-stage").textContent = chapters[index].stage;
      document.querySelectorAll("[data-chapter]").forEach(element => {
        if (element.tagName === "BUTTON") element.setAttribute("aria-pressed", String(Number(element.dataset.chapter) === index));
      });
    }
    body.classList.toggle("scene-reset", playing && ms > 15500);
  }
  function updateMotionControl() {
    document.getElementById("motion-toggle").setAttribute("aria-pressed", String(!playing));
    document.getElementById("motion-label").textContent = playing ? "Pause motion" : "Play motion";
    document.getElementById("motion-symbol").textContent = playing ? "Ⅱ" : "▷";
  }
  function tick(now) {
    frame = 0;
    if (playing) {
      let ms = elapsed(now);
      if (ms >= 16000) { offset = 0; started = now; ms = 0; }
      updateChapter(ms);
      if (now - lastDraw > 32) { draw(ms); lastDraw = now; }
      frame = requestAnimationFrame(tick);
    }
  }
  function play() { if (playing && frame) return; started = performance.now(); playing = true; updateMotionControl(); frame = requestAnimationFrame(tick); }
  function pause() { offset = elapsed(); playing = false; cancelAnimationFrame(frame); frame = 0; updateMotionControl(); updateChapter(offset); draw(offset); }
  document.getElementById("motion-toggle").addEventListener("click", () => playing ? pause() : play());
  document.getElementById("process-replay").addEventListener("click", () => { cancelAnimationFrame(frame); frame = 0; offset = 0; started = performance.now(); lastChapter = -1; playing = false; updateChapter(0); play(); });
  document.querySelectorAll("button[data-chapter]").forEach(button => button.addEventListener("click", () => {
    pause(); offset = chapters[Number(button.dataset.chapter)].at; updateChapter(offset); draw(offset);
  }));
  reduced.addEventListener("change", event => { if (event.matches) { pause(); offset = 12000; updateChapter(offset); draw(offset); } });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { visibilityPlaying = playing; if (playing) pause(); }
    else if (visibilityPlaying) { visibilityPlaying = false; play(); }
  });
  canvas.addEventListener("webglcontextlost", event => { event.preventDefault(); lost = true; body.classList.remove("webgl-ready"); });
  canvas.addEventListener("webglcontextrestored", () => { lost = false; setup(); draw(elapsed()); });
  window.addEventListener("resize", resize);
  window.addEventListener("pagehide", () => { offset = elapsed(); started = performance.now(); cancelAnimationFrame(frame); frame = 0; });
  window.addEventListener("pageshow", event => { if (event.persisted && playing) { started = performance.now(); frame = requestAnimationFrame(tick); } });
  setup(); updateChapter(offset); updateMotionControl(); draw(offset);
  if (playing) frame = requestAnimationFrame(tick);
})();
