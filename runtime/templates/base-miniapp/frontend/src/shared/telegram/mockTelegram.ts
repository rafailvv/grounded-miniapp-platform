import type { TelegramWebApp } from '@/shared/telegram/webApp';

const mockBackButtonState = {
  handler: null as null | (() => void),
};

function createMockWebApp(): TelegramWebApp {
  return {
    platform: 'browser',
    colorScheme: window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light',
    initData: '',
    initDataUnsafe: {
      user: {
        id: 0,
        first_name: '',
        last_name: '',
        username: 'demo_user',
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
