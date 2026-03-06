const { app, BrowserWindow, ipcMain, screen, shell } = require("electron");
const fs = require("fs");
const path = require("path");

let mainWindow = null;

function createWindow() {
  const display = screen.getPrimaryDisplay();
  const width = 420;
  const height = 620;
  const x = display.workArea.x + display.workArea.width - width - 24;
  const y = display.workArea.y + display.workArea.height - height - 36;

  mainWindow = new BrowserWindow({
    width,
    height,
    x,
    y,
    frame: false,
    transparent: true,
    resizable: false,
    maximizable: false,
    minimizable: true,
    hasShadow: false,
    alwaysOnTop: true,
    backgroundColor: "#00000000",
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });

  const devUrl = process.env.VITE_DEV_SERVER_URL;
  if (devUrl) {
    mainWindow.loadURL(devUrl);
  } else {
    mainWindow.loadFile(path.join(__dirname, "../dist/index.html"));
  }

  mainWindow.setAlwaysOnTop(true, "screen-saver");
  mainWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
}

app.whenReady().then(() => {
  // Enables Windows login auto-start for packaged desktop builds.
  app.setLoginItemSettings({
    openAtLogin: true,
    path: process.execPath,
  });
  createWindow();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

ipcMain.on("assistant:minimize", () => {
  mainWindow?.minimize();
});

ipcMain.on("assistant:hide", () => {
  mainWindow?.hide();
});

ipcMain.on("assistant:show", () => {
  if (!mainWindow) return;
  mainWindow.show();
  mainWindow.focus();
});

ipcMain.handle("assistant:version", () => app.getVersion());

ipcMain.handle("assistant:open-file", async (_event, filePath) => {
  if (typeof filePath !== "string" || !filePath.trim()) {
    return { ok: false, error: "invalid_path" };
  }

  const requested = filePath.trim();
  if (!path.isAbsolute(requested)) {
    return { ok: false, error: "path_must_be_absolute" };
  }
  const resolved = path.normalize(requested);
  if (!fs.existsSync(resolved)) {
    return { ok: false, error: "file_not_found" };
  }

  const openErr = await shell.openPath(resolved);
  if (openErr) {
    return { ok: false, error: openErr };
  }
  return { ok: true };
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});
