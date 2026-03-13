export type AuthTokens = {
  accessToken: string;
  refreshToken?: string;
  tokenType?: string;
  expiresAt?: string;
};

export type TelegramAuthPayload = {
  initData: string;
  initDataUnsafe: unknown;
  userId: number | null;
};

export type TelegramAuthResult = {
  role?: string | null;
  tokens?: AuthTokens;
};
