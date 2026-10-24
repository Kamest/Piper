'use strict';
/** Signing is opt-in; electron-builder performs notarization when explicitly configured. */
module.exports = async function afterSign(context) {
  if (context.electronPlatformName !== 'darwin' || !process.env.APPLE_ID || !process.env.APPLE_APP_SPECIFIC_PASSWORD || !process.env.APPLE_TEAM_ID) return;
  const { notarize } = require('@electron/notarize');
  await notarize({ appPath: require('node:path').join(context.appOutDir, 'Piper.app'),
    appleId: process.env.APPLE_ID, appleIdPassword: process.env.APPLE_APP_SPECIFIC_PASSWORD, teamId: process.env.APPLE_TEAM_ID });
};
