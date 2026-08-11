// CANopen Bench frontend — pixel-faithful port of the Claude Design prototype.
// Server (FastAPI) owns all bench state; this file renders it and sends actions.
import { html, render, useState, useEffect, useMemo, useRef } from '/static/vendor/preact-htm.module.js';

const MONO = "'IBM Plex Mono',monospace";

function send(action, params = {}) {
  return fetch('/api/action', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action, params }),
  });
}

function useServerState() {
  const [st, setSt] = useState(null);
  useEffect(() => {
    let ws, alive = true;
    const connect = () => {
      ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws');
      ws.onmessage = (e) => {
        const m = JSON.parse(e.data);
        if (m.type === 'state') setSt(m.state);
      };
      ws.onclose = () => { if (alive) setTimeout(connect, 1000); };
      ws.onerror = () => ws.close();
    };
    connect();
    return () => { alive = false; if (ws) ws.close(); };
  }, []);
  return st;
}

// Input that holds the user's text while focused, syncs from the server otherwise,
// and commits on blur / Enter (the state snapshot re-renders every tick).
// After a commit the field keeps showing the typed text until the server echoes
// the new value — resyncing immediately would flash the stale value for the
// duration of the action round-trip. If the server never adopts the input
// (invalid text), the field reverts to the server value after a grace period.
function SyncInput({ value, onCommit, style, klass, title, placeholder, width }) {
  const [focused, setFocused] = useState(false);
  const [val, setVal] = useState(value ?? '');
  const refs = useRef({ prop: value, prev: value, holdUntil: 0, focused: false }).current;
  refs.prop = value;
  useEffect(() => {
    const changed = refs.prev !== value;
    refs.prev = value;
    if (focused) return;
    if (!changed && Date.now() < refs.holdUntil) return; // just committed: wait for the echo
    setVal(value ?? '');
  }, [value, focused]);
  return html`<input
    value=${val} title=${title} placeholder=${placeholder} class=${klass} style=${style}
    onFocus=${() => { refs.focused = true; setFocused(true); }}
    onInput=${(e) => setVal(e.target.value)}
    onKeyDown=${(e) => { if (e.key === 'Enter') e.target.blur(); }}
    onBlur=${(e) => {
      refs.focused = false;
      setFocused(false);
      if (onCommit && e.target.value !== (value ?? '')) {
        refs.holdUntil = Date.now() + 1200;
        onCommit(e.target.value);
        setTimeout(() => { if (!refs.focused) { refs.holdUntil = 0; setVal(refs.prop ?? ''); } }, 1200);
      }
    }} />`;
}

const cbStyle = (on, extra = '') =>
  `width:13px;height:13px;border-radius:3px;border:1.5px solid ${on ? 'var(--acc)' : 'var(--inp)'};` +
  `background:${on ? 'var(--acc)' : 'transparent'};color:#fff;font-size:9px;display:grid;place-items:center;${extra}`;
const Cb = (on, extra) => html`<span style=${cbStyle(on, extra)}>${on ? '✓' : ''}</span>`;

const ledFor = (nmt) =>
  nmt === 'Operational' ? '#4ecb71' : nmt === 'Emergency' ? '#ff5252' : nmt === 'Stopped' ? '#98a1af' : '#e8b23a';

const PAGES = [['setup', 'ST', 'Setup'], ['objects', 'OB', 'Objects'], ['tests', 'TS', 'Tests'], ['swdl', 'DL', 'SWDL'], ['trace', 'TR', 'Trace']];

const btn = {
  acc: 'border:1px solid var(--acc-bd);background:var(--acc-soft);color:var(--acc);font-weight:600;',
  ghost: 'border:1px solid var(--inp);color:var(--mid);font-weight:600;',
};

// Bench power supply (canopen_bench/instruments): shown once one has been
// found. Channel count comes from the instrument — the same model line
// exists with one output and with two — so the row is drawn from the data,
// never from a fixed pair of boxes. Values are the supply's *set* values;
// the label says so, because a set voltage is not a measurement.
function PsuBox({ psu }) {
  const fieldStyle = `border:1px solid var(--inp);background:var(--panel);color:var(--tx);font:11px ${MONO};border-radius:5px;padding:4px 7px;outline:none;width:64px`;
  const head = (t) => html`<span style="font-size:10.5px;color:var(--dim);font-weight:600">${t}</span>`;
  // A search that found nothing is still "no supply", so it belongs in this
  // box and not in the one below: that one is built for a supply that
  // answered, and without `found` it offered an output toggle and a refresh
  // for a device that is not there — while the one control that helps, the
  // search, was the one it left out. The only way back out was Release,
  // labelled as handing the port back, which is not what a reader looks for
  // when nothing was ever connected.
  if (!psu || !psu.found) {
    return html`
    <div style="grid-column:1/-1;background:var(--panel);border:1px solid var(--bd);border-radius:8px;padding:10px 16px;display:flex;align-items:center;gap:10px">
      <span style="font-weight:600;font-size:12px;flex:none">Power supply</span>
      <span style="flex:none;font:600 10px ${MONO};background:var(--chip);color:var(--faint);padding:2px 8px;border-radius:9px">NONE</span>
      <span class="hv" onClick=${() => send('psu_search')} style="${btn.ghost}font-size:11.5px;padding:5px 12px;border-radius:6px;cursor:pointer;flex:none">Search…</span>
      ${psu && psu.error
        ? html`<span style="font-size:11px;color:var(--red);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${psu.error}</span>`
        : html`<span style="font-size:10.5px;color:var(--faint);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
        Only ports whose description a driver recognises are opened — a serial port might be the CAN adapter.</span>`}
    </div>`;
  }
  const on = psu.output;
  return html`
  <div style="grid-column:1/-1;background:var(--panel);border:1px solid var(--bd);border-radius:8px;padding:10px 16px;display:flex;flex-direction:column;gap:10px">
    <div style="display:flex;align-items:center;gap:10px;min-width:0">
      <span style="font-weight:600;font-size:12px;flex:none">Power supply</span>
      <span title=${psu.raw || ''} style="flex:none;font:600 11px ${MONO};color:var(--tx)">${psu.model || psu.name}</span>
      <span style="flex:none;font:10.5px ${MONO};color:var(--faint)">${psu.port}${psu.sn ? ` · SN ${psu.sn}` : ''}${psu.fw ? ` · ${psu.fw}` : ''}</span>
      <span class="hv-chip" onClick=${() => send('psu_output', { on: !on })}
        style="${on ? btn.acc : btn.ghost}font-size:11.5px;padding:4px 12px;border-radius:6px;cursor:pointer;flex:none">
        Output ${on === null ? '?' : on ? 'on' : 'off'}</span>
      <span class="hv-white" title="Read the supply — nothing here polls" onClick=${() => send('psu_refresh')} style="color:var(--faint);cursor:pointer;flex:none">⟳</span>
      <label onClick=${() => send('psu_sidebar_toggle')} title="A second, smaller box in the sidebar: the set values, output and a refresh — no port, no serial number, those stay here"
        style="display:flex;align-items:center;gap:7px;cursor:pointer;font-size:11.5px;color:var(--dim);flex:none;margin-left:auto">
        ${Cb(psu.sidebar, 'flex:none;')}Quick access</label>
      <span class="hv" onClick=${() => send('psu_release')} title="Hand the serial port back to the system"
        style="${btn.ghost}font-size:11px;padding:4px 10px;border-radius:6px;cursor:pointer;flex:none">Release</span>
    </div>
    ${psu.error && html`<div style="font-size:11px;color:var(--red)">${psu.error}</div>`}
    <div style="display:flex;gap:18px;flex-wrap:wrap">
      ${(psu.channels || []).map((c, i) => html`
        <div style="display:flex;align-items:center;gap:8px">
          ${head(`CH ${i + 1}`)}
          <${SyncInput} value=${String(c.volt)} style=${fieldStyle} title=${`set voltage${c.limit ? ` · the supply reports a limit of ${c.limit} V` : ''}`}
            onCommit=${(v) => send('psu_set', { ch: i + 1, volt: v })} />
          <span style="font-size:11px;color:var(--dim)">V</span>
          <${SyncInput} value=${String(c.curr)} style=${fieldStyle} title="set current"
            onCommit=${(v) => send('psu_set', { ch: i + 1, curr: v })} />
          <span style="font-size:11px;color:var(--dim)">A</span>
        </div>`)}
      <span style="font-size:10.5px;color:var(--faint);align-self:center">set values, read from the supply</span>
    </div>
    ${(psu.channels || []).some((c) => c.mvolt !== null && c.mvolt !== undefined) && html`
    <div style="display:flex;gap:18px;flex-wrap:wrap;align-items:center">
      ${(psu.channels || []).map((c, i) => html`
        <div style="display:flex;align-items:center;gap:8px">
          <span style="font-size:10.5px;color:var(--dim);font-weight:600">CH ${i + 1}</span>
          <span style="font:11px ${MONO};color:var(--tx)">${c.mvolt ?? '—'} V</span>
          <span style="font:11px ${MONO};color:var(--tx)">${c.mcurr ?? '—'} A</span>
        </div>`)}
      <span style="font-size:10.5px;color:var(--faint)">measured at the terminals</span>
    </div>`}
  </div>`;
}

// The supply in the sidebar, when Setup says so: what a run needs to see
// without leaving the page it is watching. Set values, not measured ones —
// the refresh is there for somebody who turned a knob on the instrument and
// wants the bench to agree, and a knob moves a set value. Everything else
// about the supply (port, serial, firmware, Release) stays in Setup.
function psuBox(psu) {
  const chans = psu.channels || [];
  const on = psu.output;
  const fieldStyle = `border:1px solid var(--inp);background:var(--panel);color:var(--tx);font:600 12px ${MONO};border-radius:4px;padding:2px 5px;outline:none;width:52px;text-align:right`;
  // The same set values the Setup box edits, and edited the same way: a
  // supply worth watching from here is one worth nudging from here, and
  // walking to Setup to change 57 V to 55 V is the walk this box exists
  // to save.
  const field = (c, i, key, unit) => html`
    <div style="display:flex;align-items:center;gap:4px">
      <${SyncInput} value=${String(c[key] ?? '')} style=${fieldStyle}
        title=${`set ${key === 'volt' ? 'voltage' : 'current'}${chans.length > 1 ? ` · channel ${i + 1}` : ''}`}
        onCommit=${(v) => send('psu_set', { ch: i + 1, [key]: v })} />
      <span style="font-size:10px;color:var(--dim);width:8px">${unit}</span>
    </div>`;
  // Two channels stand side by side, each with its volts over its amps; a
  // single channel has the room to put them next to each other instead.
  // Each column is left-aligned within itself but sits off the box edge —
  // a value pinned to the border reads as the start of the box rather than
  // the start of a column.
  const channel = (c, i) => html`
    <div style="min-width:0;display:flex;flex-direction:column;gap:4px">
      ${chans.length > 1 && html`<span style="font:600 9.5px ${MONO};color:var(--faint)">CH ${i + 1}</span>`}
      <div style="display:flex;${chans.length > 1 ? 'flex-direction:column;gap:4px' : 'align-items:center;gap:12px'}">
        ${field(c, i, 'volt', 'V')}${field(c, i, 'curr', 'A')}
      </div>
    </div>`;
  return html`
  <div style="margin:8px 10px 0;background:var(--sb-box);border:1px solid var(--sb-bd);border-radius:8px;overflow:hidden">
    <div style="display:flex;justify-content:space-between;align-items:center;gap:6px;padding:7px 10px;border-bottom:1px solid var(--sb-bd)">
      <span style="font-weight:600;color:var(--tx);font-size:12px;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${psu.model || psu.name}</span>
      <span style="display:flex;align-items:center;gap:8px;flex:none">
        <span class="hv-chip" onClick=${() => send('psu_output', { on: !on })}
          title=${`Output is ${on === null || on === undefined ? 'unknown' : on ? 'on' : 'off'} — click to turn it ${on ? 'off' : 'on'}`}
          style="display:grid;place-items:center;width:22px;height:18px;border-radius:5px;cursor:pointer;font-size:11px;border:1px solid ${on ? 'var(--grn)' : 'var(--inp)'};background:${on ? 'var(--grn)' : 'transparent'};color:${on ? '#fff' : 'var(--dim)'}">⏻</span>
        <span class="hv-white" onClick=${() => send('psu_refresh')} title="Read the supply — nothing here polls"
          style="color:var(--faint);cursor:pointer">⟳</span>
      </span>
    </div>
    ${psu.error
      ? html`<div style="padding:8px 10px;font-size:11px;color:var(--red)">${psu.error}</div>`
      : html`<div style="display:flex;justify-content:space-around;gap:10px;padding:9px 8px">${chans.map(channel)}</div>`}
  </div>`;
}

// Plugin-contributed sidebar panel (canopen_bench/plugin.py, DevicePanel):
// a declarative box the core renders without knowing the device family it
// belongs to. Every part is optional — buttons without a canvas, LEDs
// alone, any combination.
const PANEL_SLOTS = [['tl', 'bl'], ['tr', 'br']];

// "fg"/"dim" resolve against the canvas foreground; anything else is a
// literal colour, because a physical display's own colours have no business
// following the page theme.
const panelInk = (c, fg) => (c === 'fg' || c === 'dim' || !c ? fg : c);
const panelOpacity = (c) => (c === 'dim' ? 0.38 : 1);

// a blinking element is a state of the device, not decoration — same
// vocabulary as an LED, so a panel says it once and means it everywhere
const panelAnim = (d) => (d.blink
  ? `animation:coPulse ${d.blink === 'fast' ? '.4s' : '1s'} infinite`
  : '');

const panelShape = (d, fg) => {
  const ink = panelInk(d.c, fg);
  const op = panelOpacity(d.c);
  const an = panelAnim(d);
  if (d.t === 'line') return html`<line x1=${d.p[0]} y1=${d.p[1]} x2=${d.p[2]} y2=${d.p[3]}
    stroke=${ink} stroke-width=${d.w || 1} stroke-linecap="round" opacity=${op} style=${an} />`;
  if (d.t === 'poly') return html`<polygon points=${d.p.join(' ')} opacity=${op} style=${an}
    fill=${d.fill ? ink : 'none'} stroke=${ink} stroke-width=${d.w || 1} />`;
  // "tl" squeezes the glyphs into exactly that width. A physical display
  // prints its legends into fixed cells, and the panel can only match that
  // if it can say so — font metrics are the browser's to pick, so a plugin
  // laying out a row by guessing them lands differently on another machine.
  if (d.t === 'text') {
    const fit = d.tl ? { textLength: d.tl, lengthAdjust: 'spacingAndGlyphs' } : {};
    return html`<text x=${d.x} y=${d.y} fill=${ink} opacity=${op} style=${an}
      font-size=${d.size || 9} font-family="IBM Plex Sans, sans-serif" ...${fit}>${d.s}</text>`;
  }
  return null;  // unknown primitive: skip it, never break the whole panel
};

// on: true lit · false dark · null NOT READABLE — a neutral ring, never
// the same as dark, so a missing capability can't read as a measurement
const panelLed = (l) => html`
  <span title=${l.title || ''} style="width:9px;height:9px;border-radius:50%;flex:none;
    background:${l.on ? l.c : 'transparent'};
    border:1.5px solid ${l.on === null ? 'var(--faint)' : l.c};
    opacity:${l.on === null ? 0.5 : 1};
    animation:${l.blink ? `coPulse ${l.blink === 'fast' ? '.4s' : '1s'} infinite` : 'none'}"></span>`;

function panelBox(p) {
  const fg = (p.canvas || {}).fg || '#1d2b14';
  const btns = p.buttons || [];
  const press = (b) => (e) => send(p.buttonAction, { node: p.node, btn: b.id, long: !!e.shiftKey });
  const column = (slots) => html`
    <div style="display:flex;flex-direction:column;gap:6px;justify-content:center">
      ${slots.map((slot) => btns.filter((b) => b.slot === slot).map((b) => html`
        <span class="hv-chip" onClick=${press(b)} title=${b.title || ''}
          style="width:23px;height:23px;display:grid;place-items:center;border:1px solid var(--inp);
                 border-radius:5px;color:var(--mid);font:600 12px 'IBM Plex Sans';cursor:pointer">${b.label}</span>`))}
    </div>`;

  return html`
  <div style="margin:0 10px 10px;background:var(--sb-box);border:1px solid var(--sb-bd);border-radius:8px;overflow:hidden">
    <div style="display:flex;justify-content:space-between;align-items:center;padding:7px 10px;border-bottom:1px solid var(--sb-bd)">
      <span style="font-weight:600;color:var(--tx);font-size:12px">${p.title} · Node ${String(p.node).padStart(2, '0')}</span>
      ${p.refresh && html`<span class="hv-white" title="Read the device — nothing here polls"
        onClick=${() => send(p.refresh, { node: p.node })} style="color:var(--faint);cursor:pointer">⟳</span>`}
    </div>
    <div style="display:flex;align-items:center;gap:6px;padding:8px 8px 4px">
      ${column(PANEL_SLOTS[0])}
      ${p.canvas && html`
        <svg viewBox=${`0 0 ${p.canvas.w} ${p.canvas.h}`} preserveAspectRatio="xMidYMid meet"
          style="flex:1;min-width:0;background:${p.canvas.bg || 'transparent'};border-radius:4px;
                 box-shadow:inset 0 2px 6px rgba(0,0,0,.35)">
          ${(p.canvas.draw || []).map((d) => panelShape(d, fg))}
        </svg>`}
      ${column(PANEL_SLOTS[1])}
    </div>
    ${p.caption && html`
      <div style="padding:0 10px 2px;text-align:center;font-size:10.5px;color:var(--dim)">${p.caption}</div>`}
    ${(p.leds || []).length > 0 && html`
      <div style="display:flex;gap:9px;justify-content:center;padding:4px 10px 9px">${p.leds.map(panelLed)}</div>`}
  </div>`;
}

// ---------------------------------------------------------------- sidebar --
function Sidebar({ s, ui, setUi }) {
  const selDevs = s.devices.filter((d) => d.sel);
  const adapterInfo = s.adapters.find((a) => a.key === s.adapter);
  const ifaceFoot = s.connected ? `${adapterInfo.foot} · ${s.bitrate} kbit/s` : 'interface offline';
  const mDev = s.connected ? s.devices.find((d) => d.node === ui.menuNode) : null;
  const closeMenu = () => setUi({ ...ui, menuNode: null });
  const menuAct = (what) => () => { send('dev_menu', { node: mDev.node, what }); closeMenu(); };
  // device commands are per-EDS data (special functions like a vendor's
  // SuperUser) — chips, badges and menu entries render from the registry
  const edsCmds = (file) => (s.eds.files.find((e) => e.file === file) || {}).device_commands || [];
  const devCmds = Object.values(Object.fromEntries(
    s.devices.flatMap((d) => edsCmds(d.eds)).map((c) => [c.key, c])));
  const menuActions = mDev ? [
    { code: 'RST', label: 'Restart device', fg: 'var(--tx)', go: menuAct('restart') },
    { code: 'NMT', label: 'Set Operational', fg: 'var(--tx)', go: menuAct('op') },
    { code: 'NMT', label: 'Set Pre-Operational', fg: 'var(--tx)', go: menuAct('preop') },
    { code: 'NMT', label: 'Reset communication', fg: 'var(--tx)', go: menuAct('resetcomm') },
    ...edsCmds(mDev.eds).map((c) => ({
      code: c.badge || 'CMD',
      label: (mDev.cmds || {})[c.key] ? `Deactivate ${c.label}` : `Activate ${c.label}…`,
      fg: 'var(--su)',
      go: () => { send('dev_cmd', { node: mDev.node, key: c.key }); closeMenu(); },
    })),
    { code: 'EDS', label: `EDS: ${mDev.eds} · change…`, fg: 'var(--acc)', go: menuAct('eds_next') },
  ] : [];

  const nmtChip = (label, cmd, extra = '') => html`
    <span class='hv-chip'
      onClick=${() => send('nmt', { cmd })}
      style="font:600 10px 'IBM Plex Sans';border:1px solid var(--inp);color:var(--mid);padding:3px 8px;border-radius:5px;cursor:pointer;${extra}">${label}</span>`;
  const cmdChip = (c) => html`
    <span class='hv-su' title=${`Toggle ${c.label} on the selected devices`}
      onClick=${() => send('dev_cmd', { key: c.key })}
      style="font:600 10px 'IBM Plex Sans';border:1px solid var(--su);color:var(--su);padding:3px 8px;border-radius:5px;cursor:pointer">${c.label}</span>`;

  return html`
  <div style="width:238px;flex:none;background:var(--sb-bg);color:var(--mid);border-right:1px solid var(--sb-bd);display:flex;flex-direction:column;min-height:0">
    <div style="padding:14px 16px 12px;border-bottom:1px solid var(--sb-bd)">
      <div style="font-weight:700;font-size:14px;color:var(--tx);letter-spacing:.02em">CANopen Bench</div>
      <div style="font:11px ${MONO};color:var(--faint);margin-top:2px">v${s.version} · workspace: ${s.workspace}</div>
    </div>
    <div style="padding:10px 10px 6px;display:flex;flex-direction:column;gap:2px">
      ${PAGES.map(([key, code, label]) => {
        const on = ui.page === key;
        return html`
        <div class=${on ? '' : 'hv-nav'} onClick=${() => setUi({ ...ui, page: key })}
          style="display:flex;align-items:center;gap:9px;padding:7px 10px;border-radius:6px;cursor:pointer;background:${on ? 'var(--acc)' : 'transparent'};color:${on ? '#fff' : 'var(--dim)'};font-weight:${on ? 600 : 400}">
          <span style="font:600 9px ${MONO};width:22px;height:16px;display:grid;place-items:center;border:1.5px solid currentColor;border-radius:3px;flex:none;opacity:.85">${code}</span>${label}
        </div>`;
      })}
    </div>

    ${s.psu && s.psu.sidebar && psuBox(s.psu)}

    <div style="margin:8px 10px;background:var(--sb-box);border:1px solid var(--sb-bd);border-radius:8px;overflow:visible;position:relative">
      <div style="display:flex;align-items:center;justify-content:space-between;padding:8px 10px;border-bottom:1px solid var(--sb-bd)">
        <span style="font-weight:600;color:var(--tx);font-size:12px">Devices <span style="font:10.5px ${MONO};color:var(--faint)">${s.connected ? `${selDevs.length}/${s.devices.length} sel` : '—'}</span></span>
        <span class="hv-scan" onClick=${() => send('scan')}
          style="font:600 10.5px 'IBM Plex Sans';background:var(--acc);color:#fff;padding:3px 10px;border-radius:5px;cursor:pointer">${s.scanBusy ? '…' : 'Scan'}</span>
      </div>
      <div style="display:flex;flex-direction:column;min-height:34px">
        ${!s.connected && html`<div style="padding:10px;color:var(--faint);font-size:11.5px">Connect the interface, then scan.</div>`}
        ${s.connected && s.devices.map((d) => html`
          <div class="hv-dk" style="display:flex;align-items:center;gap:8px;padding:6px 8px 6px 10px;background:${d.sel ? 'var(--sb-sel)' : 'transparent'};cursor:pointer">
            <span onClick=${() => send('dev_toggle', { node: d.node })}
              style="width:12px;height:12px;border-radius:3px;border:1.5px solid ${d.sel ? 'var(--acc)' : 'var(--inp)'};background:${d.sel ? 'var(--acc)' : 'transparent'};flex:none;display:grid;place-items:center;color:#fff;font-size:9px">${d.sel ? '✓' : ''}</span>
            <span onClick=${() => send('dev_toggle', { node: d.node })} style="font:11px ${MONO};color:var(--acc);width:20px">${String(d.node).padStart(2, '0')}</span>
            <span onClick=${() => send('dev_toggle', { node: d.node })} style="color:${d.sel ? 'var(--tx)' : 'var(--mid)'};flex:1;font-weight:${d.sel ? 600 : 400}">${d.name}</span>
            ${d.variant && html`<span title="Variant (auto-detected on scan)" style="font:600 8.5px ${MONO};background:var(--chip);color:var(--faint);padding:1px 4px;border-radius:3px">${d.variant}</span>`}
            ${edsCmds(d.eds).filter((c) => (d.cmds || {})[c.key]).map((c) => html`<span title=${c.label} style="font:600 8.5px ${MONO};background:var(--su-soft);color:var(--su);padding:1px 4px;border-radius:3px">${c.badge || c.key.toUpperCase()}</span>`)}
            <span title=${d.nmt} style="width:7px;height:7px;border-radius:50%;background:${ledFor(d.nmt)};flex:none;animation:${d.nmt === 'Emergency' ? 'coPulse 1s infinite' : 'none'}"></span>
            <span class="hv-white" onClick=${() => setUi({ ...ui, menuNode: ui.menuNode === d.node ? null : d.node })}
              style="color:var(--faint);padding:0 3px;cursor:pointer;font-weight:700">⋮</span>
          </div>`)}
      </div>
      <div style="padding:8px 10px;border-top:1px solid var(--sb-bd);display:flex;flex-wrap:wrap;gap:5px">
        ${nmtChip('Op', 'start')}${nmtChip('Pre-Op', 'preop')}${nmtChip('Stop', 'stop')}${nmtChip('Reset', 'reset')}${devCmds.map(cmdChip)}
      </div>
      ${mDev && html`
        <div style="position:absolute;left:6px;right:6px;top:38px;z-index:30;background:var(--sb-menu);border:1px solid var(--sb-bd);border-radius:8px;box-shadow:0 8px 24px rgba(0,0,0,.25);overflow:hidden">
          <div style="display:flex;align-items:center;justify-content:space-between;padding:8px 12px;border-bottom:1px solid var(--sb-bd);font-weight:600;color:var(--tx);font-size:12px">
            Node ${String(mDev.node).padStart(2, '0')} · ${mDev.name} · SN ${mDev.sn}
            <span class="hv-white" onClick=${closeMenu} style="cursor:pointer;color:var(--faint);padding:0 2px">✕</span>
          </div>
          ${menuActions.map((a) => html`
            <div class="hv-mrow" onClick=${a.go} style="padding:7px 12px;cursor:pointer;color:${a.fg};font-size:12px;display:flex;gap:8px;align-items:center">
              <span style="font:600 9px ${MONO};color:var(--faint);width:30px">${a.code}</span>${a.label}
            </div>`)}
        </div>`}
    </div>

    ${s.mirror && html`
    <div style="margin:0 10px 10px;background:var(--sb-box);border:1px solid var(--sb-bd);border-radius:8px;overflow:hidden">
      <div style="display:flex;justify-content:space-between;align-items:center;padding:7px 10px;border-bottom:1px solid var(--sb-bd)">
        <span style="font-weight:600;color:var(--tx);font-size:12px">Display · Node ${String(s.mirror.node).padStart(2, '0')}</span>
        <span class="hv-white" onClick=${() => send('mirror_refresh')} style="color:var(--faint);cursor:pointer">⟳</span>
      </div>
      <div style="margin:8px 10px 10px;background:#9fb98a;border-radius:4px;padding:8px 10px;color:#1d2b14;font:600 13px ${MONO};box-shadow:inset 0 2px 6px rgba(0,0,0,.35)">
        <div style="display:flex;justify-content:space-between;font-size:9.5px;font-weight:500">${s.mirror.values.map((v) => html`<span>${v.label}</span>`)}</div>
        <div style="display:flex;justify-content:space-between;font-size:20px;letter-spacing:.06em">${s.mirror.values.map((v) => html`<span>${v.value}</span>`)}</div>
      </div>
    </div>`}

    ${(s.panels || []).map(panelBox)}
    <div class=${ui.page === 'about' ? '' : 'hv-nav'} onClick=${() => setUi({ ...ui, page: 'about' })}
      style="margin:auto 10px 0;display:flex;align-items:center;gap:8px;padding:7px 10px;border-radius:6px;cursor:pointer;background:${ui.page === 'about' ? 'var(--acc)' : 'transparent'};color:${ui.page === 'about' ? '#fff' : 'var(--dim)'};font-size:12px">
      <span style="font:600 9px ${MONO};width:22px;height:16px;display:grid;place-items:center;border:1.5px solid currentColor;border-radius:3px;flex:none;opacity:.85">i</span>About
    </div>
    <div style="padding:10px 16px;border-top:1px solid var(--sb-bd);margin-top:8px;font:10.5px ${MONO};color:var(--faint);line-height:1.6">${ifaceFoot}<br/>${s.connected ? html`bus load ${s.busLoad.toFixed(1)}% · <span style="color:${s.errFrames ? 'var(--red)' : 'inherit'}">err ${s.errFrames}</span>` : '—'}</div>
  </div>`;
}

// ------------------------------------------------------------------ about --
function AboutPage() {
  const h = (t) => html`<div style="font-weight:600;font-size:12.5px;color:var(--tx);margin-top:4px">${t}</div>`;
  const link = (href, label) => html`<a href=${href} target="_blank" rel="noopener">${label || href}</a>`;
  return html`
  <div style="flex:1;overflow:auto;padding:16px 18px;display:grid;align-content:start">
    <div style="background:var(--panel);border:1px solid var(--bd);border-radius:8px;padding:18px 20px;max-width:640px;display:flex;flex-direction:column;gap:10px;font-size:12px;color:var(--mid);line-height:1.7">
      <div style="font-weight:700;font-size:15px;color:var(--tx)">CANopen Bench</div>
      <div>Web tool to test and control CANopen devices on a bench — scan, object access,
        system tests, firmware download and a live CAN trace, driven by the devices' EDS files.</div>
      ${h('Highlights')}
      <ul style="margin:0;padding-left:18px">
        <li>Scan with identity read (0x1018) and automatic EDS assignment</li>
        <li>Object dictionary browser with favorites, RAW SDO and last-known values</li>
        <li>Declarative system test cases (YAML) with reports and suites</li>
        <li>Trace with class filters, capture save/load and µs timestamps</li>
        <li>Demo mode without hardware; vendor features as extension packages</li>
      </ul>
      ${h('Documentation')}
      <ul style="margin:0;padding-left:18px">
        <li><span style="font-family:${MONO}">README.md</span> — what the tool does</li>
        <li><span style="font-family:${MONO}">IMPLEMENTATION.md</span> — architecture</li>
        <li><span style="font-family:${MONO}">docs/ablaeufe/</span> — operational sequences + test-case format</li>
        <li><span style="font-family:${MONO}">docs/extending.md</span> — writing plugin packages</li>
        <li><span style="font-family:${MONO}">examples/</span> — EDS + test-case examples</li>
      </ul>
      ${h('Author')}
      <div>Created by NobseVomBerg · ${link('https://unsix.de', 'unsix.de')} ·
        ${link('https://github.com/NobseVomBerg/CANopen-Bench', 'GitHub repository')}</div>
      ${h('License')}
      <div>Core application: MIT. Vendor extension packages: proprietary.</div>
    </div>
  </div>`;
}

// EDS row's variant-object config: index:sub to read on scan + value->label map.
function VariantEditor({ e, onClose }) {
  const [index, setIndex] = useState(e.variant_index || '');
  const [sub, setSub] = useState(e.variant_sub || '');
  const [pairs, setPairs] = useState(Object.entries(e.variant_map || {}));
  const fieldStyle = `border:1px solid var(--inp);background:var(--panel);color:var(--tx);font:11px ${MONO};border-radius:5px;padding:4px 7px;outline:none`;

  const commit = (idx, sb, prs) => {
    const map = {};
    prs.forEach(([k, v]) => { if (k) map[k] = v; });
    send('eds_variant', { file: e.file, index: idx, sub: sb, map });
  };
  const updatePair = (i, key, val) => {
    const next = pairs.map((p, pi) => (pi === i ? [key, val] : p));
    setPairs(next);
    commit(index, sub, next);
  };

  return html`
  <div style="padding:10px 11px;border-bottom:1px solid var(--bd2);background:var(--panel2);display:flex;flex-direction:column;gap:8px">
    <div style="font-size:10.5px;color:var(--dim);font-weight:600">VARIANT OBJECT
      <span style="font-weight:400;color:var(--faint)">· read on scan, mapped value → label shown per device · empty index = not tracked</span>
    </div>
    <div style="display:flex;gap:8px;align-items:center">
      <${SyncInput} value=${index} onCommit=${(v) => { setIndex(v); commit(v, sub, pairs); }} placeholder="0x2000" style="${fieldStyle}width:80px" />
      <span style="color:var(--faint)">:</span>
      <${SyncInput} value=${sub} onCommit=${(v) => { setSub(v); commit(index, v, pairs); }} placeholder="00" style="${fieldStyle}width:50px" />
    </div>
    ${pairs.map(([k, v], i) => html`
      <div style="display:flex;gap:8px;align-items:center">
        <${SyncInput} value=${k} onCommit=${(nv) => updatePair(i, nv, v)} placeholder="0x00" style="${fieldStyle}width:80px;color:var(--acc)" />
        <span style="color:var(--faint)">→</span>
        <${SyncInput} value=${v} onCommit=${(nv) => updatePair(i, k, nv)} placeholder="label" style="${fieldStyle}flex:1;font-family:'IBM Plex Sans'" />
        <span class="hv" onClick=${() => { const next = pairs.filter((_, pi) => pi !== i); setPairs(next); commit(index, sub, next); }}
          style="color:var(--faint);cursor:pointer;padding:2px 6px">✕</span>
      </div>`)}
    <div style="display:flex;gap:14px">
      <span class="hv" onClick=${() => setPairs([...pairs, ['', '']])} style="color:var(--acc);font-weight:600;font-size:11px;cursor:pointer">+ add value mapping</span>
      <span class="hv" onClick=${onClose} style="color:var(--faint);font-size:11px;cursor:pointer;margin-left:auto">Close</span>
    </div>
  </div>`;
}

// ------------------------------------------------------------------ setup --
function SetupPage({ s }) {
  const inputStyle = `border:1px solid var(--inp);background:var(--panel);color:var(--tx);border-radius:6px;padding:6px 9px;font:12px ${MONO};outline:none`;
  const label = (t) => html`<div style="font-size:11px;color:var(--dim);font-weight:600;margin-bottom:5px">${t}</div>`;
  const mc = s.mc;
  const [openVariant, setOpenVariant] = useState(null);
  const handleEdsFile = (ev) => {
    const file = ev.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => send('eds_upload', { filename: file.name, content: reader.result });
    reader.readAsText(file);
    ev.target.value = '';
  };
  const handlePluginFile = (ev) => {
    const file = ev.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      const b64 = (reader.result.split(',')[1]) || '';
      send('plugin_install', { filename: file.name, content: b64 });
    };
    reader.readAsDataURL(file);  // data: URL — base64 payload after the comma
    ev.target.value = '';
  };
  const mcOpt = (key, text, note) => html`
    <label onClick=${() => send('mc_opt', { key })} style="display:flex;align-items:center;gap:8px;cursor:pointer">
      ${Cb(mc[key], 'flex:none;')}${text} ${note && html`<span style="color:var(--faint)">${note}</span>`}
    </label>`;

  return html`
  <div style="flex:1;overflow:auto;padding:16px 18px;display:grid;grid-template-columns:1fr 1fr;gap:14px;align-content:start;min-height:0">
    ${s.workspaces.canSwitch && html`
    <div style="grid-column:1 / -1;background:var(--panel);border:1px solid var(--bd);border-radius:8px;padding:11px 16px;display:flex;align-items:center;gap:12px">
      <span style="font-weight:600;font-size:13px">Workspace</span>
      <select onChange=${(e) => {
          const name = e.target.value;
          if (name && name !== s.workspace && confirm(`Switch to workspace "${name}"? The bus will be disconnected.`)) send('workspace_switch', { name });
          else e.target.value = s.workspace;
        }}
        style="border:1px solid var(--inp);background:var(--panel);color:var(--tx);border-radius:5px;padding:4px 8px;font:12px ${MONO};outline:none;min-width:160px">
        ${s.workspaces.list.map((w) => html`<option value=${w} selected=${w === s.workspace}>${w}</option>`)}
      </select>
      <span class="hv" onClick=${() => { const name = prompt('New workspace name:'); if (name) send('workspace_create', { name }); }}
        style="${btn.ghost}font-size:11.5px;padding:5px 12px;border-radius:6px;cursor:pointer">＋ New…</span>
      <span style="font-size:10.5px;color:var(--faint)">each workspace keeps its own EDS files, machine-control state, test config and captures · folder: data/${s.workspace}</span>
    </div>`}
    <div style="background:var(--panel);border:1px solid var(--bd);border-radius:8px;padding:14px 16px;display:flex;flex-direction:column;gap:12px;min-width:0">
      <div style="font-weight:600;font-size:13px">Bus interface</div>
      <!-- grid, not a wrapping flex row: with flex-grow a card that wraps into
           a row of its own stretches across the full panel, so the adapters
           end up different sizes. Equal columns keep every card the same. -->
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:8px">
        ${s.adapters.map((a) => {
          const on = s.adapter === a.key;
          return html`
          <div class="hv-bd" onClick=${() => send('set_adapter', { adapter: a.key })}
            style="min-width:0;border:1px solid ${on ? 'var(--acc)' : 'var(--bd)'};background:${on ? 'var(--acc-soft)' : 'var(--panel)'};border-radius:7px;padding:9px 11px;cursor:pointer">
            <div style="font-weight:600;font-size:12px;color:${on ? 'var(--acc)' : 'var(--tx)'}">${a.label}</div>
            <div style="font:10.5px ${MONO};color:var(--dim);margin-top:2px">${a.sub}</div>
            <div style="font:10px ${MONO};color:var(--faint);margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${a.driver || ''}</div>
          </div>`;
        })}
      </div>
      <div style="display:flex;gap:24px">
        <div>
          ${label('BITRATE')}
          <div style="display:flex;gap:6px">
            ${['125', '250', '500', '1000'].map((b) => {
              const on = s.bitrate === b;
              return html`<span onClick=${() => send('set_bitrate', { bitrate: b })}
                style="border:1px solid ${on ? 'var(--acc)' : 'var(--inp)'};background:${on ? 'var(--acc-soft)' : 'transparent'};color:${on ? 'var(--acc)' : 'var(--mid)'};font:600 11.5px ${MONO};padding:5px 12px;border-radius:6px;cursor:pointer">${b === '1000' ? '1 M' : b + ' k'}</span>`;
            })}
          </div>
        </div>
        <div title="Cyclic SYNC frames (COB 0x080) — devices with synchronous TPDOs only transmit while a SYNC producer runs">
          ${label('SYNC PRODUCER')}
          <div style="display:flex;gap:8px;align-items:center">
            <span onClick=${() => send('sync_toggle')}
              style="border:1px solid ${s.sync.run ? 'var(--acc)' : 'var(--inp)'};background:${s.sync.run ? 'var(--acc-soft)' : 'transparent'};color:${s.sync.run ? 'var(--acc)' : 'var(--mid)'};font:600 11.5px ${MONO};padding:5px 14px;border-radius:6px;cursor:pointer">${s.sync.run ? '⟳ running' : 'off'}</span>
            <span style="font-size:11px;color:var(--dim)">every</span>
            <${SyncInput} value=${String(s.sync.ms)} onCommit=${(v) => send('set_sync_ms', { ms: v })}
              style="border:1px solid var(--inp);background:var(--panel);color:var(--tx);border-radius:5px;padding:5px 8px;font:11.5px ${MONO};width:52px;outline:none;text-align:right" />
            <span style="font-size:11px;color:var(--dim)">ms</span>
          </div>
        </div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px">
        <label style="font-size:11px;color:var(--dim);font-weight:600;display:flex;flex-direction:column;gap:4px">ADDRESS RANGE (NODE-ID)
          <${SyncInput} value=${`${s.scanRange[0]} – ${s.scanRange[1]}`} title="Node-ID range of this bus: addressing (teach) assigns IDs up to the upper end, and the scan probes exactly this range — e.g. 1 – 32 (a single number means just that node)"
            onCommit=${(v) => { const m = v.match(/^\s*(\d+)(?:\D+(\d+))?\s*$/); if (m) send('set_scan_range', { from: +m[1], to: +(m[2] ?? m[1]) }); }} style=${inputStyle} />
        </label>
        <label style="font-size:11px;color:var(--dim);font-weight:600;display:flex;flex-direction:column;gap:4px">SDO TIMEOUT (MS)<input value="500" style=${inputStyle} /></label>
        <label title="This tool's own CANopen node-ID as bus master, typically 126 or 127 — kept out of the auto-addressing range" style="font-size:11px;color:var(--dim);font-weight:600;display:flex;flex-direction:column;gap:4px">OWN NODE-ID
          <${SyncInput} value=${String(s.ownNodeId)} onCommit=${(v) => send('set_own_node_id', { node_id: v })} style=${inputStyle} />
        </label>
      </div>
    </div>

    <div style="background:var(--panel);border:1px solid var(--bd);border-radius:8px;padding:14px 16px;display:flex;flex-direction:column;gap:12px;min-width:0">
      <div style="font-weight:600;font-size:13px">EDS files</div>
      <div style="display:flex;gap:6px;align-items:center">
        <span style="font-size:11px;color:var(--dim);font-weight:600;flex:none">EDS FOLDER</span>
        <${SyncInput} value=${s.paths.eds} title="Folder holding the registered EDS files — e.g. a central pool shared between workspaces. Clear the field to reset to the workspace default."
          onCommit=${(v) => send('set_path', { which: 'eds', value: v })} style="flex:1;${inputStyle}" />
        <span class="hv" onClick=${() => send('browse_open', { which: 'eds' })} style="${btn.ghost}font-size:11.5px;padding:6px 12px;border-radius:6px;cursor:pointer">Browse…</span>
      </div>
      <div style="font-size:11px;color:var(--dim);font-weight:600">DEVICE PROFILES <span style="font-weight:400;color:var(--faint)">· active files are matched to devices on scan via Identity (0x1018)</span></div>
      <input type="file" id="eds-file-input" accept=".eds,.dcf" style="display:none" onChange=${handleEdsFile} />
      <div style="display:flex;flex-direction:column;border:1px solid var(--bd2);border-radius:7px;overflow:hidden">
        ${s.eds.files.map((e) => {
          const on = e.enabled;
          const used = s.devices.filter((d) => d.eds === e.file).length;
          const hasVariant = !!e.variant_index;
          const conflict = (e.conflict || []).length > 0;
          const conflictTitle = conflict
            ? `Identity conflict — same identity as ${e.conflict.join(', ')}. The newest file wins when matching devices${e.conflictWin ? ': this one' : ' — this file is shadowed'}.`
            : '';
          return html`
          <div>
          <div class="hv" onClick=${() => send('eds_toggle', { file: e.file })}
            style="display:grid;grid-template-columns:13px 1fr 48px 76px 100px 60px 40px 18px 18px;align-items:center;gap:10px;padding:8px 11px;border-bottom:1px solid var(--bd2);background:${on ? 'var(--sel)' : 'transparent'};cursor:pointer">
            ${Cb(on)}
            <span style="font:11.5px ${MONO};color:var(--tx);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${e.file}</span>
            <span onClick=${(ev) => ev.stopPropagation()}>
              <${SyncInput} value=${e.code || '?'} onCommit=${(v) => send('eds_code', { file: e.file, code: v })}
                title="Shortcode used in setup filenames"
                style="border:1px solid var(--inp);background:var(--panel);color:var(--acc);font:600 10.5px ${MONO};border-radius:4px;padding:3px 5px;width:100%;box-sizing:border-box;text-align:center;outline:none" />
            </span>
            <span style="font-size:11px;color:var(--dim)">${e.dev}</span>
            <span title=${conflict ? conflictTitle : 'Identity match: vendor · product code'} style="font:10.5px ${MONO};color:${conflict ? 'var(--amb)' : 'var(--faint)'}">${e.ident}</span>
            <span onClick=${(ev) => { ev.stopPropagation(); setOpenVariant(openVariant === e.file ? null : e.file); }}
              title="Configure per-device variant identification object"
              style="font:10.5px ${MONO};color:${hasVariant ? 'var(--acc)' : 'var(--faint)'};cursor:pointer;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${hasVariant ? `${e.variant_index}:${e.variant_sub || '00'}` : 'variant…'}</span>
            <span style="font:10.5px ${MONO};color:${used ? 'var(--grn)' : 'var(--faint)'};text-align:right">${used ? used + ' dev' : ''}</span>
            <span title=${conflictTitle} style="color:var(--amb);font-size:11px;text-align:center">${conflict ? '⚠' : ''}</span>
            <span onClick=${(ev) => { ev.stopPropagation(); send('eds_remove', { file: e.file }); }}
              title="Remove EDS file" class="hv" style="color:var(--faint);cursor:pointer;text-align:center">✕</span>
          </div>
          ${openVariant === e.file && html`<${VariantEditor} e=${e} onClose=${() => setOpenVariant(null)} />`}
          </div>`;
        })}
        <div class="hv" onClick=${() => document.getElementById('eds-file-input').click()}
          style="padding:8px 11px;color:var(--acc);font-weight:600;font-size:11.5px;cursor:pointer">+ Add EDS file…</div>
      </div>
    </div>

    <${PsuBox} psu=${s.psu} />

    <div style="grid-column:1/-1;background:var(--panel);border:1px solid var(--bd);border-radius:8px;padding:10px 16px;display:flex;flex-direction:column;gap:10px">
      <div style="display:flex;align-items:center;gap:10px;min-width:0">
        <span style="font-weight:600;font-size:12px;flex:none">Extensions</span>
        ${(s.ext?.plugins || []).length
          ? html`<span style="flex:none;font:600 10px ${MONO};background:var(--grn-soft);color:var(--grn);padding:2px 8px;border-radius:9px">${s.ext.plugins.length} PLUGIN${s.ext.plugins.length > 1 ? 'S' : ''}</span>
                 <span style="flex:none;font:600 11px ${MONO};color:var(--tx)">${s.ext.plugins.join(' · ')}</span>
                 <span style="min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:10.5px;color:var(--faint)">vendor packages loaded — their adapters, procedures and seeds are active</span>`
          : html`<span style="flex:none;font:600 10px ${MONO};background:var(--chip);color:var(--faint);padding:2px 8px;border-radius:9px">NONE</span>
                 <span style="min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:10.5px;color:var(--faint)">vendor-specific features — addressing procedures & session identities, special adapters, SWDL protocols — ship as separately installed plugin packages</span>`}
      </div>
      ${s.ext?.canInstall && html`
        <div style="display:flex;align-items:center;gap:10px">
          <input type="file" id="plugin-file-input" accept=".whl" style="display:none" onChange=${handlePluginFile} />
          <span class="hv" onClick=${() => document.getElementById('plugin-file-input').click()}
            style="${btn.ghost}font-size:11.5px;padding:5px 12px;border-radius:6px;cursor:pointer;white-space:nowrap;flex:none">+ Install plugin…</span>
          <span style="font-size:10.5px;color:var(--faint)">Upload a plugin package (.whl) — extracted and activated immediately, no restart. Plugins are executable code: only install packages from a source you trust.</span>
        </div>`}
      ${!!s.ext?.installed?.length && html`
        <div style="display:flex;flex-direction:column;border:1px solid var(--bd2);border-radius:7px;overflow:hidden">
          ${s.ext.installed.map((pkg) => html`
            <div style="display:flex;align-items:center;gap:10px;padding:6px 11px;border-bottom:1px solid var(--bd2);font-size:11.5px">
              <span style="font:600 11px ${MONO};color:var(--tx)">${pkg.name}</span>
              <span style="font:10.5px ${MONO};color:var(--faint)">v${pkg.version}</span>
              <span style="margin-left:auto;color:var(--faint)">installed package</span>
              <span class="hv" onClick=${() => send('plugin_remove', { pkg: pkg.name })} title="remove this package"
                style="color:var(--faint);cursor:pointer">✕</span>
            </div>`)}
        </div>`}
    </div>

    <div style="grid-column:1/-1;background:var(--panel);border:1px solid var(--bd);border-radius:8px;padding:14px 16px;display:flex;flex-direction:column;gap:12px">
      <div style="display:flex;align-items:center;gap:10px">
        <span style="font-weight:600;font-size:13px">Machine control</span>
        <span style="font:600 10px ${MONO};background:${mc.enabled ? 'var(--grn-soft)' : 'var(--chip)'};color:${mc.enabled ? 'var(--grn)' : 'var(--faint)'};padding:2px 8px;border-radius:9px">${mc.enabled ? 'ACTIVE' : 'INACTIVE'}</span>
        <span style="font-size:10.5px;color:var(--faint)">tool acts as CANopen master · addressing procedure = exchangeable flow file</span>
        <span class="hv-b" onClick=${() => send('mc_toggle')}
          style="margin-left:auto;font-weight:600;font-size:12px;color:${mc.enabled ? 'var(--red)' : '#fff'};border:1px solid ${mc.enabled ? 'var(--red)' : 'var(--grn)'};background:${mc.enabled ? 'var(--red-soft)' : 'var(--grn)'};padding:5px 14px;border-radius:6px;cursor:pointer">${mc.enabled ? 'Deactivate' : 'Activate machine control'}</span>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1.2fr 1.1fr;gap:14px">
        <div style="border:1px solid var(--bd2);border-radius:7px;padding:10px 12px;display:flex;flex-direction:column;gap:6px;font-size:12px">
          <div style="display:flex;justify-content:space-between;color:var(--mid)"><span>Expected state</span><span style="font:600 11px ${MONO};color:${mc.ref ? 'var(--tx)' : 'var(--faint)'}">${mc.ref ? `${mc.ref.expected} device(s) · ${mc.ref.adopted || ''}` : 'not adopted yet'}</span></div>
          <div style="display:flex;justify-content:space-between;color:var(--mid)"><span>Session-ID</span><span style="font:600 11px ${MONO};color:${mc.session ? 'var(--acc)' : 'var(--faint)'}" title=${mc.session ? '' : 'distributed by the next addressing run — needs an addressing provider (vendor plugin)'}>${mc.session || '—'}</span></div>
          <div style="display:flex;justify-content:space-between;color:var(--mid)"><span>Devices</span><span style="font:600 11px ${MONO};color:${mc.last ? 'var(--tx)' : 'var(--faint)'}">${mc.last ? `${mc.found} / ${mc.expected} found` : '—'}</span></div>
          <div style="display:flex;justify-content:space-between;color:var(--mid)"><span>Last check</span><span style="font:600 11px ${MONO};color:var(--faint)">${mc.last || '—'}</span></div>
          <span class="hv" onClick=${() => send('mc_adopt')} title="Store the scanned devices, their EDS assignments and the session-ID as the expected state — stored in the workspace, verified by Scan & verify"
            style="${btn.ghost}font-size:11px;padding:5px 10px;border-radius:6px;cursor:pointer;text-align:center;margin-top:2px">⟲ Adopt current state as expected</span>
        </div>
        <div style="border:1px solid var(--bd2);border-radius:7px;padding:10px 12px;display:flex;flex-direction:column;gap:8px">
          <div style="display:flex;gap:8px">
            <span class="hv-b" onClick=${() => send('mc_verify')} style="${btn.acc}font-size:11.5px;padding:6px 14px;border-radius:6px;cursor:pointer">${mc.busy ? 'Scanning…' : '⌕ Scan & verify'}</span>
            <span class="hv" onClick=${() => send('mc_readdress')} title="Addresses across the whole address range (Bus interface) — ends via the procedure's end signal; the freshly addressed bus is then adopted as the expected state"
              style="${btn.ghost}font-size:11.5px;padding:6px 14px;border-radius:6px;cursor:pointer">${mc.teach ? 'Teaching…' : 'Re-address (teach)'}</span>
          </div>
          ${mc.teach && html`
          <div style="border:1px solid var(--acc-bd);background:var(--acc-soft);border-radius:6px;padding:8px 10px;display:flex;flex-direction:column;gap:6px">
            <div style="font-size:11.5px;color:var(--acc);font-weight:600">Teach ${mc.teach.step}/${mc.teach.of} — ${mc.teach.text}</div>
            <div style="display:flex;gap:8px">
              ${s.adapter === 'demo' && html`<span class="hv-b" onClick=${() => send('demo_press')} style="${btn.acc}font-size:11px;padding:4px 10px;border-radius:5px;cursor:pointer">Simulate button press</span>`}
              <span class="hv" onClick=${() => send('mc_teach_abort')} style="border:1px solid var(--inp);color:var(--red);font-weight:600;font-size:11px;padding:4px 10px;border-radius:5px;cursor:pointer">Abort teach</span>
            </div>
          </div>`}
          ${mc.last
            ? html`<div style="font-size:11.5px;color:${mc.result === 'ok' ? 'var(--grn)' : 'var(--red)'};line-height:1.5">${mc.result === 'ok' ? '✓ Expected state valid — all devices found, EDS assignments match.' : '⚠ Mismatch — devices missing or wrong EDS assignment.'}</div>`
            : html`<div style="font-size:11.5px;color:var(--faint);line-height:1.5">No verification yet — adopt an expected state, then Scan & verify.</div>`}
          <div style="font-size:10.5px;color:var(--faint);line-height:1.5">Verification: a fresh scan must find every expected device with its assigned EDS. On mismatch the bus is re-addressed${mc.autoReaddr ? ' automatically' : ' after confirmation'}.</div>
          ${mc.enabled && mc.ref && html`
            <div style="font-size:11.5px;line-height:1.5;color:${mc.hbLost.length ? 'var(--red)' : 'var(--faint)'}">
              ${mc.hbLost.length
                ? `⚠ Heartbeat lost — node${mc.hbLost.length > 1 ? 's' : ''} ${mc.hbLost.map((n) => String(n).padStart(2, '0')).join(', ')}`
                : `♥ Heartbeat monitoring — watching ${Object.keys(mc.ref.assignments || {}).length} device(s), timeout ${mc.hbTimeoutMs} ms`}
            </div>`}
        </div>
        <div style="border:1px solid var(--bd2);border-radius:7px;padding:10px 12px;display:flex;flex-direction:column;gap:9px;font-size:12px;color:var(--mid)">
          ${mcOpt('autoStart', 'Restore machine control state at startup', '(last on/off state is remembered · off = always start deactivated)')}
          ${mcOpt('autoReaddr', 'Auto re-address when verification fails', '(default)')}
          ${mcOpt('scanStart', 'Scan & verify on server start', '')}
          <div style="display:flex;align-items:center;gap:8px" title="How long a monitored device may stay silent before Machine Control logs a heartbeat-lost alert">
            <span style="font-size:11px;color:var(--dim)">Heartbeat timeout</span>
            <${SyncInput} value=${String(mc.hbTimeoutMs)} onCommit=${(v) => send('mc_set_hb_timeout', { ms: v })}
              style="border:1px solid var(--inp);background:var(--panel);color:var(--tx);border-radius:5px;padding:4px 7px;font:11.5px ${MONO};width:64px;outline:none;text-align:right" />
            <span style="font-size:11px;color:var(--dim)">ms</span>
          </div>
          <div style="display:flex;align-items:center;gap:8px">
            <span style="font-size:11px;color:var(--dim)">Addressing procedure</span>
            <select onChange=${(e) => send('mc_flow', { file: e.target.value })}
              style="flex:1;border:1px solid var(--inp);background:var(--panel);color:var(--tx);border-radius:5px;padding:3px 6px;font:11px ${MONO};outline:none">
              ${(mc.flows || []).map((f) => html`<option value=${f} selected=${f === mc.teachFlow}>${f}</option>`)}
            </select>
          </div>
          ${!(s.ext?.addressing) && html`<div style="font-size:10.5px;color:var(--faint);line-height:1.4">Standard LSS only (untested on real hardware) — vendor procedures like button-teach and their session identities ship as plugin packages.</div>`}
        </div>
      </div>
    </div>

    <div style="grid-column:1/-1;background:var(--panel);border:1px solid var(--bd);border-radius:8px;padding:14px 16px;display:flex;flex-direction:column;gap:12px">
      <div style="font-weight:600;font-size:13px">Test configuration</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
        <div style="display:flex;flex-direction:column;gap:4px">
          <span style="font-size:11px;color:var(--dim);font-weight:600">TESTCASES FOLDER</span>
          <div style="display:flex;gap:6px">
            <${SyncInput} value=${s.paths.tc} onCommit=${(v) => send('set_path', { which: 'tc', value: v })} style="flex:1;${inputStyle}" />
            <span class="hv" onClick=${() => send('browse_open', { which: 'tc' })} style="${btn.ghost}font-size:11.5px;padding:6px 12px;border-radius:6px;cursor:pointer">Browse…</span>
          </div>
          <span style="font:10.5px ${MONO};color:var(--faint)">${s.tests.fileCount} test case file${s.tests.fileCount === 1 ? '' : 's'} discovered</span>
        </div>
        <div style="display:flex;flex-direction:column;gap:4px">
          <span style="font-size:11px;color:var(--dim);font-weight:600">RESULTS FOLDER</span>
          <div style="display:flex;gap:6px">
            <${SyncInput} value=${s.paths.res} onCommit=${(v) => send('set_path', { which: 'res', value: v })} style="flex:1;${inputStyle}" />
            <span class="hv" onClick=${() => send('browse_open', { which: 'res' })} style="${btn.ghost}font-size:11.5px;padding:6px 12px;border-radius:6px;cursor:pointer">Browse…</span>
          </div>
          <span style="font:10.5px ${MONO};color:var(--faint)">${s.tests.reports.length} report${s.tests.reports.length === 1 ? '' : 's'}</span>
        </div>
      </div>
    </div>

  </div>`;
}

// ---------------------------------------------------------------- objects --
// EDS LowLimit/HighLimit is a hint, not an enforced bound — real hardware is
// the one that gets to accept or abort a write, so this only ever colors the
// displayed value, never blocks Read/Write.
function outOfRange(display, min, max) {
  if (min == null && max == null) return false;
  if (display == null) return false;
  const n = parseInt(display, 16);
  if (!Number.isFinite(n)) return false;
  return (min != null && n < min) || (max != null && n > max);
}

function ObjectsPage({ s, ui, setUi }) {
  // favorites panel width: user-resizable via the divider, persisted
  const [favW, setFavW] = useState(() => Math.min(640, Math.max(240, parseInt(localStorage.getItem('cb-fav-w') || '340', 10) || 340)));
  const dragFav = (e) => {
    e.preventDefault();
    const startX = e.clientX;
    const startW = favW;
    const move = (ev) => {
      const w = Math.min(640, Math.max(240, startW + (startX - ev.clientX)));
      setFavW(w);
      localStorage.setItem('cb-fav-w', String(w));
    };
    const up = () => { window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', up); };
    window.addEventListener('mousemove', move);
    window.addEventListener('mouseup', up);
  };
  const selDevs = s.devices.filter((d) => d.sel);
  const targetNode = selDevs[0] ? selDevs[0].node : 1;
  const mirrorNode = selDevs[0] ? String(selDevs[0].node).padStart(2, '0') : '—';
  const edsCur = selDevs[0] ? selDevs[0].eds : 'no device selected';
  const vals = s.objects.vals;
  const raw = s.raw;
  // fixed columns must leave room for star+Read+Write (last col); NAME is
  // the only flexible one and may wrap long identifiers inside its cell
  const cols = '62px 30px minmax(120px,1fr) 40px 34px minmax(200px,260px) 132px';
  const rawInp = (r, ri, field, width) => html`
    <${SyncInput} value=${r[field]} onCommit=${(v) => send('raw_update', { row: ri, field, value: v })}
      style="border:1px solid var(--inp);background:var(--panel);color:var(--tx);border-radius:5px;padding:5px 8px;font:11.5px ${MONO};width:${width}px;outline:none" />`;

  const fav = s.favorites;
  const favKeys = new Set(fav.rows.map((r) => r.idx + ':' + r.sub));
  const plotSel = s.trace.plot.sel || [];
  const plotKeys = new Set(plotSel.map((r) => r.idx + ':' + r.sub));
  const plotFull = plotSel.length >= 4;
  const plotIcon = (idx, sub) => {
    const key = idx + ':' + sub;
    const on = plotKeys.has(key);
    return html`
      <span onClick=${() => send('plot_toggle', { idx, sub })}
        title=${on ? 'remove from signal plot' : plotFull ? 'signal plot full (4 max) — remove one first' : 'add to signal plot'}
        style="cursor:${!on && plotFull ? 'default' : 'pointer'};color:${on ? 'var(--acc)' : plotFull ? 'var(--bd2)' : 'var(--faint)'};font-size:12px">∿</span>`;
  };
  // Values are formatted core-side (canopen_bench/values.py) so parsing and
  // display have one home: `txt` in the chosen base, `alt` every reading of
  // it for the tooltip, `sym` the symbolic one where a plugin declared
  // fields. The raw number never disappears behind a name.
  const fmt = s.objects.fmt || {};
  const shownValue = (key, fallback) => (fmt[key] ? fmt[key].txt : fallback);
  const valueTitle = (key, fallback) => (fmt[key] ? fmt[key].alt : fallback);

  // What a person types is read one way and one way only: 0x makes it
  // hex, anything else is decimal (values.py, parse_value). The chip
  // below changes what the *table* shows and nothing about that — it
  // used to decide both, so with the table in hex a typed 12345678 came
  // back as 0x12345678: the same digits, a different number.
  const numberHint = ' · a number (0x… for hex, 0b… for binary, otherwise decimal) or a symbol name';
  const baseChip = html`
    <span class="hv-chip" onClick=${() => send('num_base')}
      title="show values as hex or decimal — the other reading stays in the tooltip. Typing is unaffected: 0x… is hex, bare digits are decimal"
      style="font:600 9px ${MONO};border:1px solid var(--inp);color:var(--acc);padding:1px 5px;border-radius:4px;cursor:pointer;letter-spacing:.04em">${(s.objects.base || 'hex').toUpperCase()}</span>`;

  const accByKey = {};
  for (const rows of Object.values(s.objects.catalog)) {
    for (const r of rows) accByKey[r[0] + ':' + r[1]] = r[4];
  }
  const favPanel = html`
    <div style="border:1px solid var(--bd);border-radius:8px;overflow:hidden">
      <div style="padding:8px 12px;font-weight:600;font-size:12px;border-bottom:1px solid var(--bd2);background:var(--panel2);display:flex;align-items:center;gap:8px">
        <span style="flex:1">${fav.rows.length} object${fav.rows.length === 1 ? '' : 's'}</span>
        ${baseChip}
      </div>
      ${fav.rows.length ? fav.rows.map(({ idx, sub, label }) => {
        const key = idx + ':' + sub;
        const cur = vals[key];
        const favAcc = accByKey[key] || 'ro';
        const writable = favAcc !== 'ro';
        return html`
        <div style="display:flex;align-items:center;gap:8px;padding:6px 12px;border-bottom:1px solid var(--bd2)">
          ${favAcc !== 'wo' ? html`
            <span class="hv-acc" onClick=${() => send('fav_read', { idx, sub })} title="read now" style="color:var(--faint);cursor:pointer">⟳</span>`
          : html`<span title="write-only — cannot be read" style="color:var(--faint);opacity:.35">⟳</span>`}
          <span style="font:10.5px ${MONO};color:var(--acc)">${idx}:${sub}</span>
          <span style="flex:1;min-width:0;color:var(--mid);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${label || '—'}</span>
          ${writable ? html`
            <${SyncInput} value=${shownValue(key, cur ?? '')}
              title=${valueTitle(key, 'staged value — Write sends it') + numberHint}
              onCommit=${(v) => send('obj_set', { idx, sub, val: v })}
              style="border:1px solid var(--inp);background:var(--panel);color:${cur ? 'var(--acc)' : 'var(--tx)'};border-radius:4px;padding:2px 7px;font:600 12px ${MONO};width:86px;outline:none;text-align:right" />
            <span class="hv" onClick=${() => send('obj_write', { idx, sub })} title="write the staged value to the device"
              style="${btn.ghost}font-size:10.5px;padding:2px 8px;border-radius:4px;cursor:pointer">Write</span>`
          : html`
            <span title=${valueTitle(key, '')} style="font:600 12px ${MONO};color:${cur ? 'var(--acc)' : 'var(--tx)'};background:var(--chip);padding:2px 9px;border-radius:4px;min-width:44px;text-align:right">${shownValue(key, cur ?? '—')}</span>`}
          ${plotIcon(idx, sub)}
          <span class="hv" onClick=${() => send('fav_toggle', { idx, sub })} title="remove favorite" style="color:var(--faint);cursor:pointer">✕</span>
        </div>`;
      }) : html`<div style="padding:10px 12px;font-size:11px;color:var(--faint)">no favorites yet — click ☆ in the object table</div>`}
    </div>`;

  const favNote = selDevs[0]
    ? `last known values · SN ${selDevs[0].sn} · from workspace db${s.favorites.lastDb ? ' (' + s.favorites.lastDb + ')' : ''}`
    : 'select a device to restore last known values (matched via 0x1018:04 serial number)';

  return html`
  <div style="flex:1;display:flex;min-height:0">
    <div style="width:200px;flex:none;border-right:1px solid var(--bd);background:var(--panel);display:flex;flex-direction:column;padding:12px 10px;gap:4px">
      ${!s.objects.groups.length && html`
        <div style="font-size:11px;color:var(--faint);line-height:1.5;padding:4px">${s.objects.hint}</div>`}
      ${s.objects.groups.map((g) => {
        const on = ui.objGroup === g.key;
        return html`
        <div class=${on ? '' : 'hv'} onClick=${() => setUi({ ...ui, objGroup: g.key })}
          style="padding:8px 10px;border-radius:6px;cursor:pointer;background:${on ? 'var(--acc-soft)' : 'transparent'}">
          <div style="display:flex;justify-content:space-between;font-weight:600;font-size:12px;color:${on ? 'var(--acc)' : 'var(--tx)'}">${g.label}<span style="color:var(--faint);font-weight:400">${g.count}</span></div>
          <div style="font:10px ${MONO};color:var(--faint);margin-top:1px">${g.range}</div>
        </div>`;
      })}
      <div style="margin-top:auto;font-size:10.5px;color:var(--faint);line-height:1.5;padding:0 4px">EDS: <span style="font-family:${MONO}">${edsCur}</span></div>
    </div>

    <div style="flex:1;min-width:0;display:flex;flex-direction:column;background:var(--panel2)">
      <div style="display:grid;grid-template-columns:${cols};padding:7px 0 7px 14px;border-bottom:1px solid var(--bd);font:600 10.5px 'IBM Plex Sans';color:var(--dim);text-transform:uppercase;letter-spacing:.05em;background:var(--panel)">
        <span>Index</span><span>Sub</span><span>Name</span><span>Type</span><span>Acc</span>
        <span style="display:flex;align-items:center;gap:6px">Value
          ${baseChip}
        </span><span></span>
      </div>
      <div style="flex:1;min-height:0;overflow:auto">
        ${!s.objects.groups.length && html`
          <div style="padding:26px 18px;text-align:center;font-size:12px;color:var(--faint)">${s.objects.hint}</div>`}
        ${(s.objects.catalog[ui.objGroup] || []).map(([idx, sub, name, type, acc, val, min, max]) => {
          const key = idx + ':' + sub;
          const cur = vals[key];
          const shown = shownValue(key, cur ?? val);
          const sym = (fmt[key] || {}).sym;
          const oor = outOfRange(cur ?? val, min, max);
          const rangeHint = (min != null || max != null) ? ` (EDS range ${min ?? '−∞'}…${max ?? '∞'})` : '';
          return html`
          <div style="display:grid;grid-template-columns:${cols};align-items:center;padding:5px 0 5px 14px;border-bottom:1px solid var(--bd2)">
            <span style="font:11.5px ${MONO};color:var(--acc)">${idx}</span>
            <span style="font:11.5px ${MONO};color:var(--faint)">${sub}</span>
            <span style="color:var(--tx);min-width:0;overflow-wrap:anywhere;padding-right:8px">${name}</span>
            <span style="font:10.5px ${MONO};color:var(--faint)">${type}</span>
            <span style="font:10.5px ${MONO};color:${acc === 'ro' ? 'var(--faint)' : acc === 'wo' ? 'var(--amb)' : 'var(--grn)'}">${acc}</span>
            ${acc !== 'ro' ? html`
              <span style="display:flex;align-items:center;gap:6px;min-width:0;padding-right:10px">
                <${SyncInput} value=${shown} title=${valueTitle(key, shown) + numberHint + rangeHint}
                  onCommit=${(v) => send('obj_set', { idx, sub, val: v })}
                  style="border:1px solid ${oor ? 'var(--amb)' : 'var(--inp)'};background:var(--panel);color:${cur ? 'var(--acc)' : 'var(--tx)'};border-radius:5px;padding:3px 7px;font:11.5px ${MONO};width:82px;outline:none;flex:none" />
                ${sym && html`<span title=${valueTitle(key, shown)} style="font-size:10.5px;color:var(--dim);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${sym}</span>`}
              </span>`
            : html`
              <span title=${valueTitle(key, shown) + rangeHint} style="font:11.5px ${MONO};font-weight:${oor ? 600 : 400};color:${oor ? 'var(--amb)' : (cur ? 'var(--acc)' : 'var(--tx)')};padding-right:10px;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${shown}${sym ? html`<span style="color:var(--dim);font-family:'IBM Plex Sans';font-size:10.5px"> ${sym}</span>` : ''}</span>`}
            <span style="display:flex;gap:5px;align-items:center;padding-right:12px">
              <span onClick=${() => send('fav_toggle', { idx, sub })} title="favorite"
                style="cursor:pointer;color:${favKeys.has(key) ? 'var(--amb, #d97706)' : 'var(--faint)'};font-size:12px">${favKeys.has(key) ? '★' : '☆'}</span>
              ${plotIcon(idx, sub)}
              ${acc !== 'wo' && html`<span class="hv-b" onClick=${() => send('obj_read', { idx, sub })} style="${btn.acc}font-size:10.5px;padding:2px 9px;border-radius:4px;cursor:pointer">Read</span>`}
              ${acc !== 'ro' && html`<span class="hv" onClick=${() => send('obj_write', { idx, sub })} style="${btn.ghost}font-size:10.5px;padding:2px 9px;border-radius:4px;cursor:pointer">Write</span>`}
            </span>
          </div>`;
        })}
      </div>
      <div style="flex:none;border-top:1px solid var(--bd);background:var(--panel);padding:8px 14px 10px;display:flex;flex-direction:column;gap:6px">
        <div style="display:flex;align-items:center;gap:8px">
          <span style="font-weight:600;font-size:11px;color:var(--dim)">RAW SDO · PDO · NMT</span>
          <span style="font:10px ${MONO};color:var(--faint)">autosaved · restored on next start</span>
          <span style="margin-left:auto;font:10.5px ${MONO};color:var(--faint)" title="used when a row's NODE field is empty">default target: node ${mirrorNode}</span>
          <span class="hv" onClick=${() => send('raw_remove')} style="width:20px;height:20px;display:grid;place-items:center;border:1px solid var(--inp);border-radius:4px;color:var(--mid);cursor:pointer;font-weight:700">−</span>
          <span style="font:600 11px ${MONO};color:var(--mid)">${raw.length}</span>
          <span class="hv" onClick=${() => send('raw_add')} style="width:20px;height:20px;display:grid;place-items:center;border:1px solid var(--inp);border-radius:4px;color:var(--mid);cursor:pointer;font-weight:700">+</span>
        </div>
        ${raw.map((r, ri) => {
          const type = r.type || 'sdo';
          const selStyle = `border:1px solid var(--inp);background:var(--panel);color:var(--tx);border-radius:5px;padding:4px 5px;font:11px ${MONO};outline:none`;
          const rawSel = (field, value, options, width) => html`
            <select onChange=${(e) => send('raw_update', { row: ri, field, value: e.target.value })} style="${selStyle};width:${width}px">
              ${options.map(([v, label]) => html`<option value=${v} selected=${v === value}>${label}</option>`)}
            </select>`;
          const sendBtn = html`<span class="hv-b" onClick=${() => send('raw_send', { row: ri })} style="${btn.acc}font-size:11px;padding:5px 12px;border-radius:5px;cursor:pointer">Send</span>`;
          return html`
          <div style="display:flex;align-items:center;gap:8px">
            ${rawSel('type', type, [['sdo', 'SDO'], ['pdo', 'PDO'], ['nmt', 'NMT']], 58)}
            <${SyncInput} value=${r.node || ''} title="node-id for this row — empty = selected device${type === 'nmt' ? ', 0/empty = all nodes' : ''}"
              placeholder=${type === 'nmt' ? 'all' : mirrorNode}
              onCommit=${(v) => send('raw_update', { row: ri, field: 'node', value: v })}
              style="border:1px solid var(--inp);background:var(--panel);color:var(--tx);border-radius:5px;padding:5px 8px;font:11.5px ${MONO};width:36px;outline:none;text-align:center" />
            ${type === 'sdo' && html`
              ${rawInp(r, ri, 'i', 64)}${rawInp(r, ri, 's', 34)}${rawInp(r, ri, 'l', 28)}${rawInp(r, ri, 'v', 110)}
              <span class="hv-b" onClick=${() => send('raw_read', { row: ri })} style="${btn.acc}font-size:11px;padding:5px 12px;border-radius:5px;cursor:pointer">Read</span>
              <span class="hv" onClick=${() => send('raw_write', { row: ri })} style="${btn.ghost}font-size:11px;padding:5px 12px;border-radius:5px;cursor:pointer">Write</span>`}
            ${type === 'pdo' && html`
              ${rawSel('pdo', r.pdo || 'RxPDO1',
                ['RxPDO1', 'RxPDO2', 'RxPDO3', 'RxPDO4', 'TxPDO1', 'TxPDO2', 'TxPDO3', 'TxPDO4'].map((x) => [x, x]), 86)}
              <${SyncInput} value=${r.data || ''} placeholder="data bytes, e.g. 01 A0 00 FF" title="up to 8 hex bytes"
                onCommit=${(v) => send('raw_update', { row: ri, field: 'data', value: v })}
                style="border:1px solid var(--inp);background:var(--panel);color:var(--tx);border-radius:5px;padding:5px 8px;font:11.5px ${MONO};width:246px;outline:none" />
              ${sendBtn}
              <${SyncInput} value=${r.cyc || '100'} title="cycle time in ms for cyclic sending (⟳)"
                onCommit=${(v) => send('raw_update', { row: ri, field: 'cyc', value: v })}
                style="border:1px solid var(--inp);background:var(--panel);color:var(--tx);border-radius:5px;padding:5px 6px;font:11.5px ${MONO};width:42px;outline:none;text-align:right" />
              <span style="font:10px ${MONO};color:var(--faint)">ms</span>
              <span class="hv" onClick=${() => send('raw_cycle', { row: ri })}
                title="send this frame cyclically — e.g. to feed a device's RPDO like the machine's PLC would"
                style="white-space:nowrap;border:1px solid ${r.run ? 'var(--acc)' : 'var(--inp)'};background:${r.run ? 'var(--acc-soft)' : 'transparent'};color:${r.run ? 'var(--acc)' : 'var(--mid)'};font:600 11px ${MONO};padding:5px 10px;border-radius:5px;cursor:pointer">${r.run ? '⟳ on' : '⟳ off'}</span>`}
            ${type === 'nmt' && html`
              ${rawSel('cmd', r.cmd || 'start',
                [['start', 'Start (Operational)'], ['preop', 'Pre-Operational'], ['stop', 'Stop'],
                 ['reset', 'Reset node'], ['resetcomm', 'Reset communication']], 176)}
              ${sendBtn}`}
          </div>`;
        })}
      </div>
    </div>

    <div class="vdrag" onMouseDown=${dragFav} title="drag to resize the favorites panel"
      style="flex:none;width:5px;cursor:col-resize;background:var(--bd)"></div>
    <div style="width:${favW}px;flex:none;background:var(--panel);overflow:auto;padding:14px 14px;display:flex;flex-direction:column;gap:12px">
      <div style="display:flex;justify-content:space-between;align-items:baseline"><span style="font-weight:600;font-size:13px">Favorites</span><span style="font-size:10px;color:var(--faint)">auto-saved in the workspace</span></div>
      <div style="font:10px ${MONO};color:var(--faint);line-height:1.5">${favNote}</div>
      ${favPanel}
      <span class="hv-b" onClick=${() => send('fav_read_all')} style="text-align:center;${btn.acc}font-size:11.5px;padding:7px 0;border-radius:6px;cursor:pointer">⟳ Read all favorites</span>
    </div>
  </div>`;
}

// ------------------------------------------------------------------ tests --
// A catalog filter, offering only what the folder actually contains — a
// dropdown listing grades or variants no case has is a filter that can
// only ever empty the list.
//
// The subtree is memoised, and that is not a micro-optimisation — it is
// what makes the dropdown usable at all. Preact clears `option.value`
// while diffing an <option>'s text child and writes it back straight
// after, on every render, whether or not anything changed. At one state
// snapshot per tick that rewrites every option ten times a second, and
// Chromium rebuilds an open popup when its options are touched: whatever
// you were pointing at snaps back to the current selection unless you
// click within the tick. Returning the identical vnode while the chip's
// own inputs are unchanged makes Preact skip the subtree by vnode
// identity, so the popup is left alone. `onPick` goes through a ref
// because the memo would otherwise hold the first render's closure and
// pick against a stale `ui`.
function FilterChip({ label, value, options, empty, onPick }) {
  const pick = useRef(onPick);
  pick.current = onPick;
  return useMemo(() => {
    const active = !!value;
    return html`
    <span style="display:flex;align-items:center;gap:5px;border:1px solid ${active ? 'var(--acc)' : 'var(--inp)'};background:${active ? 'var(--acc-soft)' : 'transparent'};border-radius:6px;padding:4px 8px;color:${active ? 'var(--acc)' : 'var(--mid)'}">
      <span style="font-size:12px">${label}:</span>
      <select value=${value} onChange=${(e) => pick.current(e.target.value)}
        disabled=${!options.length}
        title=${options.length ? '' : `no case in this folder declares a ${label.toLowerCase()}`}
        style="background:transparent;color:inherit;border:0;font:600 12px 'IBM Plex Sans';outline:none;cursor:${options.length ? 'pointer' : 'default'}">
        <option value="">${empty}</option>
        ${options.map((o) => html`<option value=${o}>${o}</option>`)}
      </select>
    </span>`;
    // the options are compared by content: the server sends a fresh array
    // every tick, so comparing the array itself would never match
  }, [label, value, empty, options.join(' ')]);
}

function TestsPage({ s, ui, setUi }) {
  const t = s.tests;
  const resDir = (s.paths || {}).res || '';
  const filter = (ui.testFilter || '').toLowerCase();
  // "" means no restriction for both dropdowns. A case with no variants
  // declared runs on every variant, so it stays visible under any choice —
  // hiding it would suggest it does not apply, which is the opposite.
  const shown = t.catalog
    .filter(([, , tools]) => t.toolFilter || tools === '—')
    .filter(([, , , , , grade]) => !ui.gradeFilter || grade === ui.gradeFilter)
    .filter(([, , , , , , variants]) => !ui.variantFilter || !variants.length
      || variants.includes(ui.variantFilter))
    .filter(([id, name]) => !filter || id.includes(filter) || name.toLowerCase().includes(filter));
  const selIds = shown.map((x) => x[0]).filter((id) => t.sel.includes(id));
  // a run needs a DUT only where a selected case asks for the one picked
  // in the Devices box; a case that names its device by code brings its
  // own, and the demo catalog needs none at all. Same rule the server
  // refuses on, so the button stops promising a run that cannot start.
  const selDevs = s.devices.filter((d) => d.sel);
  const dutMissing = !selDevs.length
    && shown.some((r) => t.sel.includes(r[0]) && r[9]);
  const runningId = t.running ? t.runOrder[t.runIdx] : null;
  const total = t.runOrder.length;
  const runLog = t.runOrder
    .slice(0, t.runIdx + (t.running ? 0 : 1))
    .filter((id) => t.results[id])
    .slice(-4)
    .map((id) => ({
      line: `${id} ${t.results[id]} · ${(t.catalog.find((x) => x[0] === id) || [])[3]}`,
      fg: t.results[id] === 'FAIL' || t.results[id] === 'ERROR' ? 'var(--red)' : 'var(--dim)',
    }));
  if (t.running && runningId) runLog.push({ line: runningId + ' running…', fg: 'var(--acc)' });
  const anyFail = Object.values(t.results).includes('FAIL');
  // runIdx counts cases *finished*, so during the first case it is 0 —
  // "0 / 4" next to "Running 1000" reads as if nothing had started. While
  // a run is on, the counter names the case being worked on instead. The
  // bar folds in that case's own step progress: a case with a hundred
  // steps otherwise leaves the bar parked for minutes, which is what a
  // stuck run looks like.
  const runAt = Math.min(t.running ? t.runIdx + 1 : t.runIdx, total);
  const within = t.running && t.runProg && t.runProg.of > 0
    ? Math.min(1, t.runProg.step / t.runProg.of) : 0;
  const runFrac = total ? Math.min(1, (t.runIdx + within) / total) : 0;
  // every column is capped and clipped: a broken file's "id" is its whole
  // filename, which used to run straight through the next two columns
  const cols = '34px 72px minmax(160px,1fr) 110px 90px 76px 28px';
  const cell = 'min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap';
  const repInp = (which, value) => html`
    <${SyncInput} value=${String(value)} onCommit=${(v) => send('set_repeat', { which, n: parseInt(v, 10) || 1 })}
      style="border:1px solid var(--inp);background:var(--panel);color:var(--tx);border-radius:6px;padding:4px 8px;font:12px ${MONO};text-align:right;outline:none" />`;

  return html`
  <div style="flex:none;background:var(--panel);border-bottom:1px solid var(--bd);display:flex;align-items:center;gap:8px;padding:9px 18px">
    <${FilterChip} label="Variant" value=${ui.variantFilter || ''} options=${t.variants || []}
      empty="all" onPick=${(v) => setUi({ ...ui, variantFilter: v })} />
    <${FilterChip} label="Category" value=${ui.gradeFilter || ''} options=${t.grades || []}
      empty="all" onPick=${(v) => setUi({ ...ui, gradeFilter: v })} />
    <span onClick=${() => send('tool_filter_toggle')}
      style="border:1px solid ${t.toolFilter ? 'var(--acc)' : 'var(--inp)'};background:${t.toolFilter ? 'var(--acc-soft)' : 'transparent'};border-radius:6px;padding:5px 9px;color:${t.toolFilter ? 'var(--acc)' : 'var(--mid)'};font-weight:600;cursor:pointer">Tool: PSU ${t.toolFilter ? '✓' : '✕'}</span>
    <input placeholder="⌕ Filter test cases…" value=${ui.testFilter || ''} onInput=${(e) => setUi({ ...ui, testFilter: e.target.value })}
      style="border:1px solid var(--inp);background:var(--panel);color:var(--tx);border-radius:6px;padding:5px 10px;width:170px;outline:none;font:12px 'IBM Plex Sans'" />
    <span style="width:10px"></span>
    <span onClick=${() => send('tests_all', { ids: shown.map((r) => r[0]) })} style="font-size:11px;color:var(--acc);font-weight:600;cursor:pointer">all</span>
    <span onClick=${() => send('tests_none')} style="font-size:11px;color:var(--acc);font-weight:600;cursor:pointer">none</span>
    <span style="flex:1"></span>
    <span onClick=${() => send('tc_rescan')} title="Re-read the TestCases folder — after editing a YAML, without restarting"
      style="font-size:11px;color:var(--acc);font-weight:600;cursor:pointer;margin-right:12px">↻ reload</span>
    <span style="font:10.5px ${MONO};color:var(--faint)">${shown.length} of ${t.catalog.length}</span>
  </div>
  <div style="flex:1;display:flex;min-height:0">
    <div style="flex:1;min-width:0;display:flex;flex-direction:column">
      <div style="display:grid;grid-template-columns:${cols};padding:7px 0;border-bottom:1px solid var(--bd);font:600 10.5px 'IBM Plex Sans';color:var(--dim);text-transform:uppercase;letter-spacing:.05em;background:var(--panel2)">
        <span></span><span>ID</span><span>Test case</span><span>Tools</span><span>Result</span><span style="text-align:right">Ø time</span><span></span>
      </div>
      <div style="flex:1;min-height:0;overflow:auto">
        ${!t.catalog.length && html`
          <div style="padding:30px 24px;text-align:center;color:var(--faint);font-size:12px;line-height:1.8">
            <div style="font-weight:600;color:var(--mid)">No test cases found</div>
            <div>TestCases folder: <span style="font-family:${MONO}">${s.paths.tc}</span></div>
            <div>Test cases are YAML files (<span style="font-family:${MONO}">${'TC<id>_<name>.yaml'}</span>) —
              format spec: <span style="font-family:${MONO}">docs/ablaeufe/testfall-format.md</span> ·
              ready-to-copy examples: <span style="font-family:${MONO}">examples/testcases/</span></div>
          </div>`}
        ${shown.map(([id, name, tools, time, err, , , file, errMsg]) => {
          const sel = t.sel.includes(id);
          const res = t.results[id] || t.lastRes[id] || '—';
          const isRun = id === runningId;
          return html`
          <div class=${err ? '' : 'hv'} onClick=${err ? null : () => send('test_toggle', { id })}
            style="display:grid;grid-template-columns:${cols};align-items:center;padding:6px 0;border-bottom:1px solid var(--bd2);background:${isRun ? 'var(--acc-soft)' : sel ? 'var(--sel)' : 'transparent'};cursor:${err ? 'default' : 'pointer'};opacity:${err ? '.8' : '1'}">
            <span style="display:grid;place-items:center">${err ? html`<span style="color:var(--red);font-weight:700">!</span>` : Cb(sel)}</span>
            <span style="${cell};font:11.5px ${MONO};color:${err ? 'var(--red)' : 'var(--acc)'}" title=${id}>${err ? '—' : id}</span>
            ${err
              ? html`<span style="min-width:0;color:var(--red)" title=${`${file}\n${errMsg}`}>
                  <div style="font:11px ${MONO};overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${file}</div>
                  <div style="font-size:11px;opacity:.85;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${errMsg || 'schema error'}</div>
                </span>`
              : html`<span style="${cell};color:var(--tx)" title=${name}>${name}</span>`}
            <span style="${cell};font:10.5px ${MONO};color:var(--faint)">${err ? '' : tools}</span>
            <span style="${cell};font-weight:600;font-size:11px;color:${isRun ? 'var(--acc)' : res === 'PASS' ? 'var(--grn)' : res === 'FAIL' || res === 'ERROR' ? 'var(--red)' : 'var(--faint)'}">${err ? '' : isRun ? 'RUN…' : res}</span>
            <span style="${cell};text-align:right;font:11px ${MONO};color:var(--faint)">${err ? '' : time}</span>
            <span class="hv-white" title="Open in the system's editor"
              onClick=${(e) => { e.stopPropagation(); send('tc_open', { id }); }}
              style="text-align:center;color:var(--faint);cursor:pointer;font-size:12px">✎</span>
          </div>`;
        })}
      </div>
      <div style="flex:none;padding:8px 18px;border-top:1px solid var(--bd);background:var(--pan2, var(--panel2));display:flex;gap:10px;align-items:center;flex-wrap:wrap;font-size:11.5px;color:var(--dim)">
        <span><b style="color:var(--tx)">${shown.length}</b> shown</span><span><b style="color:var(--tx)">${selIds.length}</b> selected</span>
        <span onClick=${() => { const name = prompt('Save suite as:', t.activeSuite || ''); if (name) send('suite_save', { name }); }}
          style="margin-left:auto;color:var(--acc);font-weight:600;cursor:pointer">save suite</span>
        ${t.suites.map((name) => {
          const on = t.activeSuite === name;
          return html`<span onClick=${() => send('suite_load', { name })}
            style="border:1px solid ${on ? 'var(--acc)' : 'var(--inp)'};background:${on ? 'var(--acc-soft)' : 'transparent'};color:${on ? 'var(--acc)' : 'var(--mid)'};font:600 10.5px 'IBM Plex Sans';padding:2px 9px;border-radius:9px;cursor:pointer">${name}${on ? html`<b onClick=${(e) => { e.stopPropagation(); send('suite_delete', { name }); }} style="margin-left:5px;cursor:pointer">✕</b>` : ''}</span>`;
        })}
        ${!t.suites.length && html`<span style="color:var(--faint)">no suites saved</span>`}
      </div>
    </div>
    <div style="width:300px;flex:none;border-left:1px solid var(--bd);background:var(--panel);display:flex;flex-direction:column;padding:14px 16px;gap:12px;overflow:auto">
      <div style="font-weight:600;font-size:13px">Run configuration</div>
      <div style="display:grid;grid-template-columns:1fr 64px;gap:8px;align-items:center;font-size:12px;color:var(--mid)">
        <span>Repeat test case</span>${repInp('case', t.repeatCase)}
        <span>Repeat run</span>${repInp('run', t.repeatRun)}
      </div>
      <label onClick=${() => send('stop_err_toggle')} style="display:flex;align-items:center;gap:8px;font-size:12px;color:var(--mid);cursor:pointer">${Cb(t.stopOnErr)}Stop on error</label>
      <div style="display:flex;gap:8px">
        <span class="hv-b" onClick=${() => send('run_start', { ids: shown.map((r) => r[0]) })}
          style="flex:1;text-align:center;background:${t.running || !selIds.length || dutMissing ? 'var(--faint)' : 'var(--grn)'};color:#fff;font-weight:600;padding:8px 0;border-radius:7px;cursor:pointer">${t.running ? 'Running…' : `▶ Start ${selIds.length} tests`}</span>
        <span class="hv" onClick=${() => send('run_stop')} style="border:1px solid var(--inp);color:${t.running ? 'var(--red)' : 'var(--faint)'};font-weight:600;padding:8px 12px;border-radius:7px;cursor:pointer">■</span>
      </div>
      ${t.manual && html`<${OperatorPrompt} p=${t.manual} />`}
      <div style="border:1px solid var(--bd);border-radius:8px;padding:10px 12px;background:var(--panel2)">
        <div style="display:flex;justify-content:space-between;font-size:11.5px;color:var(--mid);margin-bottom:6px">
          <span>${t.running ? 'Running ' + runningId : total ? 'Idle — last run' : 'Idle'}</span>
          <span>${total ? runAt + ' / ' + total : '—'}</span>
        </div>
        <div style="height:6px;border-radius:3px;background:var(--bd);overflow:hidden"><span style="display:block;width:${Math.round(100 * runFrac)}%;height:100%;background:${anyFail ? 'var(--red)' : 'var(--acc)'};transition:width .4s"></span></div>
        <div style="margin-top:8px;font:10.5px ${MONO};color:var(--dim);line-height:1.7;min-height:56px">
          ${(runLog.length ? runLog : [{ line: 'no run yet — select tests and press Start', fg: 'var(--faint)' }]).map((r) => html`<div style="color:${r.fg}">${r.line}</div>`)}
          ${t.runProg && html`<div style="color:var(--acc)">TEST ${t.runProg.tid} step ${t.runProg.step}/${t.runProg.of}  ${t.runProg.text}</div>`}
        </div>
      </div>
      <div style="font-weight:600;font-size:13px;margin-top:2px">Recent reports</div>
      <div style="display:flex;flex-direction:column;gap:6px;font-size:11.5px">
        ${t.reports.map((rp) => html`
          <span style="display:flex;justify-content:space-between;color:var(--mid)">${rp.file ? reportLink(rp.file, resDir) : html`<span>${rp.name}</span>`}<span style="color:${rp.ok ? 'var(--grn)' : 'var(--red)'};font-weight:600">${rp.score}</span></span>`)}
      </div>
      <${ResultsPath} dir=${resDir} />
      <${OverviewBox} ov=${t.overview} dir=${resDir} />
    </div>
  </div>`;
}

// The run is stopped, waiting for the person at the bench. Three shapes,
// one box: confirm ("done / abort"), ask ("yes / no / cancel" — where no
// is a verdict about the device, not an aborted run) and adjust, which
// reads an object, lets it be corrected, and writes it back.
const promptBtn = 'font-weight:600;padding:6px 12px;border-radius:6px;cursor:pointer;text-align:center';

function OperatorPrompt({ p }) {
  const [val, setVal] = useState(p.value ?? '');
  const kind = p.kind || 'confirm';
  const key = `${p.tid}:${p.index || ''}:${p.sub || ''}:${p.text || ''}`;
  useEffect(() => setVal(p.value ?? ''), [key]);
  return html`
  <div style="border:1px solid var(--acc-bd);border-radius:8px;padding:10px 12px;background:var(--acc-soft)">
    <div style="font-size:10.5px;color:var(--acc);font-weight:700;letter-spacing:.05em;margin-bottom:4px">
      ${kind === 'ask' ? 'QUESTION' : kind === 'adjust' ? 'ADJUST' : 'OPERATOR ACTION'} · TEST ${p.tid}
    </div>
    ${p.title && html`<div style="font-size:12.5px;color:var(--tx);font-weight:600">${p.title}</div>`}
    <div style="font-size:12.5px;color:var(--tx);margin-bottom:8px">${p.text}</div>
    ${kind === 'adjust' && html`
      <div style="display:flex;gap:8px;align-items:center">
        <span style="font:11px ${MONO};color:var(--dim);flex:none">${p.index}:${p.sub}</span>
        <input value=${val} onInput=${(e) => setVal(e.target.value)}
          style="flex:1;min-width:0;background:var(--bg);color:var(--fg);border:1px solid var(--inp);border-radius:6px;font:12px ${MONO};padding:4px 8px" />
      </div>
      <div style="font-size:10.5px;color:var(--faint);margin:3px 0 8px 0">0x… is hex, anything else decimal</div>`}
    <div style="display:flex;gap:8px">
      ${kind === 'confirm' && html`
        <span class="hv-b" onClick=${() => send('manual_confirm')} style="flex:1;${promptBtn};background:var(--grn);color:#fff">Done ✓</span>
        <span class="hv" onClick=${() => send('manual_abort')} style="${promptBtn};border:1px solid var(--inp);color:var(--red)">Abort</span>`}
      ${kind === 'ask' && html`
        <span class="hv-b" onClick=${() => send('manual_answer', { choice: 'ok' })} style="flex:1;${promptBtn};background:var(--grn);color:#fff">Yes</span>
        <span class="hv-b" onClick=${() => send('manual_answer', { choice: 'no' })} style="flex:1;${promptBtn};background:var(--red);color:#fff">No</span>
        <span class="hv" onClick=${() => send('manual_answer', { choice: 'cancel' })} style="${promptBtn};border:1px solid var(--inp);color:var(--mid)">Cancel</span>`}
      ${kind === 'adjust' && html`
        <span class="hv-b" onClick=${() => send('manual_answer', { choice: 'ok', value: val })} style="flex:1;${promptBtn};background:var(--grn);color:#fff">Write ✓</span>
        <span class="hv" onClick=${() => send('manual_answer', { choice: 'cancel' })} style="${promptBtn};border:1px solid var(--inp);color:var(--mid)">Cancel</span>`}
    </div>
  </div>`;
}

// A report file, opened in its own tab. Underlined accent text with a
// pointer cursor and no handler is not "not clickable yet" — it is a
// promise the page does not keep, and the run it names is a file somebody
// wants to read. The server hands the results folder out under one prefix
// so the links inside a summary reach its per-case pages.
//
// The href stays http even though the file is usually on this very disk:
// a browser refuses to follow a file:// link from an http page, so that
// version of "no server needed" would be the dead link all over again —
// and when the bench runs on another machine, a local path is simply the
// wrong answer. The path goes in the tooltip instead, where it costs
// nothing and answers "where is this thing when the bench is off".
const reportLink = (name, dir) => html`
  <a href=${'/api/report/' + encodeURIComponent(name)} target="_blank" rel="noopener"
    title=${dir ? `${joinPath(dir, name)}\n\nopened here through the bench — the file itself needs no server` : 'open ' + name}
    style="color:var(--acc);text-decoration:underline;cursor:pointer">${name}</a>`;

// Separator taken from the folder itself rather than from the browser:
// the path was configured on the machine running the bench, and that is
// not necessarily this one.
const joinPath = (dir, name) =>
  dir ? dir.replace(/[\\/]+$/, '') + (dir.includes('\\') ? '\\' : '/') + name : name;

function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
  const ta = document.createElement('textarea');   // plain http on a LAN address
  ta.value = text;
  ta.style.cssText = 'position:fixed;opacity:0';
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand('copy'); } finally { ta.remove(); }
  return Promise.resolve();
}

// Where the reports actually are. A run leaves standalone HTML behind —
// relative links between the pages, one stylesheet beside them — so the
// folder is all anyone needs to read a run back with the bench stopped.
// Clicking copies it, because the next step is pasting it into a file
// manager, not reading it out loud.
function ResultsPath({ dir }) {
  const [done, setDone] = useState(false);
  if (!dir) return null;
  // shortened from the left: the tail is the part that identifies the
  // folder, and a wrapped absolute path costs three lines of a 300px panel
  const short = dir.length > 36 ? '…' + dir.slice(-35) : dir;
  return html`
    <span class="hv-white" onClick=${() => copyText(dir).then(() => {
      setDone(true);
      setTimeout(() => setDone(false), 1400);
    })}
      title=${`${dir}\n\nclick to copy — the reports are ordinary files and open from this folder without the bench running`}
      style="font:10px ${MONO};color:var(--faint);cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">
      ${done ? '✓ path copied' : short}
    </span>`;
}

// Across runs, by hardware variant. Written on request, not after every
// run: it reads the whole results folder, and most runs are one more data
// point in a picture nobody is looking at right now.
function OverviewBox({ ov, dir }) {
  const [days, setDays] = useState(7);
  return html`
    <div style="font-weight:600;font-size:13px;margin-top:6px;display:flex;justify-content:space-between;align-items:baseline">
      Overview by variant
      <span style="display:flex;gap:6px;align-items:baseline;font-weight:400">
        <span style="font-size:10.5px;color:var(--faint)">last</span>
        <select value=${String(days)} onChange=${(e) => setDays(Number(e.target.value))}
          style="background:var(--bg);color:var(--fg);border:1px solid var(--bd);border-radius:5px;font:11px ${MONO};padding:1px 3px">
          ${[1, 2, 3, 5, 7, 10, 14, 30].map((d) => html`<option value=${String(d)}>${d} d</option>`)}
        </select>
        <span class="hv" onClick=${() => send('report_overview', { days })}
          style="font-size:10.5px;color:var(--acc);font-weight:600;cursor:pointer">create →</span>
      </span>
    </div>
    <div style="font-size:11.5px;color:var(--mid);display:flex;flex-direction:column;gap:4px">
      ${!ov && html`<span style="color:var(--faint)">not created yet — the file lands beside the reports</span>`}
      ${ov && html`
        <span style="display:flex;justify-content:space-between">
          ${reportLink(ov.name, dir)}
          <span style="color:var(--faint);font:10.5px ${MONO}">${ov.runs} run${ov.runs === 1 ? '' : 's'} · ${ov.days} d</span>
        </span>
        ${ov.variants.length === 0 && html`<span style="color:var(--faint)">no runs in that window</span>`}
        ${ov.variants.map((v) => html`
          <span style="display:flex;justify-content:space-between;font:10.5px ${MONO}">
            <span>${v.key}</span>
            <span style="color:var(--faint)">${v.passed}/${v.of} · <span style="color:${v.verdict === 'PASS' ? 'var(--grn)' : v.verdict === 'FAIL' ? 'var(--red)' : 'var(--amb)'};font-weight:600">${v.verdict}</span></span>
          </span>`)}`}
    </div>`;
}

// ------------------------------------------------------------------- swdl --
function SwdlPage({ s }) {
  const selDevs = s.devices.filter((d) => d.sel);
  const w = s.swdl;
  if (s.adapter !== 'demo' && !w.vendor) {
    return html`
    <div style="flex:1;overflow:auto;padding:16px 18px;display:grid;align-content:start">
      <div style="background:var(--panel);border:1px solid var(--bd);border-radius:8px;padding:16px 18px;max-width:640px;display:flex;flex-direction:column;gap:10px">
        <div style="font-weight:600;font-size:13px">Firmware download — vendor-specific</div>
        <div style="font-size:12px;color:var(--mid);line-height:1.7">
          CANopen has no complete firmware-update standard. CiA 302-3 only defines a generic
          program-download framework (objects <span style="font-family:${MONO}">0x1F50</span>/<span style="font-family:${MONO}">0x1F51</span>) —
          the actual bootloader protocol, image format, erase/verify sequence and timing are
          manufacturer-specific.
        </div>
        <div style="font-size:12px;color:var(--mid);line-height:1.7">
          Device support therefore ships as a <b>vendor extension package</b> for this tool.
          No download protocol is installed for the current setup — contact the developer
          (see the <b>About</b> page, bottom left) to get one built for your devices.
        </div>
      </div>
    </div>`;
  }
  const status = w.run ? `transfer via ${w.mode.toUpperCase()} running…`
    : w.done ? 'all targets verified ✓' : `${selDevs.length} target(s) · v${w.sel}`;
  return html`
  <div style="flex:1;overflow:auto;padding:16px 18px;display:grid;grid-template-columns:1fr 1.2fr;gap:14px;align-content:start;min-height:0">
    <div style="background:var(--panel);border:1px solid var(--bd);border-radius:8px;padding:14px 16px;display:flex;flex-direction:column;gap:12px">
      <div style="font-weight:600;font-size:13px">Firmware library</div>
      <div class="hv-drop" style="border:1.5px dashed var(--inp);border-radius:8px;padding:18px;text-align:center;color:var(--faint);font-size:12px">Drop firmware file (.bin / .hex) here to add a version</div>
      <div style="display:flex;flex-direction:column;border:1px solid var(--bd2);border-radius:7px;overflow:hidden">
        ${w.fw.map((f) => {
          const on = w.sel === f.ver;
          return html`
          <div class="hv" onClick=${() => send('swdl_fw', { ver: f.ver })}
            style="display:flex;align-items:center;gap:10px;padding:8px 11px;border-bottom:1px solid var(--bd2);background:${on ? 'var(--sel)' : 'transparent'};cursor:pointer">
            <span style="width:12px;height:12px;border-radius:50%;border:1.5px solid ${on ? 'var(--acc)' : 'var(--inp)'};display:grid;place-items:center;flex:none"><span style="width:6px;height:6px;border-radius:50%;background:${on ? 'var(--acc)' : 'transparent'}"></span></span>
            <span style="font:11.5px ${MONO};flex:1;color:var(--tx)">${f.file}</span>
            <span style="font:600 10.5px ${MONO};color:${f.tag === 'latest' ? 'var(--grn)' : 'var(--dim)'};background:${f.tag === 'latest' ? 'var(--grn-soft)' : 'var(--chip)'};padding:1px 7px;border-radius:4px">${f.tag}</span>
            <span style="font:10.5px ${MONO};color:var(--faint)">${f.meta}</span>
          </div>`;
        })}
      </div>
      <div style="font-size:10.5px;color:var(--faint)">Versions are managed by the tool — checksum, device type and version are read from the file header.</div>
    </div>

    <div style="background:var(--panel);border:1px solid var(--bd);border-radius:8px;padding:14px 16px;display:flex;flex-direction:column;gap:12px">
      <div style="font-weight:600;font-size:13px">Download</div>
      <div style="font-size:11px;color:var(--dim);font-weight:600">TARGETS <span style="font-weight:400;color:var(--faint)">· selection from Devices box</span></div>
      ${!selDevs.length && html`<div style="border:1px solid var(--bd2);border-radius:7px;padding:14px;color:var(--faint);font-size:12px">No devices selected — tick target devices in the Devices box on the left.</div>`}
      <div style="display:flex;flex-direction:column;gap:8px">
        ${selDevs.map((d) => {
          const p = Math.round(w.prog[String(d.node)] || 0);
          const done = p >= 100;
          return html`
          <div style="border:1px solid var(--bd2);border-radius:7px;padding:9px 12px">
            <div style="display:flex;align-items:center;gap:10px">
              <span style="font:600 12px ${MONO};color:var(--acc)">${String(d.node).padStart(2, '0')}</span>
              <span style="font-weight:600;flex:1">${d.name}</span>
              <span style="font:11px ${MONO};color:var(--faint)">v${d.fw} → v${w.sel}</span>
              <span style="font-weight:600;font-size:11px;color:${done ? 'var(--grn)' : p > 0 ? 'var(--acc)' : 'var(--faint)'}">${done ? 'DONE ✓' : p > 0 ? p + '%' : w.run ? 'queued' : 'ready'}</span>
            </div>
            <div style="height:5px;border-radius:3px;background:var(--bd);overflow:hidden;margin-top:7px"><span style="display:block;width:${p}%;height:100%;background:${done ? 'var(--grn)' : 'var(--acc)'};transition:width .4s"></span></div>
          </div>`;
        })}
      </div>
      <div style="display:flex;gap:8px">
        <div onClick=${() => send('swdl_mode', { mode: 'sdo' })} style="flex:1;border:1px solid ${w.mode === 'sdo' ? 'var(--acc)' : 'var(--bd)'};background:${w.mode === 'sdo' ? 'var(--acc-soft)' : 'transparent'};border-radius:7px;padding:9px 11px;cursor:pointer">
          <div style="font-weight:600;font-size:12px">SDO · serial</div><div style="font-size:10.5px;color:var(--dim);margin-top:2px">One device after another. Safe, works in any NMT state.</div>
        </div>
        <div onClick=${() => send('swdl_mode', { mode: 'pdo' })} style="flex:1;border:1px solid ${w.mode === 'pdo' ? 'var(--acc)' : 'var(--bd)'};background:${w.mode === 'pdo' ? 'var(--acc-soft)' : 'transparent'};border-radius:7px;padding:9px 11px;cursor:pointer">
          <div style="font-weight:600;font-size:12px">PDO · parallel</div><div style="font-size:10.5px;color:var(--dim);margin-top:2px">Block transfer to all targets simultaneously. Fast.</div>
        </div>
      </div>
      <div style="display:flex;align-items:center;gap:12px">
        <span class="hv-b" onClick=${() => send('swdl_start')} style="background:${w.run || !selDevs.length ? 'var(--faint)' : 'var(--acc)'};color:#fff;font-weight:600;padding:8px 22px;border-radius:7px;cursor:pointer">${w.run ? 'Downloading…' : '⇩ Start download'}</span>
        <span style="font:11px ${MONO};color:var(--dim)">${status}</span>
      </div>
    </div>
  </div>`;
}

// ------------------------------------------------------------------ trace --
function fmtSize(bytes) {
  return bytes < 1048576 ? `${Math.round(bytes / 1024)} kB` : `${(bytes / 1048576).toFixed(1)} MB`;
}

function fmtSpan(sec) {
  const m = Math.floor(sec / 60);
  return m ? `${m}m ${Math.round(sec % 60)}s` : `${Math.round(sec)}s`;
}

function Sparkline({ vals, w = 220, h = 40 }) {
  const max = Math.max(0.1, ...vals);
  const pts = vals.map((v, i) => `${((i / Math.max(1, vals.length - 1)) * w).toFixed(1)},${(h - 2 - (v / max) * (h - 6)).toFixed(1)}`).join(' ');
  return html`
  <svg width=${w} height=${h} style="display:block">
    <polyline points=${pts} fill="none" stroke="var(--acc)" stroke-width="1.5" />
  </svg>`;
}

// -------------------------------------------------------------- signal plot --
const PLOT_COLORS = ['var(--acc)', 'var(--grn)', 'var(--amb)', 'var(--red)'];

function TracePlot({ plot, connected }) {
  const sel = plot.sel || [];
  const series = plot.series || {};
  if (!connected) return html`<div style="flex:1;display:grid;place-items:center;color:var(--faint);font-size:12.5px">offline — connect to plot signals</div>`;
  if (!sel.length) return html`
    <div style="flex:1;display:grid;place-items:center;text-align:center;color:var(--faint);font-size:12.5px;line-height:1.7">
      no signals selected<br/>click the <span style="font-family:${MONO}">∿</span> icon next to an object in Objects or Favorites to plot it
    </div>`;
  const lines = sel.map((r, i) => {
    const key = `${r.idx}:${r.sub}`;
    return { key, label: r.label || key, color: PLOT_COLORS[i % PLOT_COLORS.length], pts: series[key] || [] };
  });
  const allT = lines.flatMap((l) => l.pts.map((p) => p[0]));
  const tMax = allT.length ? Math.max(...allT) : 0;
  const tMin = allT.length ? Math.min(...allT) : 0;
  const span = Math.max(0.001, tMax - tMin);
  const W = 900, H = 280;
  return html`
  <div style="flex:1;min-height:0;overflow:auto;background:var(--panel2);padding:14px 18px;display:flex;flex-direction:column;gap:12px">
    <div style="background:var(--panel);border:1px solid var(--bd);border-radius:8px;padding:12px 14px">
      ${allT.length
        ? html`
          <svg viewBox="0 0 ${W} ${H}" style="display:block;width:100%;height:${H}px">
            <line x1="0" y1=${H - 0.5} x2=${W} y2=${H - 0.5} stroke="var(--bd)" stroke-width="1" />
            ${lines.map((l) => {
              if (l.pts.length < 2) return '';
              const vals = l.pts.map((p) => p[1]);
              const vMin = Math.min(...vals), vMax = Math.max(...vals);
              const vSpan = Math.max(1e-9, vMax - vMin);
              const pts = l.pts.map((p) => {
                const x = ((p[0] - tMin) / span) * W;
                const y = H - 6 - ((p[1] - vMin) / vSpan) * (H - 12);
                return `${x.toFixed(1)},${y.toFixed(1)}`;
              }).join(' ');
              return html`<polyline points=${pts} fill="none" stroke=${l.color} stroke-width="1.75" />`;
            })}
          </svg>`
        : html`<div style="height:${H}px;display:grid;place-items:center;color:var(--faint);font-size:12px">waiting for the first sample…</div>`}
      ${allT.length > 0 && html`<div style="text-align:right;font:10.5px ${MONO};color:var(--faint);margin-top:4px">span ${fmtSpan(span)}</div>`}
    </div>
    <div style="display:flex;flex-direction:column;gap:6px">
      ${lines.map((l) => {
        const last = l.pts.length ? l.pts[l.pts.length - 1][1] : null;
        const vals = l.pts.map((p) => p[1]);
        const range = vals.length ? `${Math.min(...vals)} … ${Math.max(...vals)}` : '—';
        return html`
        <div style="display:flex;align-items:center;gap:10px;padding:7px 12px;background:var(--panel);border:1px solid var(--bd);border-radius:7px">
          <span style="width:10px;height:10px;border-radius:50%;background:${l.color};flex:none"></span>
          <span style="font:10.5px ${MONO};color:var(--acc);flex:none">${l.key}</span>
          <span style="flex:1;min-width:0;color:var(--mid);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${l.label}</span>
          <span style="font:600 12px ${MONO};color:var(--tx)">${last ?? '—'}</span>
          <span style="font:10.5px ${MONO};color:var(--faint);min-width:120px;text-align:right">${range}</span>
          <span class="hv" onClick=${() => send('plot_toggle', { idx: l.key.split(':')[0], sub: l.key.split(':')[1] })} title="remove from plot"
            style="color:var(--faint);cursor:pointer">✕</span>
        </div>`;
      })}
    </div>
    <div style="display:flex;justify-content:space-between;align-items:center">
      <span style="font-size:10.5px;color:var(--faint)">Each line auto-scaled to its own min/max — shapes, not absolute heights, are comparable across signals. Pausing the trace freezes sampling.</span>
      <span class="hv" onClick=${() => send('plot_clear')} style="${btn.ghost}font-size:11px;padding:4px 10px;border-radius:6px;cursor:pointer">Clear all</span>
    </div>
  </div>`;
}

function TraceStats({ st, connected }) {
  if (!connected) return html`<div style="flex:1;display:grid;place-items:center;color:var(--faint);font-size:12.5px">offline — connect to collect statistics</div>`;
  if (!st || !st.total) return html`<div style="flex:1;display:grid;place-items:center;color:var(--faint);font-size:12.5px">no frames observed yet</div>`;
  const card = 'background:var(--panel);border:1px solid var(--bd);border-radius:8px;padding:11px 14px;display:flex;flex-direction:column;gap:4px';
  const lbl = 'font:600 10px ' + MONO + ';color:var(--faint);letter-spacing:.08em';
  const big = 'font:600 20px ' + MONO + ';color:var(--tx)';
  const load = st.loadHist.length ? st.loadHist[st.loadHist.length - 1] : 0;
  const peak = st.loadHist.length ? Math.max(...st.loadHist) : 0;
  const clsOrder = ['NMT', 'SDO', 'PDO', 'EMCY', 'HB', 'other'];
  const cols = '70px minmax(200px,1fr) 56px 90px 80px minmax(140px,300px)';
  return html`
  <div style="flex:1;min-height:0;overflow:auto;background:var(--panel2);padding:14px 18px;display:flex;flex-direction:column;gap:12px">
    <div style="display:grid;grid-template-columns:repeat(4,minmax(170px,1fr));gap:12px">
      <div style="${card}">
        <span style="${lbl}">BUS LOAD · last 60 s</span>
        <div style="display:flex;align-items:flex-end;gap:12px">
          <span style="${big}">${load.toFixed(1)}%</span>
          <span style="font:10.5px ${MONO};color:var(--faint);padding-bottom:3px">peak ${peak.toFixed(1)}%</span>
        </div>
        <${Sparkline} vals=${st.loadHist.length ? st.loadHist : [0]} />
      </div>
      <div style="${card}">
        <span style="${lbl}">FRAMES</span>
        <span style="${big}">${st.total.toLocaleString('en')}</span>
        <span style="font:10.5px ${MONO};color:var(--dim)">${st.rate}/s · observed ${fmtSpan(st.span)}</span>
      </div>
      <div style="${card}">
        <span style="${lbl}">ERROR FRAMES</span>
        <span style="${big};color:${st.err ? 'var(--red)' : 'var(--grn)'}">${st.err}</span>
        <span style="font:10.5px ${MONO};color:var(--faint)">since connect</span>
      </div>
      <div style="${card}">
        <span style="${lbl}">COB-IDS</span>
        <span style="${big}">${st.cobs.length + st.restCobs}</span>
        <span style="font:10.5px ${MONO};color:var(--faint)">distinct identifiers</span>
      </div>
    </div>
    <div style="display:flex;gap:8px;align-items:center">
      <span style="${lbl}">BY CLASS</span>
      ${clsOrder.filter((c) => st.classes[c]).map((c) => html`
        <span style="border:1px solid var(--bd);background:var(--panel);color:var(--mid);font:600 10.5px ${MONO};padding:3px 10px;border-radius:9px">${c} <b style="color:var(--tx)">${st.classes[c].toLocaleString('en')}</b></span>`)}
    </div>
    <div style="background:var(--panel);border:1px solid var(--bd);border-radius:8px;overflow:hidden">
      <div style="display:grid;grid-template-columns:${cols};gap:0 12px;padding:7px 14px;border-bottom:1px solid var(--bd);font:600 10px ${MONO};color:var(--faint);letter-spacing:.08em">
        <span>COB-ID</span><span>DECODED</span><span>CLASS</span><span style="text-align:right">COUNT</span><span style="text-align:right">FRAMES/S</span><span>SHARE</span>
      </div>
      ${st.cobs.map((r) => { const share = st.total ? 100 * r.n / st.total : 0; return html`
      <div style="display:grid;grid-template-columns:${cols};gap:0 12px;padding:4px 14px;border-bottom:1px solid var(--bd2);font:11px ${MONO};color:var(--mid);align-items:center">
        <span style="color:var(--acc)">${r.cob}</span>
        <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${r.dec}</span>
        <span style="color:${r.cls === 'EMCY' ? 'var(--red)' : 'var(--dim)'}">${r.cls || '—'}</span>
        <span style="text-align:right;color:var(--tx)">${r.n.toLocaleString('en')}</span>
        <span style="text-align:right">${r.rate ? r.rate.toFixed(1) : '—'}</span>
        <span style="display:flex;align-items:center;gap:8px">
          <span style="flex:1;height:5px;border-radius:3px;background:var(--bd);overflow:hidden"><span style="display:block;width:${share.toFixed(1)}%;height:100%;background:${r.cls === 'EMCY' ? 'var(--red)' : 'var(--acc)'}"></span></span>
          <span style="width:44px;text-align:right;color:var(--dim)">${share < 0.1 && share > 0 ? '<0.1' : share.toFixed(1)}%</span>
        </span>
      </div>`; })}
      ${st.restCobs > 0 && html`
      <div style="padding:6px 14px;font:10.5px ${MONO};color:var(--faint)">… + ${st.restCobs} more COB-IDs (${st.restN.toLocaleString('en')} frames)</div>`}
    </div>
    <div style="font-size:10.5px;color:var(--faint)">Counters run since connect or trace clear; frames/s over the last 5 s. Pausing the trace freezes the statistics.</div>
  </div>`;
}

function TracePage({ s }) {
  const [usTime, setUsTime] = useState(localStorage.getItem('cb-us-time') === '1');
  const [view, setView] = useState(localStorage.getItem('cb-trace-view') || 'trace');
  const setViewMode = (v) => { setView(v); localStorage.setItem('cb-trace-view', v); };
  const toggleUs = () => { const n = !usTime; setUsTime(n); localStorage.setItem('cb-us-time', n ? '1' : '0'); };
  // rows carry 6 decimals ("HH:MM:SS.ffffff"); ms mode cuts the last three.
  // Older captures may hold ms-only stamps — those render as-is either way.
  const fmtTime = (t) => (!usTime && t && t.length === 15) ? t.slice(0, 12) : t;
  const cols = `${usTime ? '124px' : '96px'} 38px 60px 32px 200px 200px minmax(260px,420px) 92px`;
  const hide = s.trace.hide || [];
  const toggle = (f) => send('trace_filter', { hide: hide.includes(f) ? hide.filter((x) => x !== f) : [...hide, f] });
  const rows = s.trace.rows;
  const saved = s.trace.saved || [];
  const auto = s.trace.auto || {};
  const handleImportFile = (ev) => {
    const file = ev.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      const b64 = (reader.result.split(',')[1]) || '';
      send('trace_import', { filename: file.name, fmt: 'candump', data: b64 });
    };
    reader.readAsDataURL(file);
    ev.target.value = '';
  };
  return html`
  <!-- Two bands, split by what the controls are for: the first acts on the
       record and its capture files, the second says how it is being looked
       at. In one row they were a dozen unrelated controls in a line, and
       even on a wide screen the eye had to read all of them to find the
       one it wanted; on a narrow one flex shrank each button until its own
       label broke in two ("⤓" over "Save").
       Not a right-hand column like Objects and Tests have: those carry
       *content* you work with while the page is open (favorites, the case
       list). This is momentary controls, and a permanent column would
       spend 300 px of width — on the widest table in the app, whose
       OBJECT column is the first thing to lose it — on chips that get
       touched once a session. Two bands cost one row of frames off a
       table that scrolls anyway. -->
  <div style="flex:none;background:var(--panel);border-bottom:1px solid var(--bd)">
  <div style="display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:9px 18px 8px;white-space:nowrap">
    <span onClick=${() => send('trace_toggle')} style="${btn.acc}font-size:11.5px;padding:5px 14px;border-radius:6px;cursor:pointer">${s.trace.paused ? '▶ Resume' : '❚❚ Pause'}</span>
    <span class="hv" onClick=${() => send('trace_clear')} style="${btn.ghost}font-size:11.5px;padding:5px 12px;border-radius:6px;cursor:pointer">Clear</span>
    <span class="hv" onClick=${() => send('trace_save')} style="${btn.ghost}font-size:11.5px;padding:5px 12px;border-radius:6px;cursor:pointer">⤓ Save</span>
    <!-- red while auto.warn is set: autosave is still on and still
         trying, but nothing is reaching the disk, and that has to be
         visible on an unattended run rather than only in the log. -->
    <span onClick=${() => send('trace_autosave')}
      title=${`Autosave: write every recorded frame to a capture file as it arrives, unfiltered. The trace in memory is a ring buffer, so on a long run the beginning is gone by the time anything is worth looking at. A new file starts on connect. Autosaved captures are kept for 14 days — less if the disk gets tight: the bench keeps 2 GB clear and drops the oldest early. It never switches itself off; if it cannot write it waits, shows why here, and carries on the moment it can. Every removal is in the state log.${auto.file ? `\n\nwriting to ${auto.file}` : ''}`}
      style="border:1px solid ${auto.warn ? 'var(--red)' : auto.on ? 'var(--acc-bd)' : 'var(--inp)'};background:${auto.warn ? 'var(--red-soft)' : auto.on ? 'var(--acc-soft)' : 'transparent'};color:${auto.warn ? 'var(--red)' : auto.on ? 'var(--acc)' : 'var(--mid)'};font:600 10.5px ${MONO};padding:4px 10px;border-radius:9px;cursor:pointer;white-space:nowrap">${auto.on ? '⏺' : '⭘'} autosave${auto.warn ? ` · ${auto.warn}` : auto.on && auto.file ? ` · ${fmtSize(auto.bytes || 0)}` : ''}</span>
    ${saved.length > 0 && html`
      <select onChange=${(e) => { if (e.target.value) send('trace_load', { file: e.target.value }); e.target.value = ''; }}
        style="border:1px solid var(--inp);background:var(--panel);color:var(--tx);border-radius:5px;padding:4px 6px;font:11px ${MONO};outline:none;max-width:240px">
        <option value="">⤒ Load capture…</option>
        ${saved.map((f) => html`<option value=${f.file}>${f.file} · ${fmtSize(f.size)}</option>`)}
      </select>`}
    ${s.trace.loaded && html`
      <span style="font:11px ${MONO};color:var(--acc)">📼 ${s.trace.loaded}</span>
      <span class="hv" onClick=${() => send('trace_del_saved', { file: s.trace.loaded })} title="delete this capture file"
        style="${btn.ghost}font-size:11.5px;padding:5px 8px;border-radius:6px;cursor:pointer">✕</span>`}
    <span style="width:1px;height:18px;background:var(--bd)"></span>
    <a class="hv" href="/api/trace/export.csv" title="export the filtered trace as CSV"
      style="${btn.ghost}font-size:11.5px;padding:5px 12px;border-radius:6px;cursor:pointer;text-decoration:none">⤓ CSV</a>
    <a class="hv" href="/api/trace/export/candump" title="export the filtered trace as a SocketCAN candump -l log"
      style="${btn.ghost}font-size:11.5px;padding:5px 12px;border-radius:6px;cursor:pointer;text-decoration:none">⤓ candump</a>
    <input type="file" id="trace-import-input" accept=".log,.txt,.asc" style="display:none" onChange=${handleImportFile} />
    <span class="hv" onClick=${() => document.getElementById('trace-import-input').click()} title="import a SocketCAN candump -l log file"
      style="${btn.ghost}font-size:11.5px;padding:5px 12px;border-radius:6px;cursor:pointer">⤒ Import…</span>
  </div>
  <div style="display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:7px 18px 9px;border-top:1px solid var(--bd2);white-space:nowrap">
    ${[['trace', 'Trace'], ['stats', 'Stats'], ['plot', 'Plot']].map(([v, label]) => { const on = view === v; return html`
      <span onClick=${() => setViewMode(v)}
        style="border:1px solid ${on ? 'var(--acc-bd)' : 'var(--inp)'};background:${on ? 'var(--acc-soft)' : 'transparent'};color:${on ? 'var(--acc)' : 'var(--mid)'};font:600 10.5px ${MONO};padding:3px 10px;border-radius:9px;cursor:pointer">${label}${v === 'plot' && (s.trace.plot.sel || []).length ? ` (${s.trace.plot.sel.length})` : ''}</span>`; })}
    <span style="width:1px;height:18px;background:var(--bd)"></span>
    <span style="font-size:11px;color:var(--dim);font-weight:600">SHOW</span>
    ${['NMT', 'SDO', 'PDO', 'EMCY', 'HB'].map((f) => { const off = hide.includes(f); return html`
      <span onClick=${() => toggle(f)}
        style="border:1px solid ${off ? 'var(--inp)' : 'var(--acc-bd)'};background:${off ? 'transparent' : 'var(--acc-soft)'};color:${off ? 'var(--faint)' : 'var(--acc)'};font:600 10.5px ${MONO};padding:3px 9px;border-radius:9px;cursor:pointer">${f}</span>`; })}
    <span onClick=${() => send('trace_devfilter')} title="Show frames from all devices or only from the devices selected in the Devices box — broadcast frames (NMT, SYNC) are always shown"
      style="border:1px solid ${s.trace.devSel ? 'var(--acc-bd)' : 'var(--inp)'};background:${s.trace.devSel ? 'var(--acc-soft)' : 'transparent'};color:${s.trace.devSel ? 'var(--acc)' : 'var(--mid)'};font:600 10.5px ${MONO};padding:3px 10px;border-radius:9px;cursor:pointer">dev: ${s.trace.devSel ? 'selected' : 'all'}</span>
    <span style="width:1px;height:18px;background:var(--bd)"></span>
    <span onClick=${toggleUs} title="Timestamp resolution: milliseconds / microseconds"
      style="border:1px solid ${usTime ? 'var(--acc-bd)' : 'var(--inp)'};background:${usTime ? 'var(--acc-soft)' : 'transparent'};color:${usTime ? 'var(--acc)' : 'var(--mid)'};font:600 10.5px ${MONO};padding:3px 10px;border-radius:9px;cursor:pointer">${usTime ? 'µs' : 'ms'}</span>
    <span style="margin-left:auto;font:11px ${MONO};color:var(--faint)">${s.connected ? `${s.trace.match} / ${s.trace.total} frames` : 'offline'}</span>
  </div>
  </div>
  ${view === 'stats' && html`<${TraceStats} st=${s.trace.stats} connected=${s.connected} />`}
  ${view === 'plot' && html`<${TracePlot} plot=${s.trace.plot} connected=${s.connected} />`}
  ${view === 'trace' && html`
  <div style="flex:1;min-height:0;overflow:auto;background:var(--panel2)">
    <div style="display:grid;grid-template-columns:${cols};padding:6px 0 6px 18px;border-bottom:1px solid var(--bd);font:600 10px ${MONO};color:var(--faint);letter-spacing:.08em;position:sticky;top:0;background:var(--panel)">
      <span>TIME</span><span>DIR</span><span>COB-ID</span><span>LEN</span><span>DATA</span><span>DECODED</span><span>OBJECT</span><span>DEC</span>
    </div>
    ${rows.map((r) => { const pdoN = r.cls === 'PDO' ? (r.dec.match(/PDO([1-4])/) || [])[1] : null;
      const bg = r.cls === 'EMCY' ? 'var(--red-soft)' : pdoN ? `var(--pdo${pdoN})` : 'transparent'; return html`
      <div style="display:grid;grid-template-columns:${cols};padding:3.5px 0 3.5px 18px;border-bottom:1px solid var(--bd2);font:11px ${MONO};background:${bg};color:${r.flag === 'red' ? 'var(--red)' : 'var(--mid)'}">
        <span style="color:var(--faint)">${fmtTime(r.time)}</span><span>${r.dir}</span><span style="color:var(--acc)">${r.cob}</span><span>${r.len}</span>${r.cls === 'SDO' && r.flag !== 'red' && r.data.length > 12
          ? html`<span>${r.data.slice(0, 12)}<span style="color:var(--hl);font-weight:600">${r.data.slice(12)}</span></span>`
          : html`<span>${r.data}</span>`}<span>${r.dec}</span><span style="color:var(--hl);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${r.obj || ''}</span><span style="color:${(r.val || '').startsWith('abort') ? 'var(--red)' : 'var(--acc)'}">${r.val || ''}</span>
      </div>`; })}
  </div>`}`;
}

// server-side directory picker behind the Browse… buttons: the browser can't
// open a native dialog for paths on the server, so the tool renders its own
function BrowseDialog({ b }) {
  return html`
  <div onClick=${() => send('browse_close')} style="position:fixed;inset:0;background:rgba(10,14,20,.4);z-index:80;display:grid;place-items:center">
    <div onClick=${(e) => e.stopPropagation()} style="width:440px;max-height:72vh;background:var(--panel);border:1px solid var(--bd);border-radius:10px;display:flex;flex-direction:column;overflow:hidden;box-shadow:0 12px 40px rgba(0,0,0,.3)">
      <div style="padding:11px 14px;border-bottom:1px solid var(--bd);font-weight:600;font-size:12.5px;color:var(--tx)">Select ${({ tc: 'TestCases', res: 'Results', eds: 'EDS' })[b.which] || b.which} folder</div>
      <div style="padding:8px 14px;font:11px ${MONO};color:var(--mid);border-bottom:1px solid var(--bd2);overflow-wrap:anywhere">${b.path}</div>
      <div style="flex:1;min-height:140px;overflow:auto;padding:4px 0">
        ${b.hasParent && html`<div class="hv" onClick=${() => send('browse_nav', { dir: '..' })} style="padding:6px 14px;cursor:pointer;font:11.5px ${MONO};color:var(--faint)">↰ ..</div>`}
        ${b.error && html`<div style="padding:10px 14px;color:var(--red);font-size:11.5px">${b.error}</div>`}
        ${b.dirs.map((d) => html`<div class="hv" onClick=${() => send('browse_nav', { dir: d })} style="padding:6px 14px;cursor:pointer;font:11.5px ${MONO};color:var(--tx)">▸ ${d}</div>`)}
        ${!b.error && !b.dirs.length && html`<div style="padding:10px 14px;color:var(--faint);font-size:11.5px">no subfolders</div>`}
      </div>
      <div style="display:flex;gap:8px;justify-content:flex-end;padding:10px 14px;border-top:1px solid var(--bd)">
        <span class="hv" onClick=${() => send('browse_close')} style="${btn.ghost}font-size:11.5px;padding:6px 14px;border-radius:6px;cursor:pointer">Cancel</span>
        <span class="hv-b" onClick=${() => send('browse_select')} style="background:var(--acc);color:#fff;font-weight:600;font-size:11.5px;padding:6px 14px;border-radius:6px;cursor:pointer">Select this folder</span>
      </div>
    </div>
  </div>`;
}

// -------------------------------------------------------------------- app --
function App() {
  const s = useServerState();
  const [ui, setUi] = useState({
    page: 'setup',
    theme: localStorage.getItem('cb-theme') || 'light',
    menuNode: null,
    logOpen: true,
    objGroup: 'manu',
    testFilter: '',
  });
  useEffect(() => { localStorage.setItem('cb-theme', ui.theme); }, [ui.theme]);

  if (!s) {
    return html`<div style="height:100vh;display:grid;place-items:center;background:#f5f6f8;color:#7a8494;font:13px 'IBM Plex Sans',system-ui,sans-serif">Connecting to CANopen Bench server…</div>`;
  }

  const dark = ui.theme === 'dark';
  const selDevs = s.devices.filter((d) => d.sel);
  const edsCur = selDevs[0] ? selDevs[0].eds : 'no device selected';
  const adapterInfo = s.adapters.find((a) => a.key === s.adapter);
  const titles = {
    setup: ['Setup', 'workspace · interface · machine control'],
    objects: ['Object Access', 'EDS: ' + edsCur],
    tests: ['System Tests', s.tests.activeSuite ? 'suite: ' + s.tests.activeSuite : 'no suite loaded'],
    swdl: ['Software Download', 'SDO serial · PDO parallel'],
    trace: ['CAN Trace', s.connected ? 'live' : 'offline'],
    about: ['About', 'project · docs · license'],
  };
  const logFg = { emcy: 'var(--red)', emcy0: 'var(--red)', nmt: 'var(--acc)', test: 'var(--amb)' };
  const logs = s.logs.slice(-30).reverse();

  return html`
  <div data-theme=${ui.theme} style="display:flex;height:100vh;min-height:780px;min-width:1400px;background:var(--bg);color:var(--tx);font-size:12.5px">
    <${Sidebar} s=${s} ui=${ui} setUi=${setUi} />
    <div style="flex:1;display:flex;flex-direction:column;min-width:0;min-height:0">
      <div style="height:52px;flex:none;background:var(--panel);border-bottom:1px solid var(--bd);display:flex;align-items:center;gap:14px;padding:0 18px">
        <span style="font-weight:600;font-size:14px">${titles[ui.page][0]}</span>
        <span style="font:11px ${MONO};color:var(--dim);background:var(--chip);padding:3px 8px;border-radius:5px">${titles[ui.page][1]}</span>
        <div style="margin-left:auto;display:flex;align-items:center;gap:12px">
          ${(() => {
            const cycRows = (s.raw || []).filter((r) => r.run).length;
            const parts = [];
            if (cycRows) parts.push(`${cycRows} cyclic row${cycRows > 1 ? 's' : ''}`);
            if (s.sync.run) parts.push(`SYNC every ${s.sync.ms} ms`);
            if (!parts.length) return '';
            return html`<span class="hv" onClick=${() => setUi({ ...ui, page: cycRows ? 'objects' : 'setup' })}
              title="Cyclic transmit active: ${parts.join(' · ')} — runs server-side until stopped or the bus disconnects. Click to open the controls."
              style="white-space:nowrap;border:1px solid var(--acc-bd);background:var(--acc-soft);color:var(--acc);font:600 10.5px ${MONO};padding:4px 10px;border-radius:9px;cursor:pointer">⟳ TX ${parts.join(' · ')}</span>`;
          })()}
          <span class="hv" onClick=${() => setUi({ ...ui, theme: dark ? 'light' : 'dark' })} title="Toggle theme"
            style="width:28px;height:28px;display:grid;place-items:center;border:1px solid var(--inp);border-radius:6px;cursor:pointer;color:var(--mid);font-size:13px">${dark ? '☀' : '☾'}</span>
          <span style="width:8px;height:8px;border-radius:50%;background:${s.connected ? 'var(--grn)' : 'var(--faint)'}"></span>
          <span style="font-size:12px;color:var(--mid)">${s.connected ? adapterInfo.conn : 'not connected'}</span>
          <span class="hv-b" onClick=${() => send('connect_toggle')}
            style="font-weight:600;font-size:12px;color:${s.connected ? 'var(--acc)' : '#fff'};border:1px solid ${s.connected ? 'var(--acc-bd)' : 'var(--grn)'};padding:5px 14px;border-radius:6px;background:${s.connected ? 'var(--acc-soft)' : 'var(--grn)'};cursor:pointer">${s.connected ? 'Disconnect' : 'Connect'}</span>
        </div>
      </div>

      ${ui.page === 'setup' && html`<${SetupPage} s=${s} />`}
      ${ui.page === 'objects' && html`<${ObjectsPage} s=${s} ui=${ui} setUi=${setUi} />`}
      ${ui.page === 'tests' && html`<${TestsPage} s=${s} ui=${ui} setUi=${setUi} />`}
      ${ui.page === 'swdl' && html`<${SwdlPage} s=${s} />`}
      ${ui.page === 'trace' && html`<${TracePage} s=${s} />`}
      ${ui.page === 'about' && html`<${AboutPage} />`}
      ${s.browse && html`<${BrowseDialog} b=${s.browse} />`}

      <div style="flex:none;border-top:1px solid var(--bd);background:var(--panel);display:flex;flex-direction:column">
        <div class="hv" onClick=${() => setUi({ ...ui, logOpen: !ui.logOpen })} style="display:flex;align-items:center;gap:10px;padding:6px 18px;cursor:pointer">
          <span style="font-weight:600;font-size:11.5px">State log</span>
          ${s.emcyNew > 0 && html`<span onClick=${(e) => { e.stopPropagation(); send('emcy_ack'); }}
            style="font-weight:600;font-size:10.5px;background:var(--red-soft);color:var(--red);padding:2px 8px;border-radius:9px">EMCY ${s.emcyNew} new · ack</span>`}
          <span style="margin-left:auto;color:var(--faint);font-size:11px">${ui.logOpen ? 'collapse ▾' : 'expand ▴'}</span>
        </div>
        ${ui.logOpen && html`
          <div style="height:96px;overflow:auto;padding:2px 18px 8px;font:11px ${MONO};line-height:1.75;border-top:1px solid var(--bd2)">
            ${logs.map((l) => html`<div style="color:${logFg[l.type] || 'var(--mid)'}">${l.t}  ${l.msg}</div>`)}
          </div>`}
      </div>
    </div>
  </div>`;
}

render(html`<${App} />`, document.getElementById('app'));
