// MailMatrix AI desktop wrapper: spawns the Flask backend (app.py) on a free
// localhost port using the repo's Python venv, then opens it in a
// BrowserWindow. The page is plain server-rendered HTML making same-origin
// fetches, so no preload script is needed and context isolation stays on.
const { app, BrowserWindow, shell } = require('electron');
const { spawn } = require('child_process');
const http = require('http');
const net = require('net');
const path = require('path');
const fs = require('fs');

// Dev-first layout: this file lives in <repo>/electron/, and the backend is
// spawned from the repo checkout. A packaged build runs from inside app.asar,
// so it can't derive the repo location — it must be told via MAILMATRIX_REPO.
const REPO_ROOT = process.env.MAILMATRIX_REPO ||
  (app.isPackaged ? null : path.resolve(__dirname, '..'));

let pyProc = null;
let quitting = false;

function pythonPath() {
  const venvPython = path.join(REPO_ROOT, '.venv', 'bin', 'python');
  return fs.existsSync(venvPython) ? venvPython : null;
}

function getFreePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.unref();
    srv.on('error', reject);
    srv.listen(0, '127.0.0.1', () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

function waitForServer(url, child, timeoutMs = 20000) {
  const start = Date.now();
  return new Promise((resolve, reject) => {
    let settled = false;
    child.once('exit', (code) => {
      if (!settled) {
        settled = true;
        reject(new Error(`Python exited with code ${code} before serving`));
      }
    });
    (function poll() {
      const req = http.get(url, (res) => {
        res.resume();
        if (!settled) { settled = true; resolve(); }
      });
      req.on('error', () => {
        if (settled) return;
        if (Date.now() - start > timeoutMs) {
          settled = true;
          reject(new Error('Timed out waiting for the Flask server to start'));
        } else {
          setTimeout(poll, 250);
        }
      });
    })();
  });
}

function killPython() {
  if (pyProc && pyProc.exitCode === null) pyProc.kill('SIGTERM');
  pyProc = null;
}

async function start() {
  const win = new BrowserWindow({
    width: 1200,
    height: 850,
    minWidth: 900,
    minHeight: 600,
    title: 'MailMatrix AI',
    show: false,
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true },
  });
  win.once('ready-to-show', () => win.show());

  // Route target="_blank" links: the app's own pages (e.g. summary previews)
  // open in a new in-app window; links in rendered email bodies point at real
  // sites and open in the user's default browser instead.
  win.webContents.setWindowOpenHandler(({ url }) => {
    let sameOrigin = false;
    try {
      sameOrigin = new URL(url).origin === new URL(win.webContents.getURL()).origin;
    } catch (_) { /* malformed URL — treat as external */ }
    if (sameOrigin) {
      return {
        action: 'allow',
        overrideBrowserWindowOptions: {
          width: 1000,
          height: 800,
          webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true },
        },
      };
    }
    if (/^https?:/i.test(url)) shell.openExternal(url);
    return { action: 'deny' };
  });

  const fail = (msg, detail) => win.loadFile(path.join(__dirname, 'error.html'), {
    query: { msg, detail: (detail || '').slice(-4000) },
  });

  if (!REPO_ROOT) {
    return fail('MAILMATRIX_REPO is not set',
      'Packaged builds need MAILMATRIX_REPO pointing at a MailMatrixAI checkout with a .venv.');
  }
  const python = pythonPath();
  if (!python) {
    return fail('Python venv not found',
      `Expected ${path.join(REPO_ROOT, '.venv', 'bin', 'python')}.\n` +
      'Create it from the repo root: python3 -m venv .venv && .venv/bin/pip install -e .');
  }

  let port;
  try {
    port = await getFreePort();
  } catch (e) {
    return fail('Could not allocate a local port', String(e));
  }
  const url = `http://127.0.0.1:${port}/`;

  let stderrTail = '';
  try {
    pyProc = spawn(python, [path.join(REPO_ROOT, 'app.py')], {
      cwd: REPO_ROOT,
      env: { ...process.env, MAILMATRIX_PORT: String(port), MAILMATRIX_HOST: '127.0.0.1' },
      stdio: ['ignore', 'inherit', 'pipe'],
    });
  } catch (e) {
    return fail('Failed to start the Python backend', String(e));
  }
  pyProc.stderr.on('data', (d) => {
    stderrTail = (stderrTail + d).slice(-4000);
    process.stderr.write(d);
  });
  pyProc.on('exit', (code) => {
    if (!quitting && code !== null && code !== 0) {
      fail(`The Python backend exited unexpectedly (code ${code})`, stderrTail);
    }
  });

  try {
    await waitForServer(url, pyProc);
    await win.loadURL(url);
  } catch (e) {
    fail('The MailMatrix backend did not start', `${e.message}\n\n${stderrTail}`);
  }
}

app.whenReady().then(start);
app.on('before-quit', () => { quitting = true; killPython(); });
app.on('window-all-closed', () => app.quit());
process.on('exit', killPython);
// A bare process 'exit' handler doesn't run when the process dies from a
// signal — translate SIGTERM/SIGINT into a normal quit so the Python child
// is still cleaned up.
for (const sig of ['SIGTERM', 'SIGINT']) {
  process.on(sig, () => { quitting = true; killPython(); app.quit(); });
}
