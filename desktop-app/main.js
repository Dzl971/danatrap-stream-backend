const { app, BrowserWindow } = require('electron');
const path = require('path');

function creerFenetre() {
  const fenetre = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 1000,
    minHeight: 700,
    title: 'DanaTrap Stream',
    backgroundColor: '#0D0B14',
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true
    }
  });

  fenetre.loadFile('index.html');

  // Décommente la ligne suivante si tu veux voir la console de debug
  // fenetre.webContents.openDevTools();
}

app.whenReady().then(() => {
  creerFenetre();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      creerFenetre();
    }
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});