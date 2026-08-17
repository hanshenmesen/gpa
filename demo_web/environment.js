(() => {
  if (globalThis.GPAClient) return;

  function environmentSnapshot() {
    const userAgent = navigator.userAgent || '';
    const browserFamily = /Edg\//.test(userAgent)
      ? 'Edge'
      : /Chrome\//.test(userAgent)
        ? 'Chrome'
        : /Firefox\//.test(userAgent)
          ? 'Firefox'
          : /Safari\//.test(userAgent)
            ? 'Safari'
            : 'Unknown';
    return {
      language: navigator.language || '',
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || '',
      screen: {
        width: window.screen?.width || 0,
        height: window.screen?.height || 0,
        pixel_ratio: window.devicePixelRatio || 1,
      },
      browser: {
        family: browserFamily,
        user_agent: userAgent,
        viewport_width: window.innerWidth || 0,
        viewport_height: window.innerHeight || 0,
      },
    };
  }

  function environmentQuery(snapshot = environmentSnapshot()) {
    return new URLSearchParams({
      language: snapshot.language,
      timezone: snapshot.timezone,
      screen_width: String(snapshot.screen.width || 0),
      screen_height: String(snapshot.screen.height || 0),
      pixel_ratio: String(snapshot.screen.pixel_ratio || 1),
      viewport_width: String(snapshot.browser.viewport_width || 0),
      viewport_height: String(snapshot.browser.viewport_height || 0),
      browser_family: snapshot.browser.family || '',
    });
  }

  function boundedDuration(value, fallback, minimum, maximum) {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return fallback;
    return Math.min(maximum, Math.max(minimum, parsed));
  }

  async function requestJson(path, options = {}) {
    const {
      timeoutMs = 60000,
      signal: externalSignal,
      baseUrl = '',
      ...fetchOptions
    } = options || {};
    const controller = new AbortController();
    let timedOut = false;
    const timeoutDuration = boundedDuration(timeoutMs, 60000, 250, 300000);
    const timeout = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, timeoutDuration);
    const abortFromOutside = () => controller.abort(externalSignal?.reason);
    if (externalSignal?.aborted) abortFromOutside();
    else externalSignal?.addEventListener('abort', abortFromOutside, { once: true });
    try {
      const response = await fetch(`${baseUrl}${path}`, {
        ...fetchOptions,
        signal: controller.signal,
      });
      const responseText = await response.text();
      let data = {};
      if (responseText) {
        try {
          data = JSON.parse(responseText);
        } catch {
          if (response.ok) throw new Error('服务返回了无法识别的数据。');
        }
      }
      if (!response.ok || data.ok === false) {
        const fallback = responseText && !responseText.trim().startsWith('<')
          ? responseText.trim().slice(0, 240)
          : `请求失败（${response.status}）`;
        throw new Error(data.error || fallback);
      }
      return data;
    } catch (error) {
      if (timedOut) throw new Error('请求等待时间过长，请重试。');
      throw error;
    } finally {
      window.clearTimeout(timeout);
      externalSignal?.removeEventListener('abort', abortFromOutside);
    }
  }

  function postJson(path, payload = {}, options = {}) {
    return requestJson(path, {
      ...options,
      method: 'POST',
      headers: { ...options.headers, 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  }

  function sendJsonBeacon(path, payload = {}, options = {}) {
    const body = JSON.stringify(payload || {});
    const url = `${options.baseUrl || ''}${path}`;
    if (navigator.sendBeacon) {
      const blob = new Blob([body], { type: 'text/plain;charset=UTF-8' });
      if (navigator.sendBeacon(url, blob)) return true;
    }
    fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'text/plain;charset=UTF-8' },
      body,
      keepalive: true,
    }).catch(() => {});
    return false;
  }

  function createVisibilityPoller(task, options = {}) {
    const activeMs = boundedDuration(options.activeMs, 2000, 250, 300000);
    const hiddenMs = Math.max(
      activeMs,
      boundedDuration(options.hiddenMs, 10000, activeMs, 900000),
    );
    let timer = 0;
    let stopped = false;
    let running = false;
    let rerunRequested = false;

    const schedule = () => {
      if (stopped) return;
      window.clearTimeout(timer);
      timer = window.setTimeout(run, document.hidden ? hiddenMs : activeMs);
    };
    const run = async () => {
      if (stopped) return;
      if (running) {
        rerunRequested = true;
        return;
      }
      running = true;
      try {
        await task();
      } catch (error) {
        if (typeof options.onError === 'function') options.onError(error);
      } finally {
        running = false;
        if (stopped) return;
        if (rerunRequested && !document.hidden) {
          rerunRequested = false;
          void run();
        } else {
          rerunRequested = false;
          schedule();
        }
      }
    };
    const restart = () => {
      if (stopped) return;
      window.clearTimeout(timer);
      if (document.hidden) schedule();
      else void run();
    };
    const stop = () => {
      stopped = true;
      rerunRequested = false;
      window.clearTimeout(timer);
      document.removeEventListener('visibilitychange', restart);
      window.removeEventListener('pagehide', stop);
    };

    document.addEventListener('visibilitychange', restart);
    window.addEventListener('pagehide', stop, { once: true });
    if (options.runImmediately === false) schedule();
    else void run();
    return stop;
  }

  globalThis.GPAClient = Object.freeze({
    environmentSnapshot,
    environmentQuery,
    requestJson,
    postJson,
    sendJsonBeacon,
    createVisibilityPoller,
  });
  globalThis.clientEnvironmentSnapshot = environmentSnapshot;
  globalThis.clientEnvironmentQuery = environmentQuery;
})();
