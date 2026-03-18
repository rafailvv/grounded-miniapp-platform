import type { TelegramAuthPayload, TelegramAuthResult } from '@/shared/auth/types';

function normalizeAuthResponse(payload: unknown): TelegramAuthResult {
  if (!payload || typeof payload !== 'object') {
    return {};
  }

  const data = payload as Record<string, unknown>;

  const accessToken = typeof data.access_token === 'string' ? data.access_token : undefined;
  const refreshToken = typeof data.refresh_token === 'string' ? data.refresh_token : undefined;
  const tokenType = typeof data.token_type === 'string' ? data.token_type : undefined;
  const expiresAt = typeof data.expires_at === 'string' ? data.expires_at : undefined;

  const explicitRole = typeof data.role === 'string' ? data.role : null;
  const nestedRole =
    data.user && typeof data.user === 'object' && typeof (data.user as Record<string, unknown>).role === 'string'
      ? ((data.user as Record<string, unknown>).role as string)
      : null;

  return {
    role: explicitRole ?? nestedRole,
    tokens: accessToken
      ? {
          accessToken,
          refreshToken,
          tokenType,
          expiresAt,
        }
      : undefined,
  };
}

export async function authenticateTelegram(payload: TelegramAuthPayload, signal?: AbortSignal): Promise<TelegramAuthResult | null> {
  const endpoint = import.meta.env.VITE_AUTH_ENDPOINT?.trim();

  if (!endpoint) {
    return null;
  }

  const response = await fetch(endpoint, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      init_data: payload.initData,
      user_id: payload.userId,
      init_data_unsafe: payload.initDataUnsafe,
    }),
    signal,
  });

  if (!response.ok) {
    const error = new Error(`Telegram auth failed with status ${response.status}`);
    (error as Error & { status?: number }).status = response.status;
    throw error;
  }

  const data = (await response.json()) as unknown;
  return normalizeAuthResponse(data);
}
