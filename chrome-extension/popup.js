/*
 * Popup: picks the platform, asks the service worker to do the work, renders
 * the result. The cookie API is never called from here, and no cookie value
 * ever lands here either - only the names the host reports back.
 */

const VD = globalThis.VDLogic;

const statusEl = document.getElementById('status');
const sendButton = document.getElementById('send');
const testButton = document.getElementById('test');
const radios = Array.from(document.querySelectorAll('input[name="platform"]'));

function selectedPlatform() {
  const checked = radios.find((radio) => radio.checked);
  return checked ? checked.value : null;
}

function setStatus(text, kind) {
  statusEl.textContent = text;
  statusEl.className = 'status' + (kind ? ' ' + kind : '');
}

function setBusy(busy) {
  sendButton.disabled = busy;
  testButton.disabled = busy;
  radios.forEach((radio) => {
    radio.disabled = busy;
  });
}

function selectPlatform(platform) {
  for (const radio of radios) {
    radio.checked = radio.value === platform;
  }
}

async function ask(message) {
  return chrome.runtime.sendMessage(message);
}

async function onSend() {
  const platform = selectedPlatform();
  if (!platform) {
    setStatus('请先选择平台。', 'error');
    return;
  }
  setBusy(true);
  setStatus('正在读取并发送 ' + VD.platformLabel(platform) + ' 的 Cookie…');
  try {
    const result = await ask({ type: 'send', platform });
    const ok = Boolean(result && result.ok);
    setStatus(VD.describeResponse(result, platform), ok ? 'ok' : 'error');
  } catch (error) {
    setStatus(VD.friendlyError(error && error.message ? error.message : error), 'error');
  } finally {
    setBusy(false);
  }
}

async function onTest() {
  setBusy(true);
  setStatus('正在测试与本机程序的连接…');
  try {
    const result = await ask({ type: 'ping' });
    const response = result && result.response;
    if (result && result.ok && response && response.ok) {
      setStatus(
        '连接正常。本机程序版本 ' + (response.hostVersion || '未知') + '，会话目录：' + (response.sessionDir || '未知') + '。',
        'ok'
      );
    } else {
      setStatus(VD.friendlyError((result && result.error) || (response && response.error) || ''), 'error');
    }
  } catch (error) {
    setStatus(VD.friendlyError(error && error.message ? error.message : error), 'error');
  } finally {
    setBusy(false);
  }
}

async function init() {
  sendButton.addEventListener('click', onSend);
  testButton.addEventListener('click', onTest);

  // Pre-select the platform of the tab the popup was opened on, so the common
  // case is one click.
  const detected = await ask({ type: 'activePlatform' }).catch(() => null);
  selectPlatform((detected && detected.platform) || 'bilibili');

  const probe = await ask({ type: 'ping' }).catch(() => null);
  const response = probe && probe.response;
  if (probe && probe.ok && response && response.ok) {
    setStatus('本机程序已就绪。登录完成后点「发送到 Video Downloader」。');
  } else {
    setStatus(
      VD.friendlyError((probe && probe.error) || (response && response.error) || ''),
      'error'
    );
  }
}

init();
