const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("desktopAssistant", {
  minimize: () => ipcRenderer.send("assistant:minimize"),
  hide: () => ipcRenderer.send("assistant:hide"),
  show: () => ipcRenderer.send("assistant:show"),
  getVersion: () => ipcRenderer.invoke("assistant:version"),
  openFile: (filePath) => ipcRenderer.invoke("assistant:open-file", filePath),
});
