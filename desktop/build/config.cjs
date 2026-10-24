'use strict';
const config = require('../package.json').build;
const signedMac = !!process.env.CSC_LINK || !!process.env.CSC_NAME;
module.exports = {
  ...config,
  extraMetadata: { desktopSigned: signedMac },
  ...(process.env.ELECTRON_CACHE ? { electronDownload: { cache: process.env.ELECTRON_CACHE } } : {}),
  mac: { ...config.mac, ...(signedMac ? {} : { identity: null }) },
  win: { ...config.win, signAndEditExecutable: !!(process.env.WIN_CSC_LINK || process.env.CSC_LINK) }
};
