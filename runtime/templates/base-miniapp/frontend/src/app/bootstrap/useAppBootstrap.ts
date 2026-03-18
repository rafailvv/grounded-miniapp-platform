import { useEffect, useState } from 'react';
import { authenticateTelegram } from '@/shared/auth/authApi';
import { setStoredTokens } from '@/shared/auth/authStorage';
import { resolveRole } from '@/shared/roles/resolveRole';
import { getTelegramStartParam, getTelegramUserId, getTelegramWebApp } from '@/shared/telegram/webApp';
import type { TelegramAuthPayload } from '@/shared/auth/types';
import type { BootstrapState } from '@/app/bootstrap/types';

function getRoleFromQuery(): string | null {
  const params = new URLSearchParams(window.location.search);
  return params.get('role') ?? params.get('mockRole');
}

function roleFromStartParam(startParam: string | null): string | null {
  if (!startParam) return null;

  if (startParam.includes('role=')) {
    const match = startParam.match(/role=([a-zA-Z_-]+)/);
    return match?.[1] ?? null;
  }

  return startParam;
}

function buildAuthPayload(): TelegramAuthPayload {
  const webApp = getTelegramWebApp();
  const queryRole = getRoleFromQuery();
  const startParam = getTelegramStartParam();
  const roleHint = roleFromStartParam(startParam) ?? queryRole ?? import.meta.env.VITE_DEFAULT_ROLE ?? 'client';
  const initData = webApp?.initData?.trim() || `role=${roleHint}`;
  const initDataUnsafe =
    webApp?.initDataUnsafe && typeof webApp.initDataUnsafe === 'object'
      ? {
          ...webApp.initDataUnsafe,
          start_param: webApp.initDataUnsafe.start_param || `role=${roleHint}`,
        }
      : {
          start_param: `role=${roleHint}`,
        };

  return {
    initData,
    initDataUnsafe,
    userId: getTelegramUserId(),
  };
}

export function useAppBootstrap(): BootstrapState {
  const [state, setState] = useState<BootstrapState>({ status: 'loading' });

  useEffect(() => {
    const controller = new AbortController();

    const bootstrap = async () => {
      try {
        const authResult = await authenticateTelegram(buildAuthPayload(), controller.signal);

        if (authResult?.tokens) {
          setStoredTokens(authResult.tokens);
        }

        const role = resolveRole({
          queryRole: getRoleFromQuery(),
          startParamRole: roleFromStartParam(getTelegramStartParam()),
          authRole: authResult?.role ?? null,
          fallbackRole: import.meta.env.VITE_DEFAULT_ROLE,
        });

        setState({ status: 'ready', role });
      } catch (error) {
        if ((error as Error).name === 'AbortError') {
          return;
        }
        const role = resolveRole({
          queryRole: getRoleFromQuery(),
          startParamRole: roleFromStartParam(getTelegramStartParam()),
          authRole: null,
          fallbackRole: import.meta.env.VITE_DEFAULT_ROLE,
        });

        setState({ status: 'ready', role });
      }
    };

    void bootstrap();

    return () => {
      controller.abort();
    };
  }, []);

  return state;
}
