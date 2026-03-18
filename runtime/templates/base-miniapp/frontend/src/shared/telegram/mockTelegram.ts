import type { TelegramWebApp } from '@/shared/telegram/webApp';

const mockBackButtonState = {
  handler: null as null | (() => void),
};
const mockDeviceStorage = new Map<string, string>();

function createMockWebApp(): TelegramWebApp {
  return {
    platform: 'browser',
    colorScheme: window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light',
    initData: '',
    initDataUnsafe: {
      user: {
        id: 0,
        first_name: 'John',
        last_name: 'Doe',
        username: 'john_doe',
      },
      start_param: '',
    },
    ready: () => {
      // no-op for local browser
    },
    close: () => {
      // no-op for local browser
    },
    expand: () => {
      // no-op for local browser
    },
    requestFullscreen: () => {
      // no-op for local browser
    },
    disableVerticalSwipes: () => {
      // no-op for local browser
    },
    BackButton: {
      show: () => {
        // no-op for local browser
      },
      hide: () => {
        // no-op for local browser
      },
      onClick: (handler: () => void) => {
        mockBackButtonState.handler = handler;
      },
      offClick: (handler: () => void) => {
        if (mockBackButtonState.handler === handler) {
          mockBackButtonState.handler = null;
        }
      },
    },
    HapticFeedback: {
      impactOccurred: () => {
        // no-op for local browser
      },
      notificationOccurred: () => {
        // no-op for local browser
      },
      selectionChanged: () => {
        // no-op for local browser
      },
    },
    DeviceStorage: {
      setItem: (key, value, callback) => {
        mockDeviceStorage.set(key, value);
        callback?.(null, true);
      },
      getItem: (key, callback) => {
        callback(null, mockDeviceStorage.get(key) ?? '');
      },
      removeItem: (key, callback) => {
        const hadKey = mockDeviceStorage.delete(key);
        callback?.(null, hadKey);
      },
      clear: (callback) => {
        mockDeviceStorage.clear();
        callback?.(null, true);
      },
    },
  };
}

export function initTelegramMock(): void {
  if (!import.meta.env.DEV) {
    return;
  }

  const shouldUseMock = new URLSearchParams(window.location.search).get('mockTelegram') === '1';
  if (!shouldUseMock) {
    return;
  }

  if (!window.Telegram?.WebApp) {
    window.Telegram = {
      WebApp: createMockWebApp(),
    };
  }
}
