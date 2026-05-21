import type {
  RpcKnownMethod,
  RpcNotificationEnvelopeV2,
  RpcParamsByMethod,
  RpcResponseEnvelopeV2,
} from "./generated/app-protocol-types";

export type RpcNotificationHandler = (message: RpcNotificationEnvelopeV2) => void;

export class RpcConnectionError extends Error {
  constructor(message = "RPC socket connection failed.") {
    super(message);
    this.name = "RpcConnectionError";
  }
}

export function isRpcConnectionError(error: unknown): boolean {
  return (
    error instanceof RpcConnectionError ||
    (error instanceof Error &&
      (error.message === "RPC socket connection failed." || error.message === "RPC socket is not open."))
  );
}

export class AppProtocolClient {
  private socket: WebSocket | null = null;
  private nextId = 1;
  private pending = new Map<number, { resolve: (value: unknown) => void; reject: (error: Error) => void }>();
  private connected: Promise<void> | null = null;
  private handlers = new Set<RpcNotificationHandler>();

  async call<T = unknown>(
    method: string,
    params: Record<string, unknown> = {},
    options: { idempotencyKey?: string } = {},
  ): Promise<T> {
    await this.ensureConnected();
    return this.callRaw<T>(method, params, options.idempotencyKey);
  }

  notify<M extends RpcKnownMethod>(method: M, params: RpcParamsByMethod[M]): void {
    const socket = this.socket;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      throw new Error("RPC socket is not open.");
    }
    socket.send(JSON.stringify({ jsonrpc: "2.0", method, params }));
  }

  subscribe(handler: RpcNotificationHandler): () => void {
    this.handlers.add(handler);
    void this.ensureConnected().catch(() => undefined);
    return () => {
      this.handlers.delete(handler);
    };
  }

  private async ensureConnected(): Promise<void> {
    if (this.socket?.readyState === WebSocket.OPEN) {
      return;
    }
    if (this.connected) {
      return this.connected;
    }
    this.connected = new Promise<void>((resolve, reject) => {
      const socket = new WebSocket(this.rpcUrl());
      this.socket = socket;
      let settled = false;
      const fail = (error: Error) => {
        if (settled) {
          return;
        }
        settled = true;
        this.connected = null;
        if (this.socket === socket) {
          this.socket = null;
        }
        if (socket.readyState === WebSocket.CONNECTING || socket.readyState === WebSocket.OPEN) {
          socket.close();
        }
        this.rejectPending(error);
        reject(error);
      };
      const succeed = () => {
        if (settled) {
          return;
        }
        settled = true;
        resolve();
      };
      const timeout = window.setTimeout(() => {
        fail(new RpcConnectionError("RPC socket connection failed."));
      }, 5000);
      socket.addEventListener("open", async () => {
        try {
          await this.callRaw("initialize", { clientInfo: { name: "grounded-miniapp-frontend" } });
          window.clearTimeout(timeout);
          succeed();
        } catch (error) {
          window.clearTimeout(timeout);
          fail(error instanceof Error ? error : new RpcConnectionError("RPC socket connection failed."));
        }
      });
      socket.addEventListener("message", (event) => {
        const message = JSON.parse(String(event.data)) as RpcResponseEnvelopeV2 | RpcNotificationEnvelopeV2;
        if ("id" in message && message.id !== undefined && message.id !== null) {
          const id = Number(message.id);
          const pending = this.pending.get(id);
          if (!pending) {
            return;
          }
          this.pending.delete(id);
          if (message.error) {
            pending.reject(new Error(message.error.message));
          } else {
            pending.resolve(message.result);
          }
          return;
        }
        this.handlers.forEach((handler) => handler(message as RpcNotificationEnvelopeV2));
      });
      socket.addEventListener("close", () => {
        this.socket = null;
        this.connected = null;
        window.clearTimeout(timeout);
        if (!settled) {
          fail(new RpcConnectionError("RPC socket connection failed."));
        }
      });
      socket.addEventListener("error", () => {
        window.clearTimeout(timeout);
        fail(new RpcConnectionError("RPC socket connection failed."));
      });
    });
    return this.connected;
  }

  private async callRaw<T>(method: string, params: Record<string, unknown> = {}, idempotencyKey?: string): Promise<T> {
    const socket = this.socket;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      throw new Error("RPC socket is not open.");
    }
    const id = this.nextId++;
    const result = new Promise<T>((resolve, reject) => {
      this.pending.set(id, { resolve: resolve as (value: unknown) => void, reject });
    });
    socket.send(JSON.stringify({ jsonrpc: "2.0", id, method, params, ...(idempotencyKey ? { idempotency_key: idempotencyKey } : {}) }));
    return result;
  }

  private rpcUrl(): string {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    return `${protocol}//${window.location.host}/rpc`;
  }

  private rejectPending(error: Error): void {
    for (const pending of this.pending.values()) {
      pending.reject(error);
    }
    this.pending.clear();
  }
}
