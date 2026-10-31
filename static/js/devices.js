// Settings → Devices + Allowed Directories manager.
import uiModule from './ui.js?v=20260916largetoolscroll1';

const API = window.location.origin;

async function _json(url, opts = {}) {
  const res = await fetch(url, { credentials: 'same-origin', ...opts });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = data.detail || data.message || res.statusText;
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return data;
}

function _el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  Object.entries(attrs).forEach(([k, v]) => {
    if (k === 'className') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2).toLowerCase(), v);
    else node.setAttribute(k, v);
  });
  (Array.isArray(children) ? children : [children]).forEach((c) => {
    if (c == null) return;
    node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  });
  return node;
}

let _selectedDeviceId = 'device:local';
let _browsePath = '';
let _selectedPaths = new Set();

export async function refreshDevicesPanel() {
  const root = document.getElementById('devices-panel-root');
  if (!root) return;
  root.innerHTML = '';
  let devices = [];
  let grants = [];
  try {
    const d = await _json(`${API}/api/devices`);
    devices = d.devices || [];
    const g = await _json(`${API}/api/allowed-directories`);
    grants = g.directories || [];
  } catch (err) {
    root.appendChild(_el('p', { className: 'admin-toggle-sub', text: `Failed to load devices: ${err.message}` }));
    return;
  }
  root.appendChild(_buildDevicesCard(devices));
  root.appendChild(_buildGrantsCard(devices, grants));
  root.appendChild(_buildBrowserCard(devices));
}

function _buildDevicesCard(devices) {
  const card = _el('div', { className: 'admin-card' }, [
    _el('h2', { text: 'Devices' }),
    _el('p', {
      className: 'admin-toggle-sub',
      text: 'Machines Mabel can reach from this desktop backend. The browser device never becomes the filesystem.',
    }),
  ]);
  const list = _el('div', { className: 'settings-col', id: 'devices-list' });
  devices.forEach((device) => {
    const row = _el('div', { className: 'settings-row', style: 'align-items:center;gap:8px;flex-wrap:wrap;' }, [
      _el('strong', { text: device.display_name }),
      _el('span', { className: 'admin-toggle-sub', text: `${device.transport} · ${device.last_status || 'unknown'}` }),
    ]);
    if (device.last_error) {
      row.appendChild(_el('span', { className: 'admin-toggle-sub', text: device.last_error, style: 'color:var(--red);' }));
    }
    if (device.transport === 'ssh') {
      row.appendChild(_el('button', {
        className: 'confirm-btn-secondary', type: 'button', text: 'Test',
        onClick: async () => {
          try {
            const result = await _json(`${API}/api/devices/${encodeURIComponent(device.id)}/test`, { method: 'POST' });
            if (result.host_key) {
              const ok = confirm(
                `Unknown host key for ${result.host_key.hostname}\n`
                + `Type: ${result.host_key.key_type}\nFingerprint: ${result.host_key.fingerprint}\n\nAccept this host key?`
              );
              if (ok) {
                await _json(`${API}/api/devices/${encodeURIComponent(device.id)}/host-key`, {
                  method: 'POST',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({ accept: true }),
                });
                uiModule.showToast('Host key accepted. Test again.');
              }
            } else if (result.guidance) {
              alert(result.guidance);
            } else if (result.ok) {
              uiModule.showToast('Connection OK');
            } else {
              alert(result.error || 'Connection failed');
            }
            refreshDevicesPanel();
          } catch (err) {
            alert(err.message);
          }
        },
      }));
      if (!device.builtin) {
        row.appendChild(_el('button', {
          className: 'confirm-btn-secondary', type: 'button', text: 'Remove',
          onClick: async () => {
            if (!confirm(`Remove device ${device.display_name}? Dependent grants will block removal unless force-removed.`)) return;
            try {
              await _json(`${API}/api/devices/${encodeURIComponent(device.id)}?remove_grants=true`, { method: 'DELETE' });
              refreshDevicesPanel();
            } catch (err) {
              alert(err.message);
            }
          },
        }));
      }
    }
    list.appendChild(row);
  });
  card.appendChild(list);

  const addRow = _el('div', { className: 'settings-row', style: 'gap:8px;flex-wrap:wrap;margin-top:12px;' });
  const input = _el('input', {
    className: 'settings-select', type: 'text', placeholder: 'ssh user@host or ssh alias',
    style: 'min-width:240px;flex:1;',
  });
  addRow.appendChild(input);
  addRow.appendChild(_el('button', {
    className: 'confirm-btn-primary', type: 'button', text: 'Add Device',
    onClick: async () => {
      const connection = input.value.trim();
      if (!connection) return;
      try {
        await _json(`${API}/api/devices`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ connection }),
        });
        input.value = '';
        uiModule.showToast('Device saved');
        refreshDevicesPanel();
      } catch (err) {
        alert(err.message);
      }
    },
  }));
  card.appendChild(addRow);
  return card;
}

function _buildGrantsCard(devices, grants) {
  const card = _el('div', { className: 'admin-card' }, [
    _el('h2', { text: 'Allowed Directories' }),
    _el('p', {
      className: 'admin-toggle-sub',
      text: 'Persistent grants. Conversations activate a subset of these; selection cannot escalate permissions.',
    }),
  ]);
  const list = _el('div', { className: 'settings-col' });
  if (!grants.length) {
    list.appendChild(_el('p', { className: 'admin-toggle-sub', text: 'No directory grants yet. Browse a device below and add folders.' }));
  }
  grants.forEach((grant) => {
    const row = _el('div', { className: 'settings-row', style: 'align-items:center;gap:8px;flex-wrap:wrap;' }, [
      _el('strong', { text: grant.display_name }),
      _el('span', { className: 'admin-toggle-sub', text: `${grant.device_name || grant.device_id} · ${grant.permission} · ${grant.path}` }),
      _el('button', {
        className: 'confirm-btn-secondary', type: 'button', text: grant.enabled ? 'Disable' : 'Enable',
        onClick: async () => {
          await _json(`${API}/api/allowed-directories/${encodeURIComponent(grant.id)}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: !grant.enabled }),
          });
          refreshDevicesPanel();
        },
      }),
      _el('button', {
        className: 'confirm-btn-secondary', type: 'button', text: 'Remove',
        onClick: async () => {
          if (!confirm(`Remove grant ${grant.display_name}?`)) return;
          await _json(`${API}/api/allowed-directories/${encodeURIComponent(grant.id)}`, { method: 'DELETE' });
          refreshDevicesPanel();
        },
      }),
    ]);
    list.appendChild(row);
  });
  card.appendChild(list);
  return card;
}

function _buildBrowserCard(devices) {
  const card = _el('div', { className: 'admin-card' }, [
    _el('h2', { text: 'Directory Browser' }),
    _el('p', { className: 'admin-toggle-sub', text: 'Browse a registered device and authorize folders. Paths resolve on that device, not in the browser.' }),
  ]);
  const select = _el('select', { className: 'settings-select', id: 'devices-browse-select' });
  devices.forEach((d) => {
    const opt = document.createElement('option');
    opt.value = d.id;
    opt.textContent = d.display_name;
    if (d.id === _selectedDeviceId) opt.selected = true;
    select.appendChild(opt);
  });
  select.addEventListener('change', () => {
    _selectedDeviceId = select.value;
    _selectedPaths = new Set();
    _loadBrowse('');
  });
  card.appendChild(select);
  const pathRow = _el('div', { className: 'settings-row', style: 'gap:8px;margin-top:8px;' });
  const pathInput = _el('input', { className: 'settings-select', id: 'devices-browse-path', style: 'flex:1;' });
  pathRow.appendChild(pathInput);
  pathRow.appendChild(_el('button', {
    className: 'confirm-btn-secondary', type: 'button', text: 'Go',
    onClick: () => _loadBrowse(pathInput.value.trim()),
  }));
  pathRow.appendChild(_el('button', {
    className: 'confirm-btn-secondary', type: 'button', text: 'Up',
    onClick: () => {
      const parent = pathInput.dataset.parent || '';
      if (parent) _loadBrowse(parent);
    },
  }));
  card.appendChild(pathRow);
  const list = _el('div', { id: 'devices-browse-list', className: 'settings-col', style: 'max-height:260px;overflow:auto;margin-top:8px;' });
  card.appendChild(list);
  const perm = _el('select', { className: 'settings-select', id: 'devices-grant-perm' }, [
    _el('option', { value: 'rw', text: 'Read/Write' }),
    _el('option', { value: 'ro', text: 'Read-only' }),
  ]);
  card.appendChild(_el('div', { className: 'settings-row', style: 'gap:8px;margin-top:8px;' }, [
    perm,
    _el('button', {
      className: 'confirm-btn-primary', type: 'button', text: 'Authorize selected',
      onClick: async () => {
        const paths = [..._selectedPaths];
        if (!paths.length && pathInput.value.trim()) paths.push(pathInput.value.trim());
        if (!paths.length) {
          alert('Select one or more folders');
          return;
        }
        const permission = perm.value;
        for (const path of paths) {
          const name = path.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || path;
          await _json(`${API}/api/allowed-directories`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              device_id: _selectedDeviceId,
              display_name: name,
              path,
              permission,
            }),
          });
        }
        _selectedPaths = new Set();
        uiModule.showToast('Directory grant(s) saved');
        refreshDevicesPanel();
      },
    }),
  ]));
  setTimeout(() => _loadBrowse(''), 0);
  return card;
}

async function _loadBrowse(path) {
  const list = document.getElementById('devices-browse-list');
  const pathInput = document.getElementById('devices-browse-path');
  if (!list || !pathInput) return;
  list.textContent = 'Loading…';
  try {
    const q = path ? `?path=${encodeURIComponent(path)}` : '';
    const data = await _json(`${API}/api/devices/${encodeURIComponent(_selectedDeviceId)}/browse${q}`);
    _browsePath = data.path || '';
    pathInput.value = _browsePath;
    pathInput.dataset.parent = data.parent || '';
    list.innerHTML = '';
    (data.dirs || []).forEach((dir) => {
      const checked = _selectedPaths.has(dir.path);
      const row = _el('div', { className: 'settings-row', style: 'gap:8px;align-items:center;' }, [
        _el('input', {
          type: 'checkbox',
          ...(checked ? { checked: 'checked' } : {}),
          onChange: (ev) => {
            if (ev.target.checked) _selectedPaths.add(dir.path);
            else _selectedPaths.delete(dir.path);
          },
        }),
        _el('button', {
          type: 'button', className: 'confirm-btn-secondary', text: dir.name, style: 'flex:1;text-align:left;',
          onClick: () => _loadBrowse(dir.path),
        }),
      ]);
      list.appendChild(row);
    });
    if (!(data.dirs || []).length) {
      list.appendChild(_el('p', { className: 'admin-toggle-sub', text: 'No subfolders (or permission denied).' }));
    }
  } catch (err) {
    list.textContent = err.message;
  }
}

export function initDevicesSettings() {
  refreshDevicesPanel();
}

export default { initDevicesSettings, refreshDevicesPanel };
