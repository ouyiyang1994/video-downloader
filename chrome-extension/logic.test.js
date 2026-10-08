/*
 * Tests for the extension's decision logic, run with `node --test`.
 *
 * `logic.js` holds every decision about *what* is sent, so testing it directly
 * (rather than only through Chrome) is what makes the security-relevant rules
 * - which domains, which cookie names, never a value in a message - checkable.
 *
 *   node --test chrome-extension/logic.test.js
 */

'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const VD = require('./logic.js');

// --- which platform a page belongs to ----------------------------------------

test('recognises the two supported platforms on any subdomain', () => {
  assert.equal(VD.platformForUrl('https://www.bilibili.com/video/BV1'), 'bilibili');
  assert.equal(VD.platformForUrl('https://passport.bilibili.com/login'), 'bilibili');
  assert.equal(VD.platformForUrl('https://www.instagram.com/accounts/login/'), 'instagram');
  assert.equal(VD.platformForUrl('https://i.instagram.com/api/v1/'), 'instagram');
});

test('does not mistake a lookalike host for the platform', () => {
  assert.equal(VD.platformForUrl('https://bilibili.com.evil.example/'), null);
  assert.equal(VD.platformForUrl('https://notbilibili.com/'), null);
  assert.equal(VD.platformForUrl('https://instagram.com.evil.example/'), null);
  assert.equal(VD.platformForUrl('https://www.youtube.com/'), null);
});

test('survives a url it cannot parse', () => {
  assert.equal(VD.platformForUrl('not a url'), null);
  assert.equal(VD.platformForUrl(''), null);
  assert.equal(VD.platformForUrl(undefined), null);
  assert.equal(VD.platformForUrl(null), null);
});

// --- domain matching ---------------------------------------------------------

test('domain matching accepts the domain and its subdomains only', () => {
  assert.equal(VD.domainMatches('bilibili.com', 'bilibili.com'), true);
  assert.equal(VD.domainMatches('.bilibili.com', 'bilibili.com'), true);
  assert.equal(VD.domainMatches('www.bilibili.com', 'bilibili.com'), true);
  assert.equal(VD.domainMatches('bilibili.com.evil.example', 'bilibili.com'), false);
  assert.equal(VD.domainMatches('notbilibili.com', 'bilibili.com'), false);
  assert.equal(VD.domainMatches('', 'bilibili.com'), false);
  assert.equal(VD.domainMatches('bilibili.com', ''), false);
});

// --- cookie selection --------------------------------------------------------

const bilibiliCookies = [
  { name: 'SESSDATA', value: 'V1', domain: '.bilibili.com', path: '/' },
  { name: 'bili_jct', value: 'V2', domain: '.bilibili.com', path: '/' },
  { name: 'SESSDATA', value: 'V1', domain: '.bilibili.com', path: '/' },
  { name: 'tracker', value: 'V3', domain: '.example.com', path: '/' },
  { name: 'sessionid', value: 'V4', domain: '.instagram.com', path: '/' },
];

test('only the platform domain survives, and duplicates collapse', () => {
  const selected = VD.selectCookies(bilibiliCookies, 'bilibili');
  assert.deepEqual(
    selected.map((cookie) => cookie.name),
    ['SESSDATA', 'bili_jct']
  );
});

test('the other platform is never included', () => {
  const selected = VD.selectCookies(bilibiliCookies, 'instagram');
  assert.deepEqual(
    selected.map((cookie) => cookie.name),
    ['sessionid']
  );
});

test('a different path is kept as a separate cookie', () => {
  const selected = VD.selectCookies(
    [
      { name: 'SESSDATA', value: 'A', domain: '.bilibili.com', path: '/' },
      { name: 'SESSDATA', value: 'B', domain: '.bilibili.com', path: '/api' },
    ],
    'bilibili'
  );
  assert.equal(selected.length, 2);
});

test('malformed input is ignored rather than thrown on', () => {
  assert.deepEqual(VD.selectCookies(null, 'bilibili'), []);
  assert.deepEqual(VD.selectCookies([null, 'x', {}], 'bilibili'), []);
  assert.deepEqual(VD.selectCookies(bilibiliCookies, 'tiktok'), []);
});

test('the authentication cookie is what proves a login', () => {
  assert.equal(VD.hasAuthCookie(bilibiliCookies, 'bilibili'), true);
  assert.equal(VD.hasAuthCookie([{ name: 'csrftoken' }], 'bilibili'), false);
  assert.equal(VD.hasAuthCookie([], 'bilibili'), false);
  assert.equal(VD.hasAuthCookie(null, 'bilibili'), false);
});

// --- expiry ------------------------------------------------------------------

test('expired cookies are dropped, session cookies are kept', () => {
  const now = 1_000_000;
  const kept = VD.dropExpired(
    [
      { name: 'a', expirationDate: now + 10 },
      { name: 'b', expirationDate: now - 10 },
      { name: 'c' },
    ],
    now
  );
  assert.deepEqual(
    kept.map((cookie) => cookie.name),
    ['a', 'c']
  );
});

// --- what crosses the bridge -------------------------------------------------

test('only the fields the host uses are forwarded', () => {
  const wire = VD.toWireCookie({
    name: 'SESSDATA',
    value: 'V',
    domain: '.bilibili.com',
    path: '/',
    secure: true,
    hostOnly: false,
    session: false,
    storeId: '0',
    sameSite: 'no_restriction',
    id: 42,
  });
  assert.deepEqual(Object.keys(wire).sort(), [
    'domain',
    'expirationDate',
    'name',
    'path',
    'secure',
    'value',
  ]);
});

test('a missing path defaults to the root', () => {
  assert.equal(VD.toWireCookie({ name: 'a', value: 'b' }).path, '/');
});

test('cookie names are the only safe thing to show', () => {
  assert.deepEqual(VD.cookieNames(bilibiliCookies), ['SESSDATA', 'bili_jct', 'sessionid', 'tracker']);
});

// --- error mapping -----------------------------------------------------------

test('the missing-host error tells the user exactly what to do', () => {
  const message = VD.friendlyError('Specified native messaging host not found.');
  assert.match(message, /登录助手/);
  assert.match(message, /安装/);
});

test('a forbidden host points at the extension id mismatch', () => {
  const message = VD.friendlyError('Access to the specified native messaging host is forbidden.');
  assert.match(message, /重新/);
});

test('an unknown error is passed through unchanged', () => {
  assert.equal(VD.friendlyError('something odd'), 'something odd');
  assert.equal(VD.friendlyError(''), '未知错误');
});

// --- describing a result -----------------------------------------------------

test('a successful send says how many cookies and what to do next', () => {
  const text = VD.describeResponse(
    { ok: true, saved: 3, cookieNames: ['SESSDATA', 'bili_jct'] },
    'bilibili'
  );
  assert.match(text, /3 个/);
  assert.match(text, /哔哩哔哩/);
  assert.match(text, /检测会话/);
});

test('an empty result says the user is not signed in', () => {
  const text = VD.describeResponse({ ok: true, saved: 0 }, 'instagram');
  assert.match(text, /登录/);
});

test('a failed result is rendered through the error mapper', () => {
  const text = VD.describeResponse(
    { ok: false, error: 'Specified native messaging host not found.' },
    'bilibili'
  );
  assert.match(text, /登录助手/);
});

test('a non-object response is reported honestly', () => {
  assert.match(VD.describeResponse(null, 'bilibili'), /没有返回/);
  assert.match(VD.describeResponse('nope', 'bilibili'), /没有返回/);
});

// --- security invariants -----------------------------------------------------

test('the platform table covers exactly the two supported sites', () => {
  assert.deepEqual(VD.PLATFORM_IDS.sort(), ['bilibili', 'instagram']);
});

test('no platform entry widens beyond its own domain', () => {
  for (const id of VD.PLATFORM_IDS) {
    for (const domain of VD.PLATFORMS[id].domains) {
      assert.ok(
        ['bilibili.com', 'instagram.com'].includes(domain),
        `unexpected domain for ${id}: ${domain}`
      );
    }
  }
});

test('no response helper can echo a cookie value', () => {
  // describeResponse only ever reads saved / cookieNames / error, so a value in
  // the payload cannot reach the popup.
  const text = VD.describeResponse(
    { ok: true, saved: 1, cookieNames: ['SESSDATA'], value: 'SECRET' },
    'bilibili'
  );
  assert.ok(!text.includes('SECRET'));
});

test('the host name matches what the application registers', () => {
  assert.equal(VD.HOST_NAME, 'com.videodownloader.cookies');
});

test('the loopback fallback is 127.0.0.1 and nothing else', () => {
  const url = VD.localBridgeUrl();
  assert.equal(url, 'http://127.0.0.1:8765/session');
  assert.ok(!url.includes('localhost'));
  assert.ok(!url.includes('0.0.0.0'));
});

test('a native messaging communication failure points at the fallback', () => {
  const message = VD.friendlyError('Error when communicating with the native messaging host.');
  assert.match(message, /回环|登录窗口/);
});

test('an unreachable local program is explained', () => {
  const message = VD.friendlyError('TypeError: Failed to fetch');
  assert.match(message, /登录窗口/);
});
