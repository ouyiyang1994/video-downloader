/*
 * Service worker: the only place that talks to Chrome and to the native host.
 *
 * The popup sends it `{type: 'collect' | 'send' | 'ping', platform}`; the reply
 * is a plain object the popup renders. Keeping every `chrome.*` call in here
 * means `logic.js` (which decides *what* is sent) stays unit-testable in Node.
 */

importScripts('logic.js');

const VD = globalThis.VDLogic;

/**
 * Read the platform's cookies out of Chrome.
 *
 * Chrome decrypts them itself - including the v20 / App-Bound Encrypted ones -
 * because the request comes from an extension the user installed, not from an
 * outside process trying to unpick the cookie database. The extension never
 * sees a key, never reads a profile file, and never touches another site.
 */
async function collect(platformId) {
  const platform = VD.PLATFORMS[platformId];
  if (!platform) {
    throw new Error('未知的平台：' + platformId);
  }
  const found = [];
  for (const domain of platform.domains) {
    const batch = await chrome.cookies.getAll({ domain });
    for (const cookie of batch) {
      found.push(cookie);
    }
  }
  return VD.dropExpired(VD.selectCookies(found, platformId));
}

/**
 * Hand one payload to the application.
 *
 * Native messaging is tried first: it needs no listening socket, Chrome
 * mediates it, and it is the only route that works while the application is
 * closed. Some locked-down Windows installs refuse to give a native host its
 * pipes, though - so the application also listens on loopback while its sign-in
 * window is open, and that is the fallback.
 */
async function deliver(payload) {
  let nativeError = null;
  try {
    return await chrome.runtime.sendNativeMessage(VD.HOST_NAME, payload);
  } catch (error) {
    nativeError = error && error.message ? error.message : String(error);
  }

  const bridged = await postToLocalBridge(payload);
  if (bridged !== null) {
    return bridged;
  }
  return {
    ok: false,
    error: VD.friendlyError(nativeError),
  };
}

/** POST to 127.0.0.1, or null when nothing is listening there. */
async function postToLocalBridge(payload) {
  try {
    const response = await fetch(VD.localBridgeUrl(), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      return { ok: false, error: '本机程序返回 HTTP ' + response.status };
    }
    return await response.json();
  } catch (error) {
    // No listener, or it went away between the two attempts.
    return null;
  }
}

async function send(platformId) {
  const cookies = await collect(platformId);
  if (cookies.length === 0) {
    return { ok: false, error: '没有找到该平台的 Cookie，请先在浏览器里完成登录。' };
  }
  if (!VD.hasAuthCookie(cookies, platformId)) {
    return {
      ok: false,
      error:
        '找到了该平台的 Cookie，但没有登录凭证（' +
        VD.PLATFORMS[platformId].authCookies.join(' / ') +
        '），请先完成登录。',
    };
  }

  const payload = {
    action: 'store',
    platform: platformId,
    extensionVersion: chrome.runtime.getManifest().version,
    cookies: cookies.map(VD.toWireCookie),
  };

  return deliver(payload);
}

async function ping() {
  return deliver({ action: 'ping' });
}

/** The active tab's platform, so the popup can pre-select it. */
async function activePlatform() {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    return tab ? VD.platformForUrl(tab.url) : null;
  } catch (error) {
    return null;
  }
}

async function dispatch(message) {
  const type = message && message.type;
  try {
    if (type === 'collect') {
      const cookies = await collect(message.platform);
      return { ok: true, count: cookies.length, cookieNames: VD.cookieNames(cookies) };
    }
    if (type === 'ping') {
      const response = await ping();
      return { ok: true, response };
    }
    if (type === 'activePlatform') {
      return { ok: true, platform: await activePlatform() };
    }
    if (type === 'send') {
      return await send(message.platform);
    }
    return { ok: false, error: '未知的请求：' + String(type) };
  } catch (error) {
    // Chrome's own message is safe to show; it never quotes a cookie value.
    return { ok: false, error: VD.friendlyError(error && error.message ? error.message : error) };
  }
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  dispatch(message).then(sendResponse);
  return true; // keep the channel open for the async reply
});

// Opening the popup on a login page is the common case, so make the extension
// icon visible there without needing a click first.
chrome.runtime.onInstalled.addListener(() => {
  chrome.action.setTitle({ title: 'Video Downloader 登录助手' });
});
