const { app, BrowserWindow, ipcMain, protocol, net, nativeImage, shell } = require('electron');
const path = require('path');
const fs = require('fs');
const { spawn, execSync } = require('child_process');

let mainWindow;
let pythonProcess = null;

function getDataDir() {
  return __dirname;
}

function getConfigPath() {
  return path.join(getDataDir(), 'config.json');
}

function loadConfig() {
  try {
    const cfgPath = getConfigPath();
    if (fs.existsSync(cfgPath)) {
      return JSON.parse(fs.readFileSync(cfgPath, 'utf-8'));
    }
  } catch {}
  return { discordWebhookUrl: '', discordEnabled: false, keywords: [] };
}

function saveConfig(config) {
  fs.writeFileSync(getConfigPath(), JSON.stringify(config, null, 2), 'utf-8');
}

// Prefer the project venv — it holds onnxruntime-directml and rapidocr.
function getPythonCmd() {
  const venv = process.platform === 'win32'
    ? path.join(__dirname, '.venv', 'Scripts', 'python.exe')
    : path.join(__dirname, '.venv', 'bin', 'python');
  if (fs.existsSync(venv)) return venv;

  for (const cmd of ['python3', 'python']) {
    try {
      execSync(`${cmd} --version`, { stdio: 'ignore' });
      return cmd;
    } catch {}
  }
  return null;
}

protocol.registerSchemesAsPrivileged([
  {
    scheme: 'page-thumb',
    privileges: { bypassCSP: true, stream: true, supportFetchAPI: true, standard: true, secure: true },
  },
]);

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1140,
    height: 800,
    minWidth: 900,
    minHeight: 600,
    frame: false,
    backgroundColor: '#ffffff',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'));

  // Renderer errors are invisible otherwise — surface them in the terminal.
  mainWindow.webContents.on('console-message', (_event, level, message, line, sourceId) => {
    const where = sourceId ? `${sourceId.split('/').pop()}:${line}` : '';
    console.log(`[renderer:${level}] ${message} ${where}`);
  });

  mainWindow.webContents.on('before-input-event', (_event, input) => {
    if (input.control && input.shift && input.key.toLowerCase() === 'i') {
      mainWindow.webContents.toggleDevTools();
    }
  });
}

// Leaflet pages are ~1.9 MB each, far too heavy for a grid of thumbnails.
// Fetch once, downscale, and keep the JPEG in memory for the session.
const thumbCache = new Map();

async function handleThumbRequest(request) {
  const url = new URL(request.url);
  const remote = url.searchParams.get('u');
  const width = parseInt(url.searchParams.get('w'), 10) || 300;
  if (!remote || !/^https:\/\//.test(remote)) {
    return new Response('', { status: 400 });
  }
  const key = `${remote}|${width}`;

  const cached = thumbCache.get(key);
  if (cached) {
    return new Response(cached, { headers: { 'Content-Type': 'image/jpeg' } });
  }

  try {
    const response = await net.fetch(remote);
    const buffer = Buffer.from(await response.arrayBuffer());
    let img = nativeImage.createFromBuffer(buffer);
    const size = img.getSize();
    if (size.width > width) {
      img = img.resize({
        width,
        height: Math.round((size.height * width) / size.width),
        quality: 'good',
      });
    }
    const jpeg = img.toJPEG(72);
    thumbCache.set(key, jpeg);
    return new Response(jpeg, { headers: { 'Content-Type': 'image/jpeg' } });
  } catch (e) {
    return new Response('', { status: 502 });
  }
}

// === Python indexer process ===

function sendEvent(event) {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('search-event', event);
  }
}

function startPython() {
  if (pythonProcess) return;

  const pythonCmd = getPythonCmd();
  if (!pythonCmd) {
    sendEvent({ type: 'error', message: 'Nie znaleziono Pythona. Utwórz .venv i zainstaluj requirements.txt.' });
    return;
  }

  const config = loadConfig();
  const env = { ...process.env, PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8' };
  env.BIEDRONA_DATA_DIR = getDataDir();
  if (config.discordWebhookUrl) {
    env.DISCORD_WEBHOOK_URL = config.discordWebhookUrl;
  }

  pythonProcess = spawn(pythonCmd, ['-u', path.join(__dirname, 'biedrona.py'), '--serve'], {
    cwd: getDataDir(),
    env,
  });

  let buffer = '';
  pythonProcess.stdout.on('data', (data) => {
    buffer += data.toString('utf-8');
    const lines = buffer.split('\n');
    buffer = lines.pop();
    for (const line of lines) {
      if (!line.startsWith('JSON:')) continue;
      try {
        sendEvent(JSON.parse(line.slice(5)));
      } catch {}
    }
  });

  let stderrTail = '';
  pythonProcess.stderr.on('data', (data) => {
    const text = data.toString();
    console.error('[Python]', text);
    stderrTail = (stderrTail + text).slice(-2000);
  });

  pythonProcess.on('close', (code) => {
    pythonProcess = null;
    if (code !== 0 && code !== null) {
      sendEvent({
        type: 'error',
        message: `Indekser zakończył się z kodem ${code}.\n${stderrTail.trim().slice(-600)}`,
      });
    }
    sendEvent({ type: 'process-ended', code });
  });
}

function sendCommand(command) {
  if (!pythonProcess) {
    sendEvent({ type: 'error', message: 'Indekser nie działa.' });
    return false;
  }
  pythonProcess.stdin.write(JSON.stringify(command) + '\n');
  return true;
}

app.whenReady().then(() => {
  protocol.handle('page-thumb', handleThumbRequest);
  createWindow();
});

app.on('window-all-closed', () => {
  if (pythonProcess) {
    sendCommand({ cmd: 'quit' });
    pythonProcess.kill();
  }
  app.quit();
});

// === IPC ===

ipcMain.handle('start-engine', async () => {
  startPython();
  return true;
});

ipcMain.handle('search', async (_event, keyword) => sendCommand({ cmd: 'search', keyword }));

ipcMain.handle('search-many', async (_event, keywords) => sendCommand({ cmd: 'search-many', keywords }));

ipcMain.handle('save-hit', async (_event, hit) => sendCommand({ cmd: 'save', ...hit }));

ipcMain.handle('send-discord', async (_event, { keyword, hits }) => {
  const config = loadConfig();
  if (!config.discordWebhookUrl) {
    sendEvent({ type: 'error', message: 'Brak webhooka Discorda w ustawieniach.' });
    return false;
  }
  return sendCommand({ cmd: 'discord', keyword, hits, webhook: config.discordWebhookUrl });
});

ipcMain.handle('index-status', async () => sendCommand({ cmd: 'status' }));

ipcMain.handle('reindex', async () => sendCommand({ cmd: 'reindex' }));

ipcMain.handle('clear-cache', async () => {
  thumbCache.clear();
  return sendCommand({ cmd: 'reset' });
});

ipcMain.handle('open-folder', async () => {
  const dir = path.join(getDataDir(), 'gazetki');
  fs.mkdirSync(dir, { recursive: true });
  shell.openPath(dir);
});

ipcMain.handle('load-config', async () => loadConfig());

ipcMain.handle('save-config', async (_event, config) => {
  saveConfig(config);
  return true;
});

ipcMain.handle('minimize-window', () => mainWindow.minimize());

ipcMain.handle('maximize-window', () => {
  if (mainWindow.isMaximized()) {
    mainWindow.unmaximize();
  } else {
    mainWindow.maximize();
  }
});

ipcMain.handle('close-window', () => mainWindow.close());
