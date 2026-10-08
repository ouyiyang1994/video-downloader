/*
 * Pure logic for the Video Downloader sign-in helper.
 *
 * Deliberately free of any `chrome.*` call so it can be exercised directly by
 * the Node test suite (`chrome-extension/logic.test.js`, run with
 * `node --test`) as well as by the service worker and the popup. Everything
 * that decides *what* gets sent lives here; `background.js` only talks to the
 * browser.
 *
 * Two rules this file must never break:
 *   - a cookie value is never put into a message, a log line, or an error;
 *   - only the platform's own domains are ever selected.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  }
  root.VDLogic = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  //: The native host name registered by the application in
  //: HKCU\Software\Google\Chrome\NativeMessagingHosts. Must match
  //: core/chrome_bridge.py::HOST_NAME.
  const HOST_NAME = 'com.videodownloader.cookies';

  //: The only two platforms this extension knows about. `domains` is what is
  //: asked of chrome.cookies.getAll; the manifest's host_permissions is what
  //: Chrome actually enforces, so the extension cannot see anything else even
  //: if this list were wrong.
  const PLATFORMS = {
    bilibili: {
      label: '哔哩哔哩',
      domains: ['bilibili.com'],
      authCookies: ['SESSDATA'],
      loginUrl: 'https://passport.bilibili.com/login',
      hostPattern: /(^|\.)bilibili\.com$/i,
    },
    instagram: {
      label: 'Instagram',
      domains: ['instagram.com'],
      authCookies: ['sessionid'],
      loginUrl: 'https://www.instagram.com/accounts/login/',
      hostPattern: /(^|\.)instagram\.com$/i,
    },
  };

  //: Fallback transport. Native messaging needs Chrome to hand a child process
  //: a pair of inheritable pipes, which some locked-down Windows installs
  //: refuse - so the application also listens on loopback while its sign-in
  //: window is open. Must match core/local_bridge.py::DEFAULT_PORT.
  const LOCAL_BRIDGE_PORT = 8765;
  const LOCAL_BRIDGE_PATH = '/session';

  const PLATFORM_IDS = Object.keys(PLATFORMS);

  function platformLabel(id) {
    const platform = PLATFORMS[id];
    return platform ? platform.label : id;
  }

  /** Which platform a tab URL belongs to, or null. */
  function platformForUrl(url) {
    if (typeof url !== 'string' || url === '') {
      return null;
    }
    for (const id of PLATFORM_IDS) {
      if (PLATFORMS[id].hostPattern.test(hostOf(url))) {
        return id;
      }
    }
    return null;
  }

  function hostOf(url) {
    try {
      return new URL(url).hostname;
    } catch (error) {
      return '';
    }
  }

  /** True when `domain` is `suffix` or a subdomain of it. */
  function domainMatches(domain, suffix) {
    const wanted = String(suffix || '').replace(/^\./, '').toLowerCase();
    const actual = String(domain || '').replace(/^\./, '').toLowerCase();
    if (!wanted || !actual) {
      return false;
    }
    return actual === wanted || actual.endsWith('.' + wanted);
  }

  /**
   * Keep only the cookies that belong to the platform, de-duplicated by
   * (name, domain, path). Chrome already restricts `getAll` by host permission;
   * this is the second, explicit gate so the intent is visible in the code.
   */
  function selectCookies(cookies, platformId) {
    const platform = PLATFORMS[platformId];
    if (!platform || !Array.isArray(cookies)) {
      return [];
    }
    const seen = new Set();
    const selected = [];
    for (const cookie of cookies) {
      if (!cookie || typeof cookie.name !== 'string') {
        continue;
      }
      if (!platform.domains.some((suffix) => domainMatches(cookie.domain, suffix))) {
        continue;
      }
      const key = cookie.name + '\u0000' + cookie.domain + '\u0000' + (cookie.path || '/');
      if (seen.has(key)) {
        continue;
      }
      seen.add(key);
      selected.push(cookie);
    }
    return selected;
  }

  /** True when the platform's authentication cookie is among `cookies`. */
  function hasAuthCookie(cookies, platformId) {
    const platform = PLATFORMS[platformId];
    if (!platform || !Array.isArray(cookies)) {
      return false;
    }
    const names = new Set(cookies.map((cookie) => cookie && cookie.name));
    return platform.authCookies.some((name) => names.has(name));
  }

  /**
   * Cookies whose expiry has already passed are dropped: sending them would
   * only produce a session the platform rejects.
   */
  function dropExpired(cookies, nowSeconds) {
    const now = typeof nowSeconds === 'number' ? nowSeconds : Date.now() / 1000;
    return cookies.filter((cookie) => {
      if (!cookie || typeof cookie.expirationDate !== 'number') {
        return true; // a session cookie has no expiry
      }
      return cookie.expirationDate > now;
    });
  }

  /**
   * Turn a raw `chrome.cookies.Cookie` into the small, explicit payload the
   * native host expects. Only the fields the host actually uses are copied, so
   * nothing unexpected (and nothing value-free) is forwarded.
   */
  function toWireCookie(cookie) {
    return {
      name: cookie.name,
      value: cookie.value,
      domain: cookie.domain || '',
      path: cookie.path || '/',
      secure: Boolean(cookie.secure),
      expirationDate: typeof cookie.expirationDate === 'number' ? cookie.expirationDate : null,
    };
  }

  /** Names only - the one thing about a cookie that is safe to display. */
  function cookieNames(cookies) {
    return Array.from(new Set(cookies.map((cookie) => cookie.name))).sort();
  }

  /** Where the application's loopback fallback listens. */
  function localBridgeUrl() {
    return 'http://127.0.0.1:' + LOCAL_BRIDGE_PORT + LOCAL_BRIDGE_PATH;
  }

  /**
   * Map the error strings Chrome produces into something a user can act on.
   * Falls back to the raw message, which never contains a cookie value.
   */
  function friendlyError(raw) {
    const text = String(raw || '');
    const lowered = text.toLowerCase();
    if (lowered.includes('native messaging host not found') || lowered.includes('not registered')) {
      return (
        '没有找到本机的 Video Downloader 登录宿主程序。' +
        '请先在 Video Downloader 里点「Chrome 登录助手 → 安装 / 更新登录助手」，然后重启 Chrome。'
      );
    }
    if (lowered.includes('forbidden') || lowered.includes('access to the specified native messaging host is forbidden')) {
      return (
        'Chrome 拒绝了这个扩展访问本机程序。' +
        '通常是扩展 ID 与宿主程序登记的 ID 不一致：请在 Video Downloader 里重新点一次「安装 / 更新登录助手」。'
      );
    }
    if (lowered.includes('host has exited') || lowered.includes('native host has exited')) {
      return '本机登录宿主程序启动后立刻退出了。请查看 Video Downloader 的 logs/native_host.log。';
    }
    if (lowered.includes('communicating with the native messaging host')) {
      return (
        'Chrome 无法与登录宿主程序通信（本机可能禁止了 native messaging 子进程）。' +
        '请确认 Video Downloader 的登录窗口正开着，然后重试——程序会自动改用本机回环通道。'
      );
    }
    if (lowered.includes('message length') || lowered.includes('exceeded')) {
      return '要发送的数据过大，已被 Chrome 拒绝。请只发送当前平台需要的 Cookie。';
    }
    if (lowered.includes('receiving end does not exist')) {
      return '扩展的后台进程没有响应，请关闭并重新打开扩展窗口后重试。';
    }
    if (lowered.includes('failed to fetch') || lowered.includes('networkerror')) {
      return (
        '无法连接本机程序。请先打开 Video Downloader 的登录窗口（本机回环通道只在窗口打开时监听）。'
      );
    }
    return text || '未知错误';
  }

  /** One line describing a host response, for the popup's status area. */
  function describeResponse(response, platformId) {
    const label = platformLabel(platformId);
    if (!response || typeof response !== 'object') {
      return '本机程序没有返回有效结果。';
    }
    if (!response.ok) {
      return friendlyError(response.error || '');
    }
    const count = typeof response.saved === 'number' ? response.saved : 0;
    if (count === 0) {
      return '没有找到 ' + label + ' 的 Cookie，请先在浏览器里完成登录。';
    }
    const names = Array.isArray(response.cookieNames) ? response.cookieNames.join(', ') : '';
    return (
      '已把 ' + count + ' 个 ' + label + ' Cookie 交给 Video Downloader' +
      (names ? '（' + names + '）' : '') +
      '。请回到 Video Downloader，点「我已登录，检测会话」。'
    );
  }

  return {
    HOST_NAME: HOST_NAME,
    LOCAL_BRIDGE_PORT: LOCAL_BRIDGE_PORT,
    PLATFORMS: PLATFORMS,
    PLATFORM_IDS: PLATFORM_IDS,
    platformLabel: platformLabel,
    platformForUrl: platformForUrl,
    localBridgeUrl: localBridgeUrl,
    domainMatches: domainMatches,
    selectCookies: selectCookies,
    hasAuthCookie: hasAuthCookie,
    dropExpired: dropExpired,
    toWireCookie: toWireCookie,
    cookieNames: cookieNames,
    friendlyError: friendlyError,
    describeResponse: describeResponse,
  };
});
