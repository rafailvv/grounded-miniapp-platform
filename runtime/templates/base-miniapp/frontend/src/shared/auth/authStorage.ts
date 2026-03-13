import type { AuthTokens } from '@/shared/auth/types';

const TOKENS_KEY = 'miniapp:auth:tokens';

function parseTokens(raw: string | null): AuthTokens | null {
  if (!raw) return null;

  try {
    return JSON.parse(raw) as AuthTokens;
  } catch {
    return null;
  }
}

export function getStoredTokens(): AuthTokens | null {
  return parseTokens(localStorage.getItem(TOKENS_KEY));
}

export function setStoredTokens(tokens: AuthTokens): void {
  localStorage.setItem(TOKENS_KEY, JSON.stringify(tokens));
}

export function clearStoredTokens(): void {
  localStorage.removeItem(TOKENS_KEY);
}

export function buildAuthorizationHeader(): string | null {
  const tokens = getStoredTokens();
  if (!tokens?.accessToken) return null;

  const type = tokens.tokenType?.toLowerCase() === 'bearer' || !tokens.tokenType ? 'Bearer' : tokens.tokenType;
  return `${type} ${tokens.accessToken}`;
}
