// CANopen Bench frontend — pixel-faithful port of the Claude Design prototype.
// Server (FastAPI) owns all bench state; this file renders it and sends actions.
import { html, render, useState, useEffect, useRef } from '/static/vendor/preact-htm.module.js';

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
        <li><span style="font-family:${MONO}">docs/erweiterungs-konzept.md</span> — vendor extension packages</li>
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
    <div style="background:var(--panel);border:1px solid var(--bd);border-radius:8px;padding:14px 16px;display:flex;flex-direction:column;gap:12px">
      <div style="font-weight:600;font-size:13px">Bus interface</div>
      <div style="display:flex;gap:8px">
        ${s.adapters.map((a) => {
          const on = s.adapter === a.key;
          return html`
          <div class="hv-bd" onClick=${() => send('set_adapter', { adapter: a.key })}
            style="flex:1;border:1px solid ${on ? 'var(--acc)' : 'var(--bd)'};background:${on ? 'var(--acc-soft)' : 'var(--panel)'};border-radius:7px;padding:9px 11px;cursor:pointer">
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

    <div style="background:var(--panel);border:1px solid var(--bd);border-radius:8px;padding:14px 16px;display:flex;flex-direction:column;gap:12px">
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
  const cols = '62px 30px minmax(140px,1fr) 40px 34px 96px 132px';
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
  const accByKey = {};
  for (const rows of Object.values(s.objects.catalog)) {
    for (const r of rows) accByKey[r[0] + ':' + r[1]] = r[4];
  }
  const favPanel = html`
    <div style="border:1px solid var(--bd);border-radius:8px;overflow:hidden">
      <div style="padding:8px 12px;font-weight:600;font-size:12px;border-bottom:1px solid var(--bd2);background:var(--panel2)">${fav.rows.length} object${fav.rows.length === 1 ? '' : 's'}</div>
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
            <${SyncInput} value=${cur ?? ''} title="staged value — Write sends it"
              onCommit=${(v) => send('obj_set', { idx, sub, val: v })}
              style="border:1px solid var(--inp);background:var(--panel);color:${cur ? 'var(--acc)' : 'var(--tx)'};border-radius:4px;padding:2px 7px;font:600 12px ${MONO};width:86px;outline:none;text-align:right" />
            <span class="hv" onClick=${() => send('obj_write', { idx, sub })} title="write the staged value to the device"