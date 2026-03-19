import { useEffect, useState } from 'react';
import { resolveRole } from '@/app/bootstrap/resolveRole';
import { getTelegramStartParam } from '@/shared/telegram/webApp';
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

export function useAppBootstrap(): BootstrapState {
  const [state, setState] = useState<BootstrapState>({ status: 'loading' });

  useEffect(() => {
    const role = resolveRole({
      queryRole: getRoleFromQuery(),
      startParamRole: roleFromStartParam(getTelegramStartParam()),
      fallbackRole: import.meta.env.VITE_DEFAULT_ROLE,
    });
    setState({ status: 'ready', role });
  }, []);

  return state;
}
